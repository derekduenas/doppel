//! Kalshi: request signing, the BRTI reference poll (`ref_brti_live`), public REST books
//! (`kalshi_books`) and the market-data WebSocket (`kalshi_ws`, new in the Rust port).
//!
//! The credential `{"key_id", "private_key_pem"}` comes from `$CREDENTIALS_DIRECTORY/kalshi-profile.json`
//! (systemd `LoadCredential`). Signature: base64(sign(timestamp_ms + "GET" + path)), Ed25519 or
//! RSA-PSS/SHA-256 with a 32-byte salt. Errors on signed paths carry only an error kind, never a
//! server body or key material.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use base64::Engine;
use futures_util::future::join_all;
use serde_json::{json, Value};
use tokio::sync::watch;

use crate::clock::{mono_ns, wall_ns};
use crate::http::{err_kind, RateLimiter};
use crate::recorder::Recorder;
use crate::universe::{KalshiUniverse, KALSHI_REST};
use crate::ws::{sleep_or_stop, Out, WsVenue};

pub const CRED_NAME: &str = "kalshi-profile.json";
pub const BRTI_URL: &str = "https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI";
pub const KALSHI_WS_URL: &str = "wss://api.elections.kalshi.com/trade-api/ws/v2";

pub enum SigningKey {
    Ed25519(Box<ed25519_dalek::SigningKey>),
    Rsa(Box<rsa::RsaPrivateKey>),
}

impl SigningKey {
    /// PKCS#8 (Ed25519 or RSA) or PKCS#1 RSA PEM.
    pub fn from_pem(pem: &str) -> Result<Self, &'static str> {
        use ed25519_dalek::pkcs8::DecodePrivateKey as _;
        if let Ok(k) = ed25519_dalek::SigningKey::from_pkcs8_pem(pem) {
            return Ok(Self::Ed25519(Box::new(k)));
        }
        if let Ok(k) = <rsa::RsaPrivateKey as rsa::pkcs8::DecodePrivateKey>::from_pkcs8_pem(pem) {
            return Ok(Self::Rsa(Box::new(k)));
        }
        if let Ok(k) = <rsa::RsaPrivateKey as rsa::pkcs1::DecodeRsaPrivateKey>::from_pkcs1_pem(pem) {
            return Ok(Self::Rsa(Box::new(k)));
        }
        Err("unsupported key")
    }

    /// base64(sign(ts_ms + method + path)).
    pub fn sign(&self, ts_ms: &str, method: &str, path: &str) -> String {
        let msg = format!("{ts_ms}{method}{path}");
        let sig: Vec<u8> = match self {
            Self::Ed25519(k) => {
                use ed25519_dalek::Signer as _;
                k.sign(msg.as_bytes()).to_bytes().to_vec()
            }
            Self::Rsa(k) => {
                use rsa::signature::{RandomizedSigner as _, SignatureEncoding as _};
                // salt length = digest length = 32 bytes
                let sk = rsa::pss::SigningKey::<sha2::Sha256>::new((**k).clone());
                sk.sign_with_rng(&mut rand::thread_rng(), msg.as_bytes()).to_vec()
            }
        };
        base64::engine::general_purpose::STANDARD.encode(sig)
    }
}

pub struct Credential {
    pub key_id: String,
    pub key: SigningKey,
}

impl Credential {
    pub fn headers(&self, path: &str) -> Vec<(String, String)> {
        let ts = (wall_ns() / 1_000_000).to_string();
        let sig = self.key.sign(&ts, "GET", path);
        vec![
            ("KALSHI-ACCESS-KEY".into(), self.key_id.clone()),
            ("KALSHI-ACCESS-TIMESTAMP".into(), ts),
            ("KALSHI-ACCESS-SIGNATURE".into(), sig),
        ]
    }
}

pub fn default_profile_path() -> Option<PathBuf> {
    std::env::var_os("CREDENTIALS_DIRECTORY").map(|d| PathBuf::from(d).join(CRED_NAME))
}

/// Load the profile, or record why not (`ctl: idle`) and return None. Never prints key material.
pub fn load_credential(path: Option<PathBuf>, rec: &Recorder) -> Option<Credential> {
    let p = path.or_else(default_profile_path);
    let Some(p) = p.filter(|p| p.is_file()) else {
        eprintln!("doppel-taped: {}: no credential file; idling", rec.name());
        rec.ctl("idle", json!({"why": "no_credentials"}));
        rec.stats.set_extra("idle", "no_credentials");
        return None;
    };
    let parsed = (|| -> Result<Credential, &'static str> {
        let b = std::fs::read(&p).map_err(|_| "Read")?;
        let v: Value = serde_json::from_slice(&b).map_err(|_| "JSON")?;
        let key_id = v.get("key_id").and_then(Value::as_str).filter(|s| !s.is_empty()).ok_or("key_id")?;
        let pem = v.get("private_key_pem").and_then(Value::as_str).ok_or("private_key_pem")?;
        Ok(Credential { key_id: key_id.to_string(), key: SigningKey::from_pem(pem).map_err(|_| "Key")? })
    })();
    match parsed {
        Ok(c) => Some(c),
        Err(kind) => {
            eprintln!("doppel-taped: {}: credential file unusable ({kind}); idling", rec.name());
            rec.ctl("idle", json!({"why": "bad_credentials", "err": kind}));
            rec.stats.set_extra("idle", "bad_credentials");
            None
        }
    }
}

fn url_path(url: &str) -> String {
    let rest = url.split_once("://").map(|x| x.1).unwrap_or(url);
    let path = rest.find('/').map(|i| &rest[i..]).unwrap_or("/");
    path.split('?').next().unwrap_or("/").to_string()
}

async fn idle(stop: &mut watch::Receiver<bool>) {
    while !*stop.borrow() {
        if stop.changed().await.is_err() {
            break;
        }
    }
}

// ------------------------------------------------------------------------------------------------
// BRTI

pub struct KalshiBrti {
    pub url: String,
    pub interval: Duration,
    pub profile_path: Option<PathBuf>,
    pub seen_cap: usize,
    seen: HashSet<(i64, String)>,
    order: VecDeque<(i64, String)>,
}

impl KalshiBrti {
    pub fn new() -> Self {
        Self {
            url: BRTI_URL.into(),
            interval: Duration::from_secs(5),
            profile_path: None,
            seen_cap: 20_000,
            seen: HashSet::new(),
            order: VecDeque::new(),
        }
    }

    /// `[time_ms, value]` pairs not seen in any earlier body, time-ascending.
    pub fn extract_new(&mut self, body: &str) -> Vec<Value> {
        let Ok(mut d) = serde_json::from_str::<Value>(body) else { return vec![] };
        if d.get("data").is_some_and(Value::is_object) {
            d = d["data"].take();
        }
        let Some(payload) = d.get("payload").and_then(Value::as_array) else { return vec![] };
        let mut pairs: Vec<(i64, String)> = payload
            .iter()
            .filter_map(|p| {
                let t = match p.get("time")? {
                    Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64))?,
                    Value::String(s) => s.trim().parse().ok()?,
                    _ => return None,
                };
                let v = match p.get("value")? {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
                Some((t, v))
            })
            .collect();
        pairs.sort();
        let mut new = Vec::new();
        for k in pairs {
            if self.seen.insert(k.clone()) {
                self.order.push_back(k.clone());
                new.push(json!([k.0, k.1]));
            }
        }
        while self.order.len() > self.seen_cap {
            if let Some(old) = self.order.pop_front() {
                self.seen.remove(&old);
            }
        }
        new
    }

    pub async fn run(mut self, rec: Recorder, mut stop: watch::Receiver<bool>) {
        let Some(cred) = load_credential(self.profile_path.clone(), &rec) else {
            return idle(&mut stop).await;
        };
        let client = crate::http::signed_client(5);
        let path = url_path(&self.url);
        rec.stats.set_connected(true);
        while !*stop.borrow() {
            let cycle = Instant::now();
            let wait = self.poll(&client, &cred, &path, &rec).await;
            if let Some(w) = wait {
                sleep_or_stop(w, &mut stop).await;
            }
            sleep_or_stop(self.interval.saturating_sub(cycle.elapsed()), &mut stop).await;
        }
        rec.stats.set_connected(false);
    }

    /// One signed poll. Returns an extra back-off when rate limited.
    pub async fn poll(&mut self, client: &reqwest::Client, cred: &Credential, path: &str, rec: &Recorder) -> Option<Duration> {
        let (sent_w, sent_m) = (wall_ns(), mono_ns());
        let mut req = client.get(&self.url);
        for (k, v) in cred.headers(path) {
            req = req.header(k, v);
        }
        let resp = match req.send().await {
            Ok(r) => r,
            Err(e) => {
                rec.stats.error(format!("poll: {}", err_kind(&e)));
                rec.ctl("poll_error", json!({"err": err_kind(&e)}));
                return None;
            }
        };
        let status = resp.status().as_u16();
        let retry_after = resp.headers().get("retry-after").and_then(|v| v.to_str().ok()).map(str::to_string);
        let body = match resp.text().await {
            Ok(b) => b,
            Err(e) => {
                rec.stats.error(format!("poll: {}", err_kind(&e)));
                rec.ctl("poll_error", json!({"err": err_kind(&e)}));
                return None;
            }
        };
        let new = if status == 200 { self.extract_new(&body) } else { vec![] };
        rec.record_at(body, wall_ns(), mono_ns(), Some(json!({"status": status, "sent_tw": sent_w, "sent_tm": sent_m, "new": new})));
        if status != 200 {
            rec.stats.incr(&format!("http_{status}"));
        }
        if status == 429 {
            // Shared key: back off so other users of the key are never starved.
            let wait = retry_after.and_then(|s| s.trim().parse::<f64>().ok()).map(|w| w.clamp(2.0, 30.0)).unwrap_or(10.0);
            rec.ctl("rate_limited", json!({"wait_s": wait}));
            return Some(Duration::from_secs_f64(wait));
        }
        None
    }
}

impl Default for KalshiBrti {
    fn default() -> Self {
        Self::new()
    }
}

// ------------------------------------------------------------------------------------------------
// public REST books (kept until kalshi_ws is proven)

pub struct KalshiBooks {
    pub base: String,
    pub interval: Duration,
    pub universe: KalshiUniverse,
    pub limiter: Arc<RateLimiter>,
}

impl KalshiBooks {
    pub fn new(limiter: Arc<RateLimiter>) -> Self {
        Self { base: KALSHI_REST.into(), interval: Duration::from_secs(1), universe: KalshiUniverse::new(KALSHI_REST), limiter }
    }

    pub async fn run(mut self, rec: Recorder, mut stop: watch::Receiver<bool>) {
        let client = crate::http::client(10);
        rec.stats.set_connected(true);
        while !*stop.borrow() {
            let cycle = Instant::now();
            let tickers = match self.universe.tickers(&client).await {
                Ok(t) => t,
                Err(e) => {
                    rec.stats.error(format!("targets: {e}"));
                    vec![]
                }
            };
            rec.stats.set_extra("targets", tickers.len());
            let base = self.base.clone();
            join_all(tickers.iter().map(|t| poll_book(&client, &self.limiter, &rec, t.clone(), format!("{base}/markets/{t}/orderbook"))))
                .await;
            sleep_or_stop(self.interval.saturating_sub(cycle.elapsed()), &mut stop).await;
        }
        rec.stats.set_connected(false);
    }
}

async fn poll_book(client: &reqwest::Client, lim: &RateLimiter, rec: &Recorder, key: String, url: String) {
    lim.take().await;
    let (sent_w, sent_m) = (wall_ns(), mono_ns());
    let res = async {
        let r = client.get(&url).send().await?;
        let status = r.status().as_u16();
        Ok::<_, reqwest::Error>((status, r.text().await?))
    }
    .await;
    let (status, body) = match res {
        Ok(x) => x,
        Err(e) => {
            let err = format!("{key}: {}", err_kind(&e));
            rec.stats.error(err.clone());
            rec.ctl("poll_error", json!({"key": key, "err": err}));
            return;
        }
    };
    if status == 429 {
        rec.stats.incr("http_429");
        rec.ctl("rate_limited", json!({"key": key}));
        tokio::time::sleep(Duration::from_secs(1)).await;
        return;
    }
    rec.record_at(
        body,
        wall_ns(),
        mono_ns(),
        Some(json!({"key": key, "url": url, "status": status, "sent_tw": sent_w, "sent_tm": sent_m})),
    );
}

// ------------------------------------------------------------------------------------------------
// market-data WebSocket

/// Kalshi market-data WS: one subscription per channel per connection (`orderbook_delta`, `trade`)
/// over the nearest open 15m crypto markets. New tickers are added with `update_subscription
/// add_markets`, closed ones removed with `delete_markets`. `seq` is per subscription id (sid);
/// any gap is recorded (`ctl: seq_gap`) and both subscriptions are dropped and re-made, which
/// brings a fresh snapshot.
pub struct KalshiWs {
    pub url: String,
    pub rest_base: String,
    pub profile_path: Option<PathBuf>,
    pub universe: KalshiUniverse,
    pub resub_timeout: Duration,
    cred: Option<Credential>,
    client: reqwest::Client,
    next_id: u64,
    /// tickers covered by the live subscription
    pub tickers: BTreeSet<String>,
    /// sid -> channel
    pub sids: BTreeMap<u64, String>,
    last_seq: HashMap<u64, u64>,
    sub_id: Option<u64>,
    resub_since: Option<Instant>,
    retry_at: Option<Instant>,
    reconnect: Option<String>,
}

pub const CHANNELS: [&str; 2] = ["orderbook_delta", "trade"];

impl KalshiWs {
    pub fn new() -> Self {
        Self {
            url: KALSHI_WS_URL.into(),
            rest_base: KALSHI_REST.into(),
            profile_path: None,
            universe: KalshiUniverse::new(KALSHI_REST),
            resub_timeout: Duration::from_secs(5),
            cred: None,
            client: crate::http::client(10),
            next_id: 0,
            tickers: BTreeSet::new(),
            sids: BTreeMap::new(),
            last_seq: HashMap::new(),
            sub_id: None,
            resub_since: None,
            retry_at: None,
            reconnect: None,
        }
    }

    fn id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    fn subscribe(&mut self, rec: &Recorder, out: &mut Out, want: &BTreeSet<String>) {
        let id = self.id();
        let tickers: Vec<_> = want.iter().cloned().collect();
        out.push(json!({"id": id, "cmd": "subscribe", "params": {"channels": CHANNELS, "market_tickers": tickers}}).to_string());
        rec.ctl("subscribe", json!({"id": id, "channels": CHANNELS, "tickers": tickers}));
        self.sub_id = Some(id);
        self.tickers = want.clone();
        self.sids.clear();
        self.last_seq.clear();
        self.retry_at = None;
    }

    fn resubscribe(&mut self, rec: &Recorder, out: &mut Out, why: &str) {
        if self.resub_since.is_some() {
            return;
        }
        rec.stats.incr("resubscribes");
        let sids: Vec<u64> = self.sids.keys().copied().collect();
        rec.ctl("resubscribe", json!({"why": why, "sids": sids}));
        if sids.is_empty() {
            let want = self.tickers.clone();
            self.subscribe(rec, out, &want);
            return;
        }
        let id = self.id();
        out.push(json!({"id": id, "cmd": "unsubscribe", "params": {"sids": sids}}).to_string());
        self.last_seq.clear();
        self.resub_since = Some(Instant::now());
    }

    /// Apply a fresh ticker set to the live subscription.
    pub fn update_tickers(&mut self, rec: &Recorder, out: &mut Out, want: BTreeSet<String>) {
        if self.resub_since.is_some() {
            self.tickers = want; // applied by the re-subscribe
            return;
        }
        if self.sub_id.is_none() {
            if !want.is_empty() && self.retry_at.is_none_or(|t| Instant::now() >= t) {
                self.subscribe(rec, out, &want);
            }
            return;
        }
        if self.sids.len() < CHANNELS.len() {
            return; // wait for both subscribed acks
        }
        let added: Vec<_> = want.difference(&self.tickers).cloned().collect();
        let removed: Vec<_> = self.tickers.difference(&want).cloned().collect();
        let sids: Vec<u64> = self.sids.keys().copied().collect();
        if !added.is_empty() {
            for sid in &sids {
                let id = self.id();
                out.push(json!({"id": id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": added, "action": "add_markets"}}).to_string());
            }
            rec.ctl("subscribe_add", json!({"tickers": added}));
        }
        if !removed.is_empty() {
            for sid in &sids {
                let id = self.id();
                out.push(json!({"id": id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": removed, "action": "delete_markets"}}).to_string());
            }
            rec.ctl("unsubscribe", json!({"tickers": removed}));
        }
        self.tickers = want;
        rec.stats.set_extra("subscribed", self.tickers.len());
    }

    pub fn handle(&mut self, raw: &str, rec: &Recorder, out: &mut Out) {
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
                rec.stats.set_extra("subscribed", self.tickers.len());
                return;
            }
            "unsubscribed" => {
                if let Some(sid) = d.get("sid").or_else(|| msg.and_then(|m| m.get("sid"))).and_then(Value::as_u64) {
                    self.sids.remove(&sid);
                    self.last_seq.remove(&sid);
                }
                if self.resub_since.is_some() && self.sids.is_empty() {
                    self.resub_since = None;
                    let want = self.tickers.clone();
                    if !want.is_empty() {
                        self.subscribe(rec, out, &want);
                    } else {
                        self.sub_id = None;
                    }
                }
                return;
            }
            "error" => {
                rec.stats.incr("sub_errors");
                let id = d.get("id").cloned().unwrap_or(Value::Null);
                let code = msg.and_then(|m| m.get("code")).cloned().unwrap_or(Value::Null);
                let text: String = msg.and_then(|m| m.get("msg")).and_then(Value::as_str).unwrap_or("").chars().take(300).collect();
                rec.ctl("sub_error", json!({"id": id, "code": code, "error": text}));
                if id.as_u64().is_some() && id.as_u64() == self.sub_id && self.sids.is_empty() {
                    self.sub_id = None; // the subscribe itself failed: retry later
                    self.retry_at = Some(Instant::now() + Duration::from_secs(5));
                }
                if let Some(sid) = d.get("sid").and_then(Value::as_u64) {
                    // a running subscription was ended by the server (codes 10/25)
                    self.sids.remove(&sid);
                    self.resubscribe(rec, out, "server_error");
                }
                return;
            }
            _ => {}
        }
        let (Some(sid), Some(seq)) = (d.get("sid").and_then(Value::as_u64), d.get("seq").and_then(Value::as_u64)) else { return };
        if !self.sids.contains_key(&sid) {
            return; // late frame of a subscription being torn down
        }
        if let Some(prev) = self.last_seq.insert(sid, seq) {
            if seq != prev + 1 {
                rec.stats.incr("seq_gaps");
                let ch = self.sids.get(&sid).cloned().unwrap_or_default();
                rec.ctl("seq_gap", json!({"sid": sid, "channel": ch, "expected": prev + 1, "got": seq}));
                self.resubscribe(rec, out, "seq_gap");
            }
        }
    }
}

impl Default for KalshiWs {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl WsVenue for KalshiWs {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        30.0
    }
    fn pings_are_liveness(&self) -> bool {
        true // Kalshi pings only after 10 s without data
    }
    fn expect_traffic(&self) -> bool {
        !self.tickers.is_empty()
    }
    fn wants_feedback(&self) -> bool {
        true
    }
    async fn prepare(&mut self, rec: &Recorder) -> bool {
        for k in ["sub_errors", "seq_gaps", "resubscribes", "subscribed"] {
            rec.stats.set_extra(k, 0);
        }
        self.cred = load_credential(self.profile_path.clone(), rec);
        self.cred.is_some()
    }
    async fn connect_headers(&mut self) -> Result<Vec<(String, String)>, String> {
        let c = self.cred.as_ref().ok_or("no credential")?;
        Ok(c.headers(&url_path(&self.url)))
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.tickers.clear();
        self.sids.clear();
        self.last_seq.clear();
        self.sub_id = None;
        self.resub_since = None;
        self.retry_at = None;
        self.reconnect = None;
        let want: BTreeSet<String> = match self.universe.tickers(&self.client).await {
            Ok(t) => t.into_iter().collect(),
            Err(e) => {
                rec.stats.error(format!("universe: {e}"));
                self.universe.cached().iter().cloned().collect()
            }
        };
        self.update_tickers(rec, out, want);
        Ok(())
    }
    async fn on_tick(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        if let Some(t) = self.resub_since {
            if t.elapsed() >= self.resub_timeout {
                self.reconnect = Some("resubscribe_timeout".into());
                return Ok(());
            }
        }
        if !self.universe.due() && (self.sub_id.is_some() || self.retry_at.is_some_and(|t| Instant::now() < t)) {
            return Ok(());
        }
        let want: BTreeSet<String> = self.universe.tickers(&self.client).await.map_err(|e| format!("universe: {e}"))?.into_iter().collect();
        self.update_tickers(rec, out, want);
        Ok(())
    }
    fn on_feedback(&mut self, raw: &str, rec: &Recorder, out: &mut Out) {
        self.handle(raw, rec, out);
    }
    fn on_feedback_overflow(&mut self, _rec: &Recorder) {
        // frames were recorded but not sequence-checked: start clean
        self.reconnect = Some("feedback_overflow".into());
    }
    fn take_reconnect(&mut self) -> Option<String> {
        self.reconnect.take()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_paths() {
        assert_eq!(url_path(BRTI_URL), "/trade-api/v2/cfbenchmarks/values");
        assert_eq!(url_path(KALSHI_WS_URL), "/trade-api/ws/v2");
        assert_eq!(url_path("http://127.0.0.1:9/x?y=1"), "/x");
    }

    #[test]
    fn extract_new_dedupes() {
        let mut b = KalshiBrti::new();
        b.seen_cap = 3;
        let body = r#"{"data":{"payload":[{"time":2,"value":"10.5"},{"time":1,"value":"10.0"}]}}"#;
        assert_eq!(b.extract_new(body), vec![json!([1, "10.0"]), json!([2, "10.5"])]);
        let body2 = r#"{"payload":[{"time":2,"value":"10.5"},{"time":3,"value":11},{"time":"x"}]}"#;
        assert_eq!(b.extract_new(body2), vec![json!([3, "11"])]);
        assert_eq!(b.extract_new("not json"), Vec::<Value>::new());
    }
}
