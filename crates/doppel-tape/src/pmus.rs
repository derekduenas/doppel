//! Polymarket US markets WebSocket (Python `sources/pmus_ws.py`).
//!
//! Auth without holding a key: before EVERY (re)connect we ask the host signer on a Unix socket
//! (`sign-market-ws\n`) for one line `access_key\tts_ms\tsignature\n`; the values become the
//! `X-PM-*` handshake headers. They are never written to the tape, logs or status (`redact`), and
//! never replayed to another host (tungstenite does not follow redirects).
//!
//! One subscription request per (type, horizon, window start); the current and next window of each
//! horizon are subscribed as soon as due, a window is unsubscribed `linger_s` after it ends, a
//! request answered with `error` is retried (5 s after its open, or ~3 s after the open when it
//! failed before it), and a started window whose book request produced no data for some slug within
//! `nodata_s` is unsubscribed and re-sent with doubling delay (cap 120 s). One connection per
//! horizon: PM US caps subscriptions per connection.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

use crate::clock::wall_s;
use crate::recorder::Recorder;
use crate::universe::{horizon_s, pmus_slug, window_starts, ASSETS};
use crate::ws::{Frame, Out, WsVenue};

pub const SIGNER_SOCK: &str = "/run/pmus-riskd-market-signer/markets.sock";
pub const PMUS_WS_URL: &str = "wss://api.polymarket.us/v1/ws/markets";
pub const HEADER_NAMES: [&str; 3] = ["X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"];
const REDACTED: &str = "<redacted>";

fn sub_type(kind: &str) -> &'static str {
    if kind == "md" {
        "SUBSCRIPTION_TYPE_MARKET_DATA"
    } else {
        "SUBSCRIPTION_TYPE_TRADE"
    }
}
const KINDS: [&str; 2] = ["md", "tr"];

/// Ask the signer for fresh handshake headers. Errors never contain anything the signer returned.
pub async fn fetch_signed_headers(sock: &str, timeout: Duration) -> Result<Vec<(String, String)>, String> {
    let mut s = tokio::time::timeout(timeout, UnixStream::connect(sock))
        .await
        .map_err(|_| "signer connect failed: Timeout".to_string())?
        .map_err(|e| format!("signer connect failed: {:?}", e.kind()))?;
    let mut line = Vec::new();
    let io = async {
        s.write_all(b"sign-market-ws\n").await?;
        let mut r = BufReader::new(&mut s);
        r.read_until(b'\n', &mut line).await
    };
    tokio::time::timeout(timeout, io)
        .await
        .map_err(|_| "signer read failed: Timeout".to_string())?
        .map_err(|e| format!("signer read failed: {:?}", e.kind()))?;
    if !line.ends_with(b"\n") {
        return Err("signer reply malformed".into());
    }
    let body = line.strip_suffix(b"\n").unwrap();
    let body = body.strip_suffix(b"\r").unwrap_or(body);
    let parts: Vec<&[u8]> = body.split(|b| *b == b'\t').collect();
    if parts.len() != 3 || parts.iter().any(|p| p.is_empty()) || !parts[1].iter().all(u8::is_ascii_digit) {
        return Err("signer reply malformed".into());
    }
    if !parts.iter().all(|p| p.is_ascii()) {
        return Err("signer reply not ascii".into());
    }
    if parts.iter().any(|p| p.iter().any(|b| !(0x20..0x7f).contains(b))) {
        return Err("signer reply has control characters".into());
    }
    Ok(HEADER_NAMES.iter().zip(parts).map(|(n, p)| (n.to_string(), String::from_utf8(p.to_vec()).expect("ascii"))).collect())
}

#[derive(Debug, Clone)]
pub struct Sub {
    pub rid: String,
    pub kind: &'static str,
    pub horizon: String,
    pub start: i64,
    pub end: i64,
    pub slugs: Vec<String>,
    pub sent: f64,
    pub seen: HashSet<String>,
}

type Key = (&'static str, String, i64);

impl Sub {
    fn key(&self) -> Key {
        (self.kind, self.horizon.clone(), self.start)
    }
}

pub struct PmusMarketsWs {
    pub url: String,
    pub socket_path: String,
    pub assets: Vec<String>,
    pub horizons: Vec<String>,
    pub clock: Box<dyn Fn() -> f64 + Send>,
    pub linger_s: f64,
    pub nodata_s: f64,
    pub signer_timeout: Duration,
    pub subs: BTreeMap<String, Sub>,
    pub retry_at: HashMap<Key, f64>,
    nodata_n: HashMap<(String, i64), u32>,
    n: u64,
    secrets: VecDeque<String>,
    stats_init: bool,
}

impl PmusMarketsWs {
    pub fn new(horizons: &[&str]) -> Self {
        Self {
            url: PMUS_WS_URL.into(),
            socket_path: SIGNER_SOCK.into(),
            assets: ASSETS.iter().map(|s| s.to_string()).collect(),
            horizons: horizons.iter().map(|s| s.to_string()).collect(),
            clock: Box::new(wall_s),
            linger_s: 30.0,
            nodata_s: 20.0,
            signer_timeout: Duration::from_secs(2),
            subs: BTreeMap::new(),
            retry_at: HashMap::new(),
            nodata_n: HashMap::new(),
            n: 0,
            secrets: VecDeque::new(),
            stats_init: false,
        }
    }

    fn init_stats(&mut self, rec: &Recorder) {
        if !self.stats_init {
            for k in ["heartbeats", "sub_errors", "resubscribes", "subscribed"] {
                rec.stats.set_extra(k, 0);
            }
            self.stats_init = true;
        }
    }

    fn count(&self, rec: &Recorder) {
        rec.stats.set_extra("subscribed", self.subs.len());
    }

    fn desired(&self, now: f64) -> Vec<(String, i64, i64)> {
        let mut out = Vec::new();
        for h in &self.horizons {
            let l = horizon_s(h);
            for st in window_starts(now, l, 1) {
                out.push((h.clone(), st, st + l));
            }
        }
        out
    }

    fn subscribe(&mut self, rec: &Recorder, out: &mut Out, kind: &'static str, h: &str, st: i64, end: i64) {
        self.n += 1;
        let rid = format!("{kind}-{h}-{st}-{}", self.n);
        let slugs: Vec<String> = self.assets.iter().map(|a| pmus_slug(a, h, st)).collect();
        out.push(json!({"subscribe": {"requestId": rid, "subscriptionType": sub_type(kind), "marketSlugs": slugs}}).to_string());
        let sub = Sub {
            rid: rid.clone(),
            kind,
            horizon: h.to_string(),
            start: st,
            end,
            slugs: slugs.clone(),
            sent: (self.clock)(),
            seen: HashSet::new(),
        };
        self.retry_at.remove(&sub.key());
        self.subs.insert(rid.clone(), sub);
        rec.ctl("subscribe", json!({"rid": rid, "type": sub_type(kind), "slugs": slugs}));
    }

    fn unsubscribe(&mut self, rec: &Recorder, out: &mut Out, rid: &str, why: &str) {
        self.subs.remove(rid);
        out.push(json!({"unsubscribe": {"request_id": rid}}).to_string());
        rec.ctl("unsubscribe", json!({"rid": rid, "why": why}));
    }

    pub fn sync(&mut self, rec: &Recorder, out: &mut Out) {
        let now = (self.clock)();
        let expired: Vec<_> = self
            .subs
            .values()
            .filter(|s| now >= s.end as f64 + self.linger_s)
            .map(|s| (s.rid.clone(), s.horizon.clone(), s.start))
            .collect();
        for (rid, h, st) in expired {
            self.unsubscribe(rec, out, &rid, "expired");
            self.nodata_n.remove(&(h, st));
        }
        // a started window whose book request is missing slugs: re-send the whole group
        let md: Vec<Sub> = self.subs.values().filter(|s| s.kind == "md").cloned().collect();
        for sub in md {
            if !self.subs.contains_key(&sub.rid) || now < sub.start as f64 || now >= sub.end as f64 {
                continue;
            }
            let mut missing: Vec<_> = sub.slugs.iter().filter(|s| !sub.seen.contains(*s)).cloned().collect();
            missing.sort();
            let g = (sub.horizon.clone(), sub.start);
            let wait = (self.nodata_s * 2f64.powi(*self.nodata_n.get(&g).unwrap_or(&0) as i32)).min(120.0);
            let since = (now - sub.sent).min(now - sub.start as f64);
            if !missing.is_empty() && since >= wait {
                *self.nodata_n.entry(g.clone()).or_insert(0) += 1;
                rec.stats.incr("resubscribes");
                rec.ctl("no_data", json!({"rid": sub.rid, "missing": missing, "waited_s": (since * 10.0).round() / 10.0}));
                let group: Vec<_> = self.subs.values().filter(|s| (s.horizon.clone(), s.start) == g).map(|s| s.rid.clone()).collect();
                for rid in group {
                    self.unsubscribe(rec, out, &rid, "no_data");
                }
            }
        }
        let active: HashSet<Key> = self.subs.values().map(Sub::key).collect();
        for (h, st, end) in self.desired(now) {
            for kind in KINDS {
                let key = (kind, h.clone(), st);
                if active.contains(&key) || self.retry_at.get(&key).copied().unwrap_or(0.0) > now {
                    continue;
                }
                self.subscribe(rec, out, kind, &h, st, end);
            }
        }
        self.count(rec);
    }

    /// Bookkeeping for one recorded frame (Python `_track`).
    pub fn track(&mut self, raw: &str, rec: &Recorder) {
        let Ok(Value::Object(d)) = serde_json::from_str::<Value>(raw) else { return };
        let rid = d.get("requestId").or_else(|| d.get("request_id")).cloned().unwrap_or(Value::Null);
        if let Some(err) = d.get("error") {
            rec.stats.incr("sub_errors");
            let sub = rid.as_str().and_then(|r| self.subs.remove(r));
            let es = match err {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            let es: String = es.chars().take(300).collect();
            rec.ctl("sub_error", json!({"rid": rid, "error": es}));
            if let Some(sub) = sub {
                let now = (self.clock)();
                let st = sub.start as f64;
                let delay = if now >= st { 5.0 } else { (st - now + 3.0).clamp(1.0, 30.0) };
                self.retry_at.insert(sub.key(), now + delay);
            }
            self.count(rec);
            return;
        }
        let (kind, body) = if let Some(b) = d.get("marketData").or_else(|| d.get("marketDataLite")) {
            ("md", b)
        } else if let Some(b) = d.get("trade") {
            ("tr", b)
        } else {
            return;
        };
        let Some(slug) = body.get("marketSlug").and_then(Value::as_str) else { return };
        for sub in self.subs.values_mut() {
            if sub.kind == kind && sub.slugs.iter().any(|s| s == slug) {
                sub.seen.insert(slug.to_string());
            }
        }
    }
}

fn pmus_classify(raw: &str) -> Frame {
    if raw.len() > 64 {
        return Frame::Data;
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(o)) if o.len() == 1 && o.contains_key("heartbeat") => Frame::Heartbeat,
        _ => Frame::Data,
    }
}

#[async_trait]
impl WsVenue for PmusMarketsWs {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        30.0
    }
    fn wants_feedback(&self) -> bool {
        true
    }
    fn classifier(&self) -> fn(&str) -> Frame {
        pmus_classify
    }
    async fn prepare(&mut self, rec: &Recorder) -> bool {
        self.init_stats(rec);
        true
    }
    async fn connect_headers(&mut self) -> Result<Vec<(String, String)>, String> {
        let h = fetch_signed_headers(&self.socket_path, self.signer_timeout).await?;
        for (_, v) in &h {
            self.secrets.push_back(v.clone());
            while self.secrets.len() > 12 {
                self.secrets.pop_front();
            }
        }
        Ok(h)
    }
    fn redact(&self, text: &str) -> String {
        let mut t = text.to_string();
        for s in &self.secrets {
            if !s.is_empty() {
                t = t.replace(s.as_str(), REDACTED);
            }
        }
        t
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.init_stats(rec);
        self.subs.clear();
        self.retry_at.clear();
        self.nodata_n.clear();
        self.sync(rec, out);
        Ok(())
    }
    async fn on_tick(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.sync(rec, out);
        Ok(())
    }
    fn on_feedback(&mut self, raw: &str, rec: &Recorder, _out: &mut Out) {
        self.track(raw, rec);
    }
}
