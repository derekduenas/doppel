//! Spot L2 order books for the BRTI constituents (`spot_l2_<venue>`), BTC-USD only. Subscriptions
//! and channels are PERELMAN `recorder.py` FEEDS (verified live 2026-11-24); frames are recorded
//! raw and decoded offline by `doppel_fair::venue`. LMAX Digital has no public WebSocket, so
//! [`RestL2`] polls its REST book once a second, as PERELMAN `rest_poll` does.
//!
//! Continuity, for the venues that send deltas:
//! - Coinbase `sequence_num` and Gemini `socket_sequence` count every frame on the connection; a
//!   jump is recorded as `ctl: seq_gap` and the connection is dropped, so the reconnect brings a
//!   fresh snapshot and a fresh sequence (Gemini's docs prescribe exactly this). `run_ws`'s
//!   backoff rate-limits repeated gaps.
//! - Crypto.com is subscribed as PERELMAN does (`book.BTC_USD.150`, full 150-level snapshots,
//!   ~2.3/s). Its delta mode exists only at depth 50 ("Unsupported depth/interval combination" at
//!   150, probed 2026-11-26); should deltas arrive, `pu` must equal the previous `u`.
//! - Kraken has no sequence, only a CRC32 of the top 10 levels; it is checked offline by the
//!   decoder (keeping book state out of the recorder).
//! - A backpressure drop leaves a hole in the tape even though the socket is fine, so delta venues
//!   reconnect for a fresh snapshot when `backpressure_drops` moves.
//!
//! Heartbeats are recorded (they carry the sequence) and so keep the stall watchdog quiet; a
//! subscription the venue rejected would then look healthy forever. So every venue must also
//! deliver a book frame at least every `no_data_s`, or the connection is dropped (`ctl: no_data`).

use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{json, Value};

use tokio::sync::watch;

use crate::clock::{mono_ns, wall_ns};
use crate::http::{err_kind, RateLimiter};
use crate::recorder::Recorder;
use crate::ws::{sleep_or_stop, Out, WsVenue};

/// WebSocket stream suffixes, in start order.
pub const L2_VENUES: [&str; 6] = ["coinbase", "kraken", "bitstamp", "gemini", "cryptocom", "bullish"];
/// REST-polled stream suffixes ([`RestL2`]).
pub const L2_REST_VENUES: [&str; 1] = ["lmax"];

/// PERELMAN `REST_FEEDS["lmax"]`.
pub const LMAX_URL: &str = "https://public-data-api.london-digital.lmax.com/v1/orderbook/btc-usd";

const BULLISH_PING: &str = r#"{"jsonrpc":"2.0","type":"command","method":"keepalivePing","params":{},"id":"0"}"#;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum V {
    Coinbase,
    Kraken,
    Bitstamp,
    Gemini,
    Cryptocom,
    Bullish,
    /// Coinbase Derivatives BTC perp-style future on the same public Advanced Trade feed
    CbdePerp,
}

pub struct SpotL2 {
    pub url: String,
    /// longest wait for a book frame (snapshot or delta) before reconnecting
    pub no_data_s: f64,
    v: V,
    last_book: Instant,
    last_seq: Option<u64>,
    drops_seen: u64,
    reconnect: Option<String>,
}

impl SpotL2 {
    /// `venue` is one of [`L2_VENUES`].
    pub fn new(venue: &str) -> Option<Self> {
        let v = match venue {
            "coinbase" => V::Coinbase,
            "kraken" => V::Kraken,
            "bitstamp" => V::Bitstamp,
            "gemini" => V::Gemini,
            "cryptocom" => V::Cryptocom,
            "bullish" => V::Bullish,
            _ => return None,
        };
        let url = match v {
            V::Coinbase | V::CbdePerp => "wss://advanced-trade-ws.coinbase.com",
            V::Kraken => "wss://ws.kraken.com/v2",
            V::Bitstamp => "wss://ws.bitstamp.net",
            V::Gemini => "wss://api.gemini.com/v1/marketdata/BTCUSD?heartbeat=true&trades=false&auctions=false",
            V::Cryptocom => "wss://stream.crypto.com/exchange/v1/market",
            V::Bullish => "wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook",
        };
        Some(Self { url: url.into(), no_data_s: 30.0, v, last_book: Instant::now(), last_seq: None, drops_seen: 0, reconnect: None })
    }

    /// `perp_cbde_btc`: Coinbase Derivatives BIP-20DEC30-CDE (nano BTC perp-style future, 0.01 BTC)
    /// on the public Advanced Trade feed: `level2` + `market_trades` + `heartbeats`, no JWT (the
    /// JWT is "optional; recommended for connection reliability"). One `sequence_num` counts every
    /// frame on the connection, as for spot.
    pub fn cbde_btc() -> Self {
        let mut s = Self::new("coinbase").expect("coinbase");
        s.v = V::CbdePerp;
        s
    }

    fn incremental(&self) -> bool {
        matches!(self.v, V::Coinbase | V::Kraken | V::Gemini | V::CbdePerp)
    }

    /// A frame that carries book data (not an ack, heartbeat, pong or error).
    fn is_book(&self, m: &Value) -> bool {
        let s = |v: Option<&Value>| v.and_then(Value::as_str).unwrap_or("").to_string();
        match self.v {
            V::Coinbase | V::CbdePerp => s(m.get("channel")) == "l2_data",
            V::Kraken => s(m.get("channel")) == "book",
            V::Bitstamp => s(m.get("event")) == "data",
            V::Gemini => s(m.get("type")) == "update",
            V::Cryptocom => s(m.get("result").and_then(|r| r.get("channel"))).starts_with("book"),
            V::Bullish => s(m.get("dataType")) == "V1TALevel2",
        }
    }

    fn subscriptions(&self) -> Vec<Value> {
        match self.v {
            V::Coinbase => vec![
                json!({"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "level2"}),
                json!({"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "heartbeats"}),
            ],
            V::Kraken => vec![json!({"method": "subscribe", "params": {"channel": "book", "symbol": ["BTC/USD"], "depth": 1000}})],
            V::Bitstamp => vec![json!({"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}})],
            V::Gemini => vec![], // the URL is the subscription
            V::CbdePerp => vec![
                json!({"type": "subscribe", "product_ids": [crate::perps::CBDE_BTC_PERP], "channel": "level2"}),
                json!({"type": "subscribe", "product_ids": [crate::perps::CBDE_BTC_PERP], "channel": "market_trades"}),
                json!({"type": "subscribe", "channel": "heartbeats"}),
            ],
            V::Cryptocom => vec![json!({"id": 1, "method": "subscribe", "params": {"channels": ["book.BTC_USD.150"]}})],
            V::Bullish => vec![
                json!({"jsonrpc": "2.0", "type": "command", "method": "subscribe", "params": {"topic": "l2Orderbook", "symbol": "BTCUSD"}, "id": "1"}),
            ],
        }
    }

    fn gap(&mut self, rec: &Recorder, expected: u64, got: u64) {
        rec.stats.incr("seq_gaps");
        rec.ctl("seq_gap", json!({"expected": expected, "got": got}));
        self.reconnect = Some("seq_gap".into());
    }

    /// Per-connection counter that must step by exactly one.
    fn step(&mut self, rec: &Recorder, got: u64) {
        if let Some(prev) = self.last_seq {
            if got != prev + 1 {
                return self.gap(rec, prev + 1, got);
            }
        }
        self.last_seq = Some(got);
    }

    /// Continuity checks and venue protocol replies for one recorded frame.
    pub fn handle(&mut self, raw: &str, rec: &Recorder, out: &mut Out) {
        if self.reconnect.is_some() {
            return; // already leaving this connection
        }
        let Ok(m) = serde_json::from_str::<Value>(raw) else { return };
        if self.is_book(&m) {
            self.last_book = Instant::now();
        }
        match self.v {
            V::Coinbase | V::CbdePerp => {
                if let Some(s) = m.get("sequence_num").and_then(Value::as_u64) {
                    self.step(rec, s);
                }
            }
            V::Gemini => {
                if let Some(s) = m.get("socket_sequence").and_then(Value::as_u64) {
                    self.step(rec, s);
                }
            }
            V::Cryptocom => {
                if m.get("method").and_then(Value::as_str) == Some("public/heartbeat") {
                    out.push(json!({"id": m.get("id"), "method": "public/respond-heartbeat"}).to_string());
                    return;
                }
                let Some(r) = m.get("result") else { return };
                let Some(d) = r.get("data").and_then(|d| d.get(0)) else { return };
                let u = d.get("u").or_else(|| d.get("s")).and_then(Value::as_u64);
                match r.get("channel").and_then(Value::as_str) {
                    Some("book") => self.last_seq = u,
                    Some("book.update") => {
                        if let (Some(prev), Some(pu)) = (self.last_seq, d.get("pu").and_then(Value::as_u64)) {
                            if pu != prev {
                                return self.gap(rec, prev, pu);
                            }
                        }
                        self.last_seq = u;
                    }
                    _ => {}
                }
            }
            V::Bitstamp => {
                if m.get("event").and_then(Value::as_str) == Some("bts:request_reconnect") {
                    self.reconnect = Some("request_reconnect".into());
                }
            }
            V::Kraken | V::Bullish => {}
        }
    }
}

#[async_trait]
impl WsVenue for SpotL2 {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        match self.v {
            V::Coinbase | V::Kraken | V::CbdePerp => 15.0, // 1 s heartbeats
            V::Cryptocom => 45.0,                          // 30 s heartbeats
            _ => 30.0,                                     // Gemini: 5 s heartbeats
        }
    }
    fn keepalive(&self) -> Option<(&'static str, f64)> {
        // Bullish has no protocol-level ping/pong and drops idle clients
        (self.v == V::Bullish).then_some((BULLISH_PING, 30.0))
    }
    fn wants_feedback(&self) -> bool {
        true
    }
    async fn prepare(&mut self, rec: &Recorder) -> bool {
        for k in ["seq_gaps", "resyncs", "no_data"] {
            rec.stats.set_extra(k, 0);
        }
        true
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.last_seq = None;
        self.reconnect = None;
        self.last_book = Instant::now();
        self.drops_seen = rec.stats.backpressure_drops.load(Ordering::Relaxed);
        let subs = self.subscriptions();
        for s in &subs {
            out.push(s.to_string());
        }
        rec.ctl("subscribe", json!({"subs": subs}));
        Ok(())
    }
    async fn on_tick(&mut self, rec: &Recorder, _out: &mut Out) -> Result<(), String> {
        let drops = rec.stats.backpressure_drops.load(Ordering::Relaxed);
        if drops > self.drops_seen && self.incremental() && self.reconnect.is_none() {
            rec.stats.incr("resyncs");
            self.reconnect = Some("backpressure_drop".into());
        }
        self.drops_seen = drops;
        if self.reconnect.is_none() && self.last_book.elapsed() > Duration::from_secs_f64(self.no_data_s) {
            rec.stats.incr("no_data");
            rec.ctl("no_data", json!({"waited_s": self.no_data_s}));
            self.reconnect = Some("no_data".into());
        }
        Ok(())
    }
    fn on_feedback(&mut self, raw: &str, rec: &Recorder, out: &mut Out) {
        self.handle(raw, rec, out);
    }
    fn on_feedback_overflow(&mut self, _rec: &Recorder) {
        // frames were recorded but not checked here: start clean
        self.reconnect = Some("feedback_overflow".into());
    }
    fn take_reconnect(&mut self) -> Option<String> {
        self.reconnect.take()
    }
}

/// A REST book polled once a second (PERELMAN `rest_poll`): 3 s timeout, and after any failure
/// (transport error, non-200, or a body without `bids` and `asks` arrays) a back-off of 5 s
/// doubling to 300 s, so a rate limit is not kept alive by our own retries. Every response body is
/// recorded raw with `{status, sent_tw, sent_tm}`; transport errors are `ctl: poll_error`.
pub struct RestL2 {
    pub url: String,
    pub limiter: RateLimiter,
    backoff_s: f64,
}

impl RestL2 {
    /// `venue` is one of [`L2_REST_VENUES`].
    pub fn new(venue: &str) -> Option<Self> {
        let url = match venue {
            "lmax" => LMAX_URL,
            _ => return None,
        };
        Some(Self { url: url.into(), limiter: RateLimiter::new(1.0, 1), backoff_s: 0.0 })
    }

    pub async fn run(mut self, rec: Recorder, mut stop: watch::Receiver<bool>) {
        let client = crate::http::client(3);
        while !*stop.borrow() {
            tokio::select! {
                _ = self.limiter.take() => {}
                _ = stop.changed() => break,
            }
            if let Some(wait) = self.poll(&client, &rec).await {
                rec.ctl("backoff", json!({"wait_s": wait}));
                sleep_or_stop(Duration::from_secs_f64(wait), &mut stop).await;
            }
        }
        rec.stats.set_connected(false);
    }

    /// One request. Returns the back-off to wait after a failure.
    pub async fn poll(&mut self, client: &reqwest::Client, rec: &Recorder) -> Option<f64> {
        let (sent_w, sent_m) = (wall_ns(), mono_ns());
        let res = async {
            let r = client.get(&self.url).send().await?;
            let status = r.status().as_u16();
            Ok::<_, reqwest::Error>((status, r.text().await?))
        }
        .await;
        let ok = match res {
            Ok((status, body)) => {
                if status != 200 {
                    rec.stats.incr(&format!("http_{status}"));
                }
                let ok = status == 200 && is_rest_book(&body);
                rec.record_at(body, wall_ns(), mono_ns(), Some(json!({"status": status, "sent_tw": sent_w, "sent_tm": sent_m})));
                ok
            }
            Err(e) => {
                rec.stats.error(format!("poll: {}", err_kind(&e)));
                rec.ctl("poll_error", json!({"err": err_kind(&e)}));
                false
            }
        };
        rec.stats.set_connected(ok);
        self.backoff_s = if ok { 0.0 } else { (2.0 * self.backoff_s).clamp(5.0, 300.0) };
        (!ok).then_some(self.backoff_s)
    }
}

/// A body PERELMAN's REST parser would read: `bids` and `asks` arrays.
fn is_rest_book(body: &str) -> bool {
    let has = |j: &Value, k: &str| j.get(k).is_some_and(Value::is_array);
    serde_json::from_str::<Value>(body).is_ok_and(|j| has(&j, "bids") && has(&j, "asks"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::recorder::spawn_writer;

    fn rec() -> (tempfile::TempDir, Recorder, crate::recorder::WriterThread) {
        let d = tempfile::tempdir().unwrap();
        let (r, w) = spawn_writer(d.path().to_path_buf(), "s", 3, 64).unwrap();
        (d, r, w)
    }

    #[test]
    fn sequence_checks_per_venue() {
        let (_d, r, w) = rec();
        let mut out = Out::new();
        let mut cb = SpotL2::new("coinbase").unwrap();
        for s in [0, 1, 2] {
            cb.handle(&json!({"channel": "heartbeats", "sequence_num": s}).to_string(), &r, &mut out);
        }
        assert!(cb.reconnect.is_none());
        cb.handle(&json!({"channel": "l2_data", "sequence_num": 4}).to_string(), &r, &mut out);
        assert_eq!(cb.take_reconnect().as_deref(), Some("seq_gap"));
        assert_eq!(r.stats.extra("seq_gaps").unwrap(), 1);

        let mut g = SpotL2::new("gemini").unwrap();
        g.handle(r#"{"type":"heartbeat","socket_sequence":0}"#, &r, &mut out);
        g.handle(r#"{"type":"heartbeat","socket_sequence":1}"#, &r, &mut out);
        assert!(g.reconnect.is_none());
        g.handle(r#"{"type":"heartbeat","socket_sequence":1}"#, &r, &mut out);
        assert_eq!(g.take_reconnect().as_deref(), Some("seq_gap"));

        let mut c = SpotL2::new("cryptocom").unwrap();
        let book = |ch: &str, d: Value| json!({"result": {"channel": ch, "data": [d]}}).to_string();
        c.handle(&book("book", json!({"u": 10})), &r, &mut out);
        c.handle(&book("book.update", json!({"u": 12, "pu": 10})), &r, &mut out);
        assert!(c.reconnect.is_none());
        c.handle(r#"{"id":77,"method":"public/heartbeat","code":0}"#, &r, &mut out);
        assert_eq!(std::mem::take(&mut out), [r#"{"id":77,"method":"public/respond-heartbeat"}"#]);
        c.handle(&book("book.update", json!({"u": 14, "pu": 13})), &r, &mut out);
        assert_eq!(c.take_reconnect().as_deref(), Some("seq_gap"));

        let mut b = SpotL2::new("bitstamp").unwrap();
        b.handle(r#"{"event":"bts:request_reconnect","channel":"","data":""}"#, &r, &mut out);
        assert_eq!(b.take_reconnect().as_deref(), Some("request_reconnect"));
        assert!(SpotL2::new("lmax").is_none() && RestL2::new("lmax").is_some() && RestL2::new("coinbase").is_none());
        assert_eq!(r.stats.extra("seq_gaps").unwrap(), 3);
        drop(r);
        w.join();
    }

    #[tokio::test]
    async fn backpressure_drop_resyncs_delta_venues_only() {
        let (_d, r, w) = rec();
        let mut out = Out::new();
        let mut k = SpotL2::new("kraken").unwrap();
        let mut bs = SpotL2::new("bitstamp").unwrap();
        k.on_open(&r, &mut out).await.unwrap();
        bs.on_open(&r, &mut out).await.unwrap();
        assert_eq!(out.len(), 2);
        r.stats.backpressure_drops.fetch_add(1, Ordering::Relaxed);
        k.on_tick(&r, &mut out).await.unwrap();
        bs.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(k.take_reconnect().as_deref(), Some("backpressure_drop"));
        assert_eq!(bs.take_reconnect(), None);
        k.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(k.take_reconnect(), None);
        drop(r);
        w.join();
    }

    #[tokio::test]
    async fn heartbeats_alone_trigger_no_data() {
        let (_d, r, w) = rec();
        let mut out = Out::new();
        let mut c = SpotL2::new("cryptocom").unwrap();
        c.no_data_s = 0.2;
        c.on_open(&r, &mut out).await.unwrap();
        // a rejected subscription (seen live at depth 150 in delta mode), then only heartbeats
        c.handle(r#"{"id":1,"method":"subscribe","code":40003,"message":"Unsupported depth/interval combination"}"#, &r, &mut out);
        c.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(c.take_reconnect(), None);
        std::thread::sleep(Duration::from_millis(150));
        c.handle(&json!({"result": {"channel": "book", "data": [{"u": 1}]}}).to_string(), &r, &mut out);
        std::thread::sleep(Duration::from_millis(150));
        c.handle(r#"{"id":5,"method":"public/heartbeat","code":0}"#, &r, &mut out);
        c.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(c.take_reconnect(), None, "a book frame 150 ms ago");
        std::thread::sleep(Duration::from_millis(150));
        c.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(c.take_reconnect().as_deref(), Some("no_data"));
        assert_eq!(r.stats.extra("no_data").unwrap(), 1);
        drop(r);
        w.join();
    }
}
