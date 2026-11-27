//! BTC perpetual-futures market data from US-accessible venues (`perp_*` streams, opt-in via
//! `--only perps`). Frames are recorded raw; `doppel_fair::perp` decodes them offline.
//!
//! - `perp_kalshi_btc`: Kalshi perps (margin) WebSocket `wss://external-api-margin-ws.kalshi.com/
//!   trade-api/ws/v2/margin`, channels `orderbook_delta`, `trade`, `ticker` for KXBTCPERP. The
//!   handshake requires an API key (AsyncAPI `security: apiKey`; an unsigned handshake returns 401,
//!   probed 2026-11-27), signed exactly as the event-contract WS, so the read-only Kalshi profile is
//!   loaded from `$CREDENTIALS_DIRECTORY/kalshi-profile.json` and the stream idles without it.
//!   `seq` is per sid on `orderbook_delta` and `trade` (`ticker` has none): a gap, a rejected
//!   subscription, missing acks or snapshot, no data for `no_data_s`, or a backpressure drop
//!   records a ctl and reconnects, which brings a fresh snapshot. Kalshi pings every 10 s.
//! - `perp_kalshi_btc_rest`: the public, unauthenticated margin REST (`security: []` on
//!   get-market, get-market-orderbook, get-trades, get-funding-rate-estimate): book and trades once
//!   a second, market (reference + mark) every 5 s, funding estimate every 10 s. Needs no
//!   credential, so it is what records Kalshi perps when the key is not enabled for margin.
//! - `perp_cbde_btc` lives in [`crate::l2::SpotL2::cbde_btc`] (Coinbase Advanced Trade public WS).
//! - `perp_cbde_btc_rest`: the public Advanced Trade product (`/api/v3/brokerage/market/products/
//!   BIP-20DEC30-CDE`) every 5 s, for `index_price` and the hourly `funding_rate`.

use std::collections::{BTreeMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::{json, Value};
use tokio::sync::watch;

use crate::clock::{mono_ns, wall_ns};
use crate::http::err_kind;
use crate::kalshi::{load_credential, Credential};
use crate::recorder::Recorder;
use crate::ws::{sleep_or_stop, Out, WsVenue};

pub const KALSHI_PERP_WS_URL: &str = "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin";
pub const KALSHI_PERP_REST: &str = "https://external-api.kalshi.com/trade-api/v2/margin";
pub const KALSHI_BTC_PERP: &str = "KXBTCPERP";
pub const CBDE_BTC_PERP: &str = "BIP-20DEC30-CDE";
pub const CBDE_PRODUCT_URL: &str = "https://api.coinbase.com/api/v3/brokerage/market/products/BIP-20DEC30-CDE";
pub const KALSHI_PERP_CHANNELS: [&str; 3] = ["orderbook_delta", "trade", "ticker"];

/// Kalshi margin WS for one market.
pub struct KalshiPerpWs {
    pub url: String,
    pub ticker: String,
    pub profile_path: Option<PathBuf>,
    /// subscribe -> every `subscribed` ack and the first snapshot
    pub ack_timeout: Duration,
    /// longest wait for a snapshot, delta, trade or ticker frame
    pub no_data_s: f64,
    cred: Option<Credential>,
    sids: BTreeMap<u64, String>,
    last_seq: BTreeMap<u64, u64>,
    sub_sent: Option<Instant>,
    snapshot: bool,
    last_data: Instant,
    drops_seen: u64,
    reconnect: Option<String>,
}

impl Default for KalshiPerpWs {
    fn default() -> Self {
        Self::new()
    }
}

impl KalshiPerpWs {
    pub fn new() -> Self {
        Self {
            url: KALSHI_PERP_WS_URL.into(),
            ticker: KALSHI_BTC_PERP.into(),
            profile_path: None,
            ack_timeout: Duration::from_secs(15),
            no_data_s: 30.0,
            cred: None,
            sids: BTreeMap::new(),
            last_seq: BTreeMap::new(),
            sub_sent: None,
            snapshot: false,
            last_data: Instant::now(),
            drops_seen: 0,
            reconnect: None,
        }
    }

    fn leave(&mut self, rec: &Recorder, ctl: &str, kw: Value) {
        if self.reconnect.is_none() {
            rec.ctl(ctl, kw);
            self.reconnect = Some(ctl.to_string());
        }
    }

    /// Bookkeeping for one recorded frame (it is already on tape).
    pub fn handle(&mut self, raw: &str, rec: &Recorder) {
        if self.reconnect.is_some() {
            return;
        }
        let Ok(Value::Object(d)) = serde_json::from_str::<Value>(raw) else { return };
        let typ = d.get("type").and_then(Value::as_str).unwrap_or("");
        let msg = d.get("msg");
        match typ {
            "subscribed" => {
                let sid = msg.and_then(|m| m.get("sid")).and_then(Value::as_u64);
                let ch = msg.and_then(|m| m.get("channel")).and_then(Value::as_str).unwrap_or("");
                if let Some(sid) = sid {
                    self.sids.insert(sid, ch.to_string());
                }
                rec.stats.set_extra("subscribed", self.sids.len());
                return;
            }
            "error" => {
                rec.stats.incr("sub_errors");
                let code = msg.and_then(|m| m.get("code")).cloned().unwrap_or(Value::Null);
                let text: String = msg.and_then(|m| m.get("msg")).and_then(Value::as_str).unwrap_or("").chars().take(300).collect();
                return self.leave(rec, "sub_error", json!({"code": code, "error": text}));
            }
            "orderbook_snapshot" => self.snapshot = true,
            _ => {}
        }
        if matches!(typ, "orderbook_snapshot" | "orderbook_delta" | "trade" | "ticker") {
            self.last_data = Instant::now();
        }
        let (Some(sid), Some(seq)) = (d.get("sid").and_then(Value::as_u64), d.get("seq").and_then(Value::as_u64)) else { return };
        if let Some(prev) = self.last_seq.insert(sid, seq) {
            if seq != prev + 1 {
                rec.stats.incr("seq_gaps");
                let ch = self.sids.get(&sid).cloned().unwrap_or_default();
                self.leave(rec, "seq_gap", json!({"sid": sid, "channel": ch, "expected": prev + 1, "got": seq}));
            }
        }
    }

    /// Acks, first snapshot, data flow and backpressure (once a second).
    pub fn check(&mut self, rec: &Recorder) {
        if self.reconnect.is_some() {
            return;
        }
        let drops = rec.stats.backpressure_drops.load(Ordering::Relaxed);
        if drops > self.drops_seen {
            self.drops_seen = drops;
            rec.stats.incr("resyncs");
            return self.leave(rec, "backpressure_drop", json!({"drops": drops}));
        }
        let late = self.sub_sent.is_some_and(|t| t.elapsed() >= self.ack_timeout);
        if late && (self.sids.len() < KALSHI_PERP_CHANNELS.len() || !self.snapshot) {
            let sids: Vec<_> = self.sids.values().cloned().collect();
            return self.leave(rec, "no_ack", json!({"acked": sids, "snapshot": self.snapshot}));
        }
        if self.last_data.elapsed() > Duration::from_secs_f64(self.no_data_s) {
            rec.stats.incr("no_data");
            self.leave(rec, "no_data", json!({"waited_s": self.no_data_s}));
        }
    }
}

#[async_trait]
impl WsVenue for KalshiPerpWs {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        30.0
    }
    fn pings_are_liveness(&self) -> bool {
        true
    }
    fn wants_feedback(&self) -> bool {
        true
    }
    async fn prepare(&mut self, rec: &Recorder) -> bool {
        for k in ["sub_errors", "seq_gaps", "resyncs", "no_data", "subscribed"] {
            rec.stats.set_extra(k, 0);
        }
        self.cred = load_credential(self.profile_path.clone(), rec);
        self.cred.is_some()
    }
    async fn connect_headers(&mut self) -> Result<Vec<(String, String)>, String> {
        let c = self.cred.as_ref().ok_or("no credential")?;
        let rest = self.url.split_once("://").map(|x| x.1).unwrap_or(&self.url);
        let path = rest.find('/').map(|i| &rest[i..]).unwrap_or("/");
        Ok(c.headers(path.split('?').next().unwrap_or("/")))
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.sids.clear();
        self.last_seq.clear();
        self.snapshot = false;
        self.reconnect = None;
        self.last_data = Instant::now();
        self.sub_sent = Some(Instant::now());
        self.drops_seen = rec.stats.backpressure_drops.load(Ordering::Relaxed);
        let sub = json!({"id": 1, "cmd": "subscribe", "params": {"channels": KALSHI_PERP_CHANNELS, "market_tickers": [self.ticker]}});
        out.push(sub.to_string());
        rec.ctl("subscribe", json!({"sub": sub}));
        Ok(())
    }
    async fn on_tick(&mut self, rec: &Recorder, _out: &mut Out) -> Result<(), String> {
        self.check(rec);
        Ok(())
    }
    fn on_feedback(&mut self, raw: &str, rec: &Recorder, _out: &mut Out) {
        self.handle(raw, rec);
    }
    fn on_feedback_overflow(&mut self, _rec: &Recorder) {
        self.reconnect = Some("feedback_overflow".into());
    }
    fn take_reconnect(&mut self) -> Option<String> {
        self.reconnect.take()
    }
}

/// One polled public REST resource.
#[derive(Clone, Debug)]
pub struct Target {
    pub key: &'static str,
    pub url: String,
    pub every: Duration,
}

/// Public REST poller over a fixed set of targets. Every response body is recorded raw with
/// `{key, status, sent_tw, sent_tm}`; transport errors are `ctl: poll_error`. After a 429 or a 5xx
/// the poller backs off 5 s, doubling to 300 s (`ctl: backoff`), and a success resets it.
pub struct PerpRest {
    pub targets: Vec<Target>,
    backoff_s: f64,
}

impl PerpRest {
    pub fn new(targets: Vec<Target>) -> Self {
        Self { targets, backoff_s: 0.0 }
    }

    pub fn kalshi_btc() -> Self {
        let b = KALSHI_PERP_REST;
        let t = KALSHI_BTC_PERP;
        let s = Duration::from_secs;
        Self::new(vec![
            Target { key: "orderbook", url: format!("{b}/markets/{t}/orderbook"), every: s(1) },
            Target { key: "trades", url: format!("{b}/trades?ticker={t}&limit=100"), every: s(1) },
            Target { key: "market", url: format!("{b}/markets/{t}"), every: s(5) },
            Target { key: "funding", url: format!("{b}/funding_rates/estimate?ticker={t}"), every: s(10) },
        ])
    }

    pub fn cbde_btc() -> Self {
        Self::new(vec![Target { key: "product", url: CBDE_PRODUCT_URL.into(), every: Duration::from_secs(5) }])
    }

    pub async fn run(mut self, rec: Recorder, mut stop: watch::Receiver<bool>) {
        let client = crate::http::client(5);
        let mut due: Vec<Instant> = vec![Instant::now(); self.targets.len()];
        let keys: HashSet<&str> = self.targets.iter().map(|t| t.key).collect();
        rec.stats.set_extra("targets", keys.len());
        while !*stop.borrow() {
            let now = Instant::now();
            let mut wait = None;
            for (i, t) in self.targets.clone().iter().enumerate() {
                if due[i] <= now {
                    due[i] = now + t.every;
                    if let Some(w) = self.poll(&client, &rec, t).await {
                        wait = Some(w);
                        break;
                    }
                }
            }
            if let Some(w) = wait {
                rec.ctl("backoff", json!({"wait_s": w}));
                sleep_or_stop(Duration::from_secs_f64(w), &mut stop).await;
                continue;
            }
            let next = due.iter().min().copied().unwrap_or(now + Duration::from_secs(1));
            sleep_or_stop(next.saturating_duration_since(Instant::now()), &mut stop).await;
        }
        rec.stats.set_connected(false);
    }

    /// One request. Returns the back-off to wait after a 429 or 5xx.
    pub async fn poll(&mut self, client: &reqwest::Client, rec: &Recorder, t: &Target) -> Option<f64> {
        let (sent_w, sent_m) = (wall_ns(), mono_ns());
        let res = async {
            let r = client.get(&t.url).send().await?;
            let status = r.status().as_u16();
            Ok::<_, reqwest::Error>((status, r.text().await?))
        }
        .await;
        match res {
            Ok((status, body)) => {
                if status != 200 {
                    rec.stats.incr(&format!("http_{status}"));
                }
                rec.record_at(
                    body,
                    wall_ns(),
                    mono_ns(),
                    Some(json!({"key": t.key, "status": status, "sent_tw": sent_w, "sent_tm": sent_m})),
                );
                rec.stats.set_connected(status == 200);
                if status == 429 || status >= 500 {
                    self.backoff_s = (2.0 * self.backoff_s).clamp(5.0, 300.0);
                    return Some(self.backoff_s);
                }
                if status == 200 {
                    self.backoff_s = 0.0;
                }
                None
            }
            Err(e) => {
                rec.stats.error(format!("{}: {}", t.key, err_kind(&e)));
                rec.ctl("poll_error", json!({"key": t.key, "err": err_kind(&e)}));
                rec.stats.set_connected(false);
                None
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::recorder::spawn_writer;

    #[test]
    fn kalshi_perp_seq_error_ack_and_backpressure() {
        let d = tempfile::tempdir().unwrap();
        let (r, w) = spawn_writer(d.path().to_path_buf(), "p", 3, 64).unwrap();
        let mut v = KalshiPerpWs::new();
        let sub = |sid: u64, ch: &str| json!({"id": 1, "type": "subscribed", "msg": {"channel": ch, "sid": sid}}).to_string();
        for (sid, ch) in [(1, "orderbook_delta"), (2, "trade"), (3, "ticker")] {
            v.handle(&sub(sid, ch), &r);
        }
        v.handle(r#"{"type":"orderbook_snapshot","sid":1,"seq":1,"msg":{}}"#, &r);
        v.handle(r#"{"type":"orderbook_delta","sid":1,"seq":2,"msg":{}}"#, &r);
        v.handle(r#"{"type":"trade","sid":2,"seq":1,"msg":{}}"#, &r);
        v.handle(r#"{"type":"ticker","sid":3,"msg":{}}"#, &r);
        v.check(&r);
        assert!(v.reconnect.is_none());
        v.handle(r#"{"type":"trade","sid":2,"seq":3,"msg":{}}"#, &r);
        assert_eq!(v.take_reconnect().as_deref(), Some("seq_gap"));
        assert_eq!(r.stats.extra("seq_gaps").unwrap(), 1);

        let mut v = KalshiPerpWs::new();
        v.handle(r#"{"id":1,"type":"error","msg":{"code":6,"msg":"Already subscribed"}}"#, &r);
        assert_eq!(v.take_reconnect().as_deref(), Some("sub_error"));

        let mut v = KalshiPerpWs::new();
        v.ack_timeout = Duration::ZERO;
        v.sub_sent = Some(Instant::now());
        v.handle(&sub(1, "orderbook_delta"), &r);
        v.check(&r);
        assert_eq!(v.take_reconnect().as_deref(), Some("no_ack"));

        let mut v = KalshiPerpWs::new();
        r.stats.backpressure_drops.fetch_add(1, Ordering::Relaxed);
        v.check(&r);
        assert_eq!(v.take_reconnect().as_deref(), Some("backpressure_drop"));
        v.check(&r);
        assert_eq!(v.take_reconnect(), None);

        let mut v = KalshiPerpWs::new();
        v.drops_seen = 1;
        v.no_data_s = 0.0;
        std::thread::sleep(Duration::from_millis(5));
        v.check(&r);
        assert_eq!(v.take_reconnect().as_deref(), Some("no_data"));
        drop(r);
        w.join();
    }

    #[test]
    fn rest_targets() {
        let k = PerpRest::kalshi_btc();
        let keys: Vec<_> = k.targets.iter().map(|t| t.key).collect();
        assert_eq!(keys, ["orderbook", "trades", "market", "funding"]);
        assert_eq!(k.targets[0].url, "https://external-api.kalshi.com/trade-api/v2/margin/markets/KXBTCPERP/orderbook");
        assert_eq!(PerpRest::cbde_btc().targets[0].url, CBDE_PRODUCT_URL);
    }
}
