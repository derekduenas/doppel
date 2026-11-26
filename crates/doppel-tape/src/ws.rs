//! Reconnecting WebSocket recorder (Python `WsSource`).
//!
//! Per connection there are two tasks:
//! - the **reader** does nothing but `next()`, stamp `tw`/`tm` the instant a message is returned,
//!   classify keepalives with a pure function, and `try_send` the payload to the stream's writer
//!   (and, for venues that track state, a copy to the control task). It never awaits anything but
//!   the socket, so universe lookups, subscription bookkeeping and disk I/O cannot delay a stamp.
//! - the **control** task owns the write half and the venue state: subscriptions, 1 s ticks, app
//!   keepalives, protocol pings, the message-gap watchdog and shutdown.
//!
//! Only data frames (and app-level keepalive replies, as in Python) reset the watchdog. Protocol
//! Ping/Pong frames do not, unless the venue opts in (Kalshi only pings when idle), because RTDS is
//! exactly the case where pings keep flowing while data silently stops.

use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures_util::{SinkExt, StreamExt};
use rand::Rng;
use serde_json::json;
use tokio::sync::{mpsc, watch};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

use crate::clock::{mono_ns, wall_ns};
use crate::http::UA;
use crate::recorder::Recorder;

/// What the reader does with a text frame.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Frame {
    /// Record it (and forward it to the venue when it wants feedback).
    Data,
    /// App-level keepalive reply: liveness only, not recorded.
    Keepalive,
    /// Server heartbeat: liveness only, counted in `stats.heartbeats`, not recorded.
    Heartbeat,
}

pub fn default_classify(raw: &str) -> Frame {
    if matches!(raw, "PONG" | "pong") {
        Frame::Keepalive
    } else {
        Frame::Data
    }
}

pub type Out = Vec<String>;

#[async_trait]
pub trait WsVenue: Send + 'static {
    fn url(&self) -> String;
    fn stall_s(&self) -> f64 {
        15.0
    }
    /// App-level ping text and its interval.
    fn keepalive(&self) -> Option<(&'static str, f64)> {
        None
    }
    fn pings_are_liveness(&self) -> bool {
        false
    }
    fn expect_traffic(&self) -> bool {
        true
    }
    fn wants_feedback(&self) -> bool {
        false
    }
    fn classifier(&self) -> fn(&str) -> Frame {
        default_classify
    }
    /// Called once before the first connect; false = idle until stop (e.g. no credential).
    async fn prepare(&mut self, _rec: &Recorder) -> bool {
        true
    }
    /// Extra handshake headers, fetched fresh before EVERY (re)connect. Values are secrets.
    async fn connect_headers(&mut self) -> Result<Vec<(String, String)>, String> {
        Ok(Vec::new())
    }
    fn redact(&self, text: &str) -> String {
        text.to_string()
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String>;
    /// Roughly once a second while connected.
    async fn on_tick(&mut self, _rec: &Recorder, _out: &mut Out) -> Result<(), String> {
        Ok(())
    }
    /// A recorded data frame, in arrival order (only when `wants_feedback`).
    fn on_feedback(&mut self, _raw: &str, _rec: &Recorder, _out: &mut Out) {}
    /// The feedback channel overflowed: some frames were recorded but not seen here.
    fn on_feedback_overflow(&mut self, _rec: &Recorder) {}
    /// Ask the driver to drop the connection (checked after every hook).
    fn take_reconnect(&mut self) -> Option<String> {
        None
    }
}

pub struct WsOpts {
    pub connect_timeout: Duration,
    pub ping_every: Duration,
    pub backoff_max_s: f64,
}

impl Default for WsOpts {
    fn default() -> Self {
        Self { connect_timeout: Duration::from_secs(15), ping_every: Duration::from_secs(20), backoff_max_s: 30.0 }
    }
}

/// Safe error text for a WebSocket failure: an HTTP rejection keeps only its status.
pub fn ws_err(e: &WsError) -> String {
    match e {
        WsError::Http(r) => format!("Http: status {}", r.status().as_u16()),
        WsError::Io(io) => format!("Io: {}", io.kind()),
        WsError::Tls(_) => "Tls".to_string(),
        WsError::Url(u) => format!("Url: {u}"),
        WsError::ConnectionClosed | WsError::AlreadyClosed => "Closed".to_string(),
        WsError::Protocol(p) => format!("Protocol: {p}"),
        WsError::Capacity(c) => format!("Capacity: {c}"),
        WsError::HttpFormat(_) => "HttpFormat".to_string(),
        other => format!("Ws: {other}"),
    }
}

pub async fn sleep_or_stop(d: Duration, stop: &mut watch::Receiver<bool>) {
    if *stop.borrow() {
        return;
    }
    tokio::select! {
        _ = tokio::time::sleep(d) => {}
        _ = stop.changed() => {}
    }
}

pub async fn run_ws<V: WsVenue>(mut v: V, rec: Recorder, mut stop: watch::Receiver<bool>, opts: WsOpts) {
    if !v.prepare(&rec).await {
        while !*stop.borrow() {
            if stop.changed().await.is_err() {
                break;
            }
        }
        return;
    }
    let mut backoff = 0.5f64;
    while !*stop.borrow() {
        let started = Instant::now();
        let connected = Arc::new(AtomicBool::new(false));
        if let Err(e) = session(&mut v, &rec, &mut stop, &opts, &connected).await {
            let msg = v.redact(&e);
            rec.stats.error(msg.clone());
            eprintln!("doppel-taped: {} error: {}", rec.name(), rec.stats.last_error().unwrap_or(msg));
        }
        if connected.load(Ordering::Relaxed) {
            rec.ctl("disconnect", json!({}));
        }
        rec.stats.set_connected(false);
        if *stop.borrow() {
            break;
        }
        rec.stats.reconnects.fetch_add(1, Ordering::Relaxed);
        if started.elapsed() > Duration::from_secs(60) {
            backoff = 0.5;
        }
        let jitter: f64 = rand::thread_rng().gen_range(0.5..1.5);
        sleep_or_stop(Duration::from_secs_f64(backoff * jitter), &mut stop).await;
        backoff = (backoff * 2.0).min(opts.backoff_max_s);
    }
}

async fn send_all<S>(sink: &mut S, out: &mut Out) -> Result<(), String>
where
    S: futures_util::Sink<Message, Error = WsError> + Unpin,
{
    for m in out.drain(..) {
        sink.send(Message::Text(m.into())).await.map_err(|e| ws_err(&e))?;
    }
    Ok(())
}

async fn session<V: WsVenue>(
    v: &mut V,
    rec: &Recorder,
    stop: &mut watch::Receiver<bool>,
    opts: &WsOpts,
    connected: &AtomicBool,
) -> Result<(), String> {
    let url = v.url();
    let headers = v.connect_headers().await?;
    let mut req = url.as_str().into_client_request().map_err(|e| ws_err(&e))?;
    {
        let h = req.headers_mut();
        h.insert("user-agent", HeaderValue::from_static(UA));
        for (k, val) in headers {
            let name = HeaderName::from_bytes(k.as_bytes()).map_err(|_| "bad header name".to_string())?;
            let value = HeaderValue::from_str(&val).map_err(|_| "bad header value".to_string())?;
            h.insert(name, value);
        }
    }
    let mut cfg = WebSocketConfig::default();
    cfg.max_message_size = None;
    cfg.max_frame_size = None;
    // tungstenite never follows redirects: a 3xx is returned as Error::Http, so signed handshake
    // headers are never replayed to another host.
    let (ws, _resp) = tokio::time::timeout(opts.connect_timeout, tokio_tungstenite::connect_async_with_config(req, Some(cfg), true))
        .await
        .map_err(|_| "connect timeout".to_string())?
        .map_err(|e| ws_err(&e))?;
    connected.store(true, Ordering::Relaxed);
    rec.stats.set_connected(true);
    rec.ctl("connect", json!({"url": url}));

    let (mut sink, mut stream) = ws.split();
    let last_rx = Arc::new(AtomicI64::new(mono_ns()));
    let overflow = Arc::new(AtomicBool::new(false));
    let (fb_tx, mut fb_rx) = mpsc::channel::<String>(65_536);
    let reader = {
        let rec = rec.clone();
        let last_rx = last_rx.clone();
        let overflow = overflow.clone();
        let classify = v.classifier();
        let feedback = v.wants_feedback();
        let ping_live = v.pings_are_liveness();
        tokio::spawn(async move {
            while let Some(m) = stream.next().await {
                let (tw, tm) = (wall_ns(), mono_ns());
                let raw: String = match m {
                    Ok(Message::Text(t)) => t.as_str().to_owned(),
                    Ok(Message::Binary(b)) => String::from_utf8_lossy(&b).into_owned(),
                    Ok(Message::Ping(_)) => {
                        if ping_live {
                            last_rx.store(tm, Ordering::Relaxed);
                        }
                        continue;
                    }
                    Ok(Message::Pong(_)) | Ok(Message::Frame(_)) => continue,
                    Ok(Message::Close(_)) => return Ok(()),
                    Err(e) => return Err(ws_err(&e)),
                };
                last_rx.store(tm, Ordering::Relaxed);
                match classify(&raw) {
                    Frame::Data => {
                        if feedback && fb_tx.try_send(raw.clone()).is_err() {
                            overflow.store(true, Ordering::Relaxed);
                        }
                        rec.record_at(raw, tw, tm, None);
                    }
                    Frame::Keepalive => {}
                    Frame::Heartbeat => rec.stats.incr("heartbeats"),
                }
            }
            Ok(())
        })
    };
    let result = control(v, rec, stop, opts, &mut sink, &mut fb_rx, &last_rx, &overflow, reader).await;
    let _ = tokio::time::timeout(Duration::from_secs(2), sink.send(Message::Close(None))).await;
    result
}

#[allow(clippy::too_many_arguments)]
async fn control<V: WsVenue, S>(
    v: &mut V,
    rec: &Recorder,
    stop: &mut watch::Receiver<bool>,
    opts: &WsOpts,
    sink: &mut S,
    fb_rx: &mut mpsc::Receiver<String>,
    last_rx: &AtomicI64,
    overflow: &AtomicBool,
    mut reader: tokio::task::JoinHandle<Result<(), String>>,
) -> Result<(), String>
where
    S: futures_util::Sink<Message, Error = WsError> + Unpin,
{
    let mut out = Out::new();
    let res = async {
        v.on_open(rec, &mut out).await?;
        send_all(sink, &mut out).await?;
        last_rx.store(mono_ns(), Ordering::Relaxed);
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut last_ka = Instant::now();
        let mut last_ping = Instant::now();
        loop {
            if let Some(why) = v.take_reconnect() {
                rec.ctl("reconnect", json!({"why": why}));
                return Ok(());
            }
            tokio::select! {
                _ = stop.changed() => return Ok(()),
                r = &mut reader => {
                    return match r { Ok(r) => r, Err(_) => Err("reader task failed".to_string()) };
                }
                Some(raw) = fb_rx.recv() => {
                    v.on_feedback(&raw, rec, &mut out);
                    send_all(sink, &mut out).await?;
                }
                _ = tick.tick() => {
                    if *stop.borrow() {
                        return Ok(());
                    }
                    if overflow.swap(false, Ordering::Relaxed) {
                        rec.ctl("feedback_overflow", json!({}));
                        v.on_feedback_overflow(rec);
                    }
                    if let Some((msg, every)) = v.keepalive() {
                        if last_ka.elapsed().as_secs_f64() >= every {
                            sink.send(Message::Text(msg.into())).await.map_err(|e| ws_err(&e))?;
                            last_ka = Instant::now();
                        }
                    }
                    if last_ping.elapsed() >= opts.ping_every {
                        sink.send(Message::Ping(Vec::new().into())).await.map_err(|e| ws_err(&e))?;
                        last_ping = Instant::now();
                    }
                    if let Err(e) = v.on_tick(rec, &mut out).await {
                        rec.stats.error(v.redact(&format!("tick: {e}")));
                    }
                    send_all(sink, &mut out).await?;
                    let silent = (mono_ns() - last_rx.load(Ordering::Relaxed)) as f64 / 1e9;
                    if v.expect_traffic() && silent > v.stall_s() {
                        rec.stats.stalls.fetch_add(1, Ordering::Relaxed);
                        rec.ctl("stall", json!({"silent_s": (silent * 1000.0).round() / 1000.0}));
                        eprintln!("doppel-taped: {} stalled {:.1}s; reconnecting", rec.name(), silent);
                        return Ok(());
                    }
                }
            }
        }
    }
    .await;
    if !reader.is_finished() {
        reader.abort();
    }
    res
}
