//! Local mock tests: WebSocket watchdog, PM US signer headers per connect and no redirects,
//! PM US rollover, Kalshi signing (generated keys), BRTI signed poll + 429, Kalshi WS seq gap,
//! spot L2 seq gap and tape decode, LMAX REST poll back-off, perps (Kalshi margin WS signed + seq
//! gap, idle without a credential, Coinbase Derivatives subscriptions, public REST poll).
//! No test touches a real venue.

// The handshake callback signature (Result<Response, ErrorResponse>) is fixed by tungstenite.
#![allow(clippy::result_large_err)]

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use base64::Engine;
use doppel_tape::kalshi::{KalshiBrti, KalshiWs, SigningKey};
use doppel_tape::pmus::{fetch_signed_headers, PmusMarketsWs};
use doppel_tape::recorder::{spawn_writer, Recorder, WriterThread};
use doppel_tape::segment::read_segment;
use doppel_tape::universe::pmus_slug;
use doppel_tape::ws::{run_ws, Out, WsOpts, WsVenue};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, UnixListener};
use tokio::sync::watch;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::Message;

const T0: f64 = 1_795_622_400.0; // 2026-11-25 16:00:00Z

fn setup() -> tempfile::TempDir {
    doppel_tape::install_crypto();
    tempfile::tempdir().unwrap()
}

fn writer(root: &Path, name: &str) -> (Recorder, WriterThread) {
    spawn_writer(root.to_path_buf(), name, 3, 1024).unwrap()
}

fn tape(root: &Path, stream: &str) -> Vec<Value> {
    let mut files = Vec::new();
    for day in std::fs::read_dir(root.join(stream)).unwrap() {
        let p = day.unwrap().path();
        if p.is_dir() {
            for f in std::fs::read_dir(&p).unwrap() {
                files.push(f.unwrap().path());
            }
        }
    }
    files.sort();
    files.iter().flat_map(|f| read_segment(f, true).unwrap()).collect()
}

fn ctls(recs: &[Value]) -> Vec<String> {
    recs.iter().filter_map(|r| r.get("meta").and_then(|m| m.get("ctl")).and_then(Value::as_str).map(str::to_string)).collect()
}

fn fast() -> WsOpts {
    WsOpts { connect_timeout: Duration::from_secs(3), ping_every: Duration::from_secs(20), backoff_max_s: 0.3 }
}

/// Short Unix socket path (AF_UNIX paths are capped near 104 bytes on macOS).
fn sock_path() -> (tempfile::TempDir, PathBuf) {
    let d = tempfile::Builder::new().prefix("dp").tempdir_in("/tmp").unwrap();
    let p = d.path().join("m.sock");
    (d, p)
}

// ------------------------------------------------------------------------------------------------
// watchdog

struct Silent {
    url: String,
}

#[async_trait]
impl WsVenue for Silent {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        1.5
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        out.push("{\"subscribe\":1}".into());
        rec.ctl("subscribe", json!({}));
        Ok(())
    }
}

#[tokio::test]
async fn watchdog_reconnects_on_silent_stall_even_with_pings() {
    let d = setup();
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", l.local_addr().unwrap());
    let conns = Arc::new(Mutex::new(0));
    let c2 = conns.clone();
    tokio::spawn(async move {
        while let Ok((s, _)) = l.accept().await {
            *c2.lock().unwrap() += 1;
            tokio::spawn(async move {
                let mut ws = tokio_tungstenite::accept_async(s).await.unwrap();
                let _ = ws.next().await; // the subscribe
                let _ = ws.send(Message::Text("hello".into())).await;
                // then: protocol pings only, no data (the RTDS silent stall)
                for _ in 0..40 {
                    if ws.send(Message::Ping(vec![1].into())).await.is_err() {
                        return;
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            });
        }
    });
    let (rec, wt) = writer(d.path(), "w");
    let stats = rec.stats.clone();
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(Silent { url }, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(4200)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();
    let recs = tape(d.path(), "w");
    let c = ctls(&recs);
    assert!(stats.stalls.load(std::sync::atomic::Ordering::Relaxed) >= 1, "{c:?}");
    assert!(*conns.lock().unwrap() >= 2);
    assert_eq!(&c[..4], ["connect", "subscribe", "stall", "disconnect"]);
    let data: Vec<_> = recs.iter().filter(|r| r.get("meta").is_none()).map(|r| r["raw"].as_str().unwrap()).collect();
    assert!(data.iter().all(|r| *r == "hello") && !data.is_empty());
    let stall = recs.iter().find(|r| r["meta"]["ctl"] == "stall").unwrap();
    assert!(stall["meta"]["silent_s"].as_f64().unwrap() > 1.5);
}

// ------------------------------------------------------------------------------------------------
// PM US signer

struct FakeSigner {
    issued: Arc<Mutex<Vec<[String; 3]>>>,
}

impl FakeSigner {
    fn start(path: &Path) -> Self {
        let l = UnixListener::bind(path).unwrap();
        let issued: Arc<Mutex<Vec<[String; 3]>>> = Arc::default();
        let i2 = issued.clone();
        tokio::spawn(async move {
            while let Ok((mut s, _)) = l.accept().await {
                let mut line = String::new();
                let mut r = BufReader::new(&mut s);
                r.read_line(&mut line).await.unwrap();
                assert_eq!(line, "sign-market-ws\n");
                let n = i2.lock().unwrap().len() + 1;
                let trip = [format!("AKEYsecret{n:04}"), format!("{}", 1_795_622_400_000u64 + n as u64), format!("SIGsecret{n:04}+/=")];
                s.write_all(format!("{}\n", trip.join("\t")).as_bytes()).await.unwrap();
                i2.lock().unwrap().push(trip);
            }
        });
        Self { issued }
    }
}

#[tokio::test]
async fn signer_protocol_and_errors() {
    let (_d, p) = sock_path();
    let s = FakeSigner::start(&p);
    let h = fetch_signed_headers(p.to_str().unwrap(), Duration::from_secs(2)).await.unwrap();
    assert_eq!(h[0], ("X-PM-Access-Key".to_string(), "AKEYsecret0001".to_string()));
    assert_eq!(h[2].0, "X-PM-Signature");
    assert_eq!(s.issued.lock().unwrap().len(), 1);
    let e = fetch_signed_headers("/nonexistent/x.sock", Duration::from_secs(1)).await.unwrap_err();
    assert!(e.starts_with("signer connect failed"), "{e}");
    // malformed replies never leak their content
    let (_d2, p2) = sock_path();
    let l = UnixListener::bind(&p2).unwrap();
    tokio::spawn(async move {
        while let Ok((mut s, _)) = l.accept().await {
            let mut b = [0u8; 64];
            let _ = s.read(&mut b).await;
            let _ = s.write_all(b"leakme\tnotdigits\tsig\n").await;
        }
    });
    let e = fetch_signed_headers(p2.to_str().unwrap(), Duration::from_secs(1)).await.unwrap_err();
    assert_eq!(e, "signer reply malformed");
}

#[tokio::test]
async fn pmus_headers_fresh_per_connect_heartbeats_not_recorded() {
    let d = setup();
    let (_sd, sp) = sock_path();
    let signer = FakeSigner::start(&sp);
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", l.local_addr().unwrap());
    let seen: Arc<Mutex<Vec<[String; 3]>>> = Arc::default();
    let subs: Arc<Mutex<Vec<Value>>> = Arc::default();
    let (seen2, subs2) = (seen.clone(), subs.clone());
    tokio::spawn(async move {
        while let Ok((s, _)) = l.accept().await {
            let (seen, subs) = (seen2.clone(), subs2.clone());
            tokio::spawn(async move {
                let cb = |req: &Request, resp: Response| {
                    let h = |n: &str| req.headers().get(n).map(|v| v.to_str().unwrap().to_string()).unwrap_or_default();
                    seen.lock().unwrap().push([h("x-pm-access-key"), h("x-pm-timestamp"), h("x-pm-signature")]);
                    Ok(resp)
                };
                let mut ws = tokio_tungstenite::accept_hdr_async(s, cb).await.unwrap();
                ws.send(Message::Text("{\"heartbeat\":{}}".into())).await.unwrap();
                // 1 horizon x (current, next) x (md, tr) = 4 subscribe requests
                for _ in 0..4 {
                    if let Some(Ok(Message::Text(t))) = ws.next().await {
                        subs.lock().unwrap().push(serde_json::from_str(t.as_str()).unwrap());
                    }
                }
                ws.send(Message::Text("{\"marketData\":{\"marketSlug\":\"x\"}}".into())).await.unwrap();
                tokio::time::sleep(Duration::from_millis(150)).await;
                let _ = ws.close(None).await; // force a reconnect
            });
        }
    });
    let (rec, wt) = writer(d.path(), "pmus_ws_5m");
    let stats = rec.stats.clone();
    let mut v = PmusMarketsWs::new(&["5m"]);
    v.url = url;
    v.socket_path = sp.to_str().unwrap().to_string();
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(1500)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();
    let seen = seen.lock().unwrap().clone();
    let issued = signer.issued.lock().unwrap().clone();
    assert!(seen.len() >= 2, "reconnected with fresh headers");
    assert_eq!(seen, issued[..seen.len()].to_vec());
    assert_ne!(seen[0], seen[1]);
    let subs = subs.lock().unwrap().clone();
    assert!(subs.iter().all(|s| s["subscribe"]["marketSlugs"].as_array().unwrap().len() == 4));
    let recs = tape(d.path(), "pmus_ws_5m");
    let text = serde_json::to_string(&recs).unwrap();
    for t in &issued {
        assert!(!text.contains(&t[0]) && !text.contains(&t[2]), "secret leaked to tape");
    }
    assert!(!recs.iter().any(|r| r["raw"].as_str().unwrap().contains("heartbeat")));
    assert!(stats.extra("heartbeats").unwrap().as_u64().unwrap() >= 2);
    assert!(stats.as_value().to_string().find("secret").is_none());
}

#[tokio::test]
async fn pmus_refuses_redirect_and_redacts() {
    let d = setup();
    let (_sd, sp) = sock_path();
    let signer = FakeSigner::start(&sp);
    let other = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let other_addr = other.local_addr().unwrap();
    let hits = Arc::new(Mutex::new(0));
    let h2 = hits.clone();
    tokio::spawn(async move {
        while let Ok(_c) = other.accept().await {
            *h2.lock().unwrap() += 1;
        }
    });
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", l.local_addr().unwrap());
    tokio::spawn(async move {
        while let Ok((mut s, _)) = l.accept().await {
            let mut b = vec![0u8; 4096];
            let _ = s.read(&mut b).await;
            let resp = format!("HTTP/1.1 302 Found\r\nLocation: ws://{other_addr}/\r\nContent-Length: 0\r\n\r\n");
            let _ = s.write_all(resp.as_bytes()).await;
        }
    });
    let (rec, wt) = writer(d.path(), "pmus_ws_15m");
    let stats = rec.stats.clone();
    let mut v = PmusMarketsWs::new(&["15m"]);
    v.url = url;
    v.socket_path = sp.to_str().unwrap().to_string();
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(1200)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();
    assert_eq!(*hits.lock().unwrap(), 0, "redirect must not be followed");
    let err = stats.last_error().unwrap();
    assert!(err.contains("302"), "{err}");
    for t in signer.issued.lock().unwrap().iter() {
        assert!(!err.contains(&t[0]) && !err.contains(&t[2]));
    }
    assert!(stats.reconnects.load(std::sync::atomic::Ordering::Relaxed) >= 1);
}

// ------------------------------------------------------------------------------------------------
// PM US rollover with an injected clock

fn pm(now: Arc<Mutex<f64>>, assets: &[&str], nodata_s: f64) -> PmusMarketsWs {
    let mut v = PmusMarketsWs::new(&["5m"]);
    v.assets = assets.iter().map(|s| s.to_string()).collect();
    v.clock = Box::new(move || *now.lock().unwrap());
    v.nodata_s = nodata_s;
    v
}

fn parsed(out: &Out) -> Vec<Value> {
    out.iter().map(|s| serde_json::from_str(s).unwrap()).collect()
}

#[tokio::test]
async fn pmus_rollover() {
    let d = setup();
    let (rec, _wt) = writer(d.path(), "p");
    let now = Arc::new(Mutex::new(T0 + 10.0));
    let mut v = pm(now.clone(), &["btc", "eth"], 1e9);
    let t0 = T0 as i64;
    let mut out = Out::new();
    v.on_open(&rec, &mut out).await.unwrap();
    let subs = parsed(&out);
    assert_eq!(subs.len(), 4);
    for st in [t0, t0 + 300] {
        for t in ["SUBSCRIPTION_TYPE_MARKET_DATA", "SUBSCRIPTION_TYPE_TRADE"] {
            assert!(subs.iter().any(|s| s["subscribe"]["subscriptionType"] == t
                && s["subscribe"]["marketSlugs"] == json!([pmus_slug("btc", "5m", st), pmus_slug("eth", "5m", st)])));
        }
    }
    let first: Vec<String> = subs
        .iter()
        .filter(|s| s["subscribe"]["marketSlugs"][0] == pmus_slug("btc", "5m", t0))
        .map(|s| s["subscribe"]["requestId"].as_str().unwrap().to_string())
        .collect();
    out.clear();
    v.on_tick(&rec, &mut out).await.unwrap();
    assert!(out.is_empty());
    *now.lock().unwrap() = T0 + 301.0;
    v.on_tick(&rec, &mut out).await.unwrap();
    let new = parsed(&out);
    assert_eq!(new.len(), 2);
    assert!(new.iter().all(|m| m["subscribe"]["marketSlugs"][0] == pmus_slug("btc", "5m", t0 + 600)));
    out.clear();
    *now.lock().unwrap() = T0 + 331.0;
    v.on_tick(&rec, &mut out).await.unwrap();
    let mut unsubs: Vec<String> = parsed(&out).iter().map(|m| m["unsubscribe"]["request_id"].as_str().unwrap().to_string()).collect();
    unsubs.sort();
    let mut first = first;
    first.sort();
    assert_eq!(unsubs, first);
    assert_eq!(rec.stats.extra("subscribed").unwrap(), 4);
}

#[tokio::test]
async fn pmus_error_retry_and_nodata() {
    let d = setup();
    let (rec, _wt) = writer(d.path(), "p");
    let now = Arc::new(Mutex::new(T0 + 10.0));
    let mut v = pm(now.clone(), &["btc"], 0.0);
    let t0 = T0 as i64;
    let mut out = Out::new();
    v.on_open(&rec, &mut out).await.unwrap();
    let nxt = v.subs.values().find(|s| s.start == t0 + 300 && s.kind == "md").unwrap().rid.clone();
    v.track(&json!({"request_id": nxt, "error": "market not found"}).to_string(), &rec);
    assert!(!v.subs.contains_key(&nxt));
    assert_eq!(rec.stats.extra("sub_errors").unwrap(), 1);
    out.clear();
    let cur = v.subs.values().find(|s| s.kind == "md" && s.start == t0).unwrap().rid.clone();
    v.track(&json!({"requestId": cur, "marketData": {"marketSlug": pmus_slug("btc", "5m", t0)}}).to_string(), &rec);
    v.on_tick(&rec, &mut out).await.unwrap();
    assert!(out.is_empty(), "{out:?}");
    for t in v.retry_at.values_mut() {
        *t = 0.0;
    }
    v.on_tick(&rec, &mut out).await.unwrap();
    let p = parsed(&out);
    assert_eq!(p.len(), 1);
    assert_eq!(p[0]["subscribe"]["marketSlugs"], json!([pmus_slug("btc", "5m", t0 + 300)]));
    *now.lock().unwrap() = T0 + 305.0;
    out.clear();
    v.on_tick(&rec, &mut out).await.unwrap();
    let kinds: Vec<String> = parsed(&out).iter().map(|m| m.as_object().unwrap().keys().next().unwrap().clone()).collect();
    assert_eq!(kinds.iter().filter(|k| *k == "unsubscribe").count(), 2);
    assert!(kinds.iter().filter(|k| *k == "subscribe").count() >= 2);
    assert_eq!(rec.stats.extra("resubscribes").unwrap(), 1);
}

#[tokio::test]
async fn pmus_preopen_error_retries_right_after_open() {
    let d = setup();
    let (rec, _wt) = writer(d.path(), "p");
    let now = Arc::new(Mutex::new(T0 + 299.0));
    let mut v = pm(now.clone(), &["btc"], 1e9);
    let t0 = T0 as i64;
    let mut out = Out::new();
    v.on_open(&rec, &mut out).await.unwrap();
    let nxt: Vec<String> = v.subs.values().filter(|s| s.start == t0 + 300).map(|s| s.rid.clone()).collect();
    assert_eq!(nxt.len(), 2);
    for rid in nxt {
        v.track(&json!({"request_id": rid, "error": "not found"}).to_string(), &rec);
    }
    let target = json!([pmus_slug("btc", "5m", t0 + 300)]);
    let retried = |out: &Out| parsed(out).iter().filter(|m| m["subscribe"]["marketSlugs"] == target).count();
    out.clear();
    *now.lock().unwrap() = T0 + 301.0;
    v.on_tick(&rec, &mut out).await.unwrap();
    assert_eq!(retried(&out), 0);
    *now.lock().unwrap() = T0 + 303.5;
    v.on_tick(&rec, &mut out).await.unwrap();
    assert_eq!(retried(&out), 2);
}

// ------------------------------------------------------------------------------------------------
// Kalshi signing

fn ed_key() -> (String, ed25519_dalek::VerifyingKey) {
    use ed25519_dalek::pkcs8::EncodePrivateKey;
    use rand::RngCore;
    let mut b = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut b);
    let k = ed25519_dalek::SigningKey::from_bytes(&b);
    (k.to_pkcs8_pem(Default::default()).unwrap().to_string(), k.verifying_key())
}

fn rsa_key(pkcs1: bool) -> (String, rsa::RsaPublicKey) {
    use rsa::pkcs1::EncodeRsaPrivateKey;
    use rsa::pkcs8::EncodePrivateKey;
    let k = rsa::RsaPrivateKey::new(&mut rand::thread_rng(), 1024).unwrap();
    let pem = if pkcs1 {
        k.to_pkcs1_pem(Default::default()).unwrap().to_string()
    } else {
        k.to_pkcs8_pem(Default::default()).unwrap().to_string()
    };
    (pem, k.to_public_key())
}

enum Pub {
    Ed(ed25519_dalek::VerifyingKey),
    Rsa(rsa::RsaPublicKey),
}

fn verify(pk: &Pub, msg: &str, sig_b64: &str) -> bool {
    let sig = base64::engine::general_purpose::STANDARD.decode(sig_b64).unwrap();
    match pk {
        Pub::Ed(k) => {
            use ed25519_dalek::Verifier;
            k.verify(msg.as_bytes(), &ed25519_dalek::Signature::from_slice(&sig).unwrap()).is_ok()
        }
        Pub::Rsa(k) => {
            use rsa::signature::Verifier;
            let vk = rsa::pss::VerifyingKey::<sha2::Sha256>::new(k.clone());
            vk.verify(msg.as_bytes(), &rsa::pss::Signature::try_from(sig.as_slice()).unwrap()).is_ok()
        }
    }
}

#[test]
fn kalshi_sign_ed25519_and_rsa_pss() {
    let (pem, vk) = ed_key();
    let k = SigningKey::from_pem(&pem).unwrap();
    assert!(matches!(k, SigningKey::Ed25519(_)));
    let sig = k.sign("1700000000000", "GET", "/trade-api/ws/v2");
    assert!(verify(&Pub::Ed(vk), "1700000000000GET/trade-api/ws/v2", &sig));
    for pkcs1 in [false, true] {
        let (pem, pk) = rsa_key(pkcs1);
        let k = SigningKey::from_pem(&pem).unwrap();
        assert!(matches!(k, SigningKey::Rsa(_)));
        let sig = k.sign("1700000000000", "GET", "/p");
        let pk = Pub::Rsa(pk);
        assert!(verify(&pk, "1700000000000GET/p", &sig));
        assert!(!verify(&pk, "1700000000001GET/p", &sig));
    }
    assert!(SigningKey::from_pem("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n").is_err());
}

#[tokio::test]
async fn kalshi_missing_credentials_idle() {
    let d = setup();
    let (rec, wt) = writer(d.path(), "ref_brti_live");
    let stats = rec.stats.clone();
    let mut b = KalshiBrti::new();
    b.profile_path = Some(d.path().join("missing.json"));
    b.url = "http://127.0.0.1:9/never".into();
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(b.run(rec, rx));
    tokio::time::sleep(Duration::from_millis(100)).await;
    tx.send(true).unwrap();
    h.await.unwrap();
    wt.join();
    assert_eq!(stats.extra("idle").unwrap(), "no_credentials");
    let recs = tape(d.path(), "ref_brti_live");
    assert_eq!(recs[0]["meta"], json!({"ctl": "idle", "why": "no_credentials"}));
}

/// Minimal HTTP/1.1 server: returns (status, extra headers, body) per request and keeps the
/// request heads it saw.
async fn http_server<F>(f: F) -> (String, Arc<Mutex<Vec<String>>>)
where
    F: Fn(&str) -> (u16, String, String) + Send + Sync + 'static,
{
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", l.local_addr().unwrap());
    let seen: Arc<Mutex<Vec<String>>> = Arc::default();
    let s2 = seen.clone();
    let f = Arc::new(f);
    tokio::spawn(async move {
        while let Ok((mut s, _)) = l.accept().await {
            let (seen, f) = (s2.clone(), f.clone());
            tokio::spawn(async move {
                loop {
                    let mut head = Vec::new();
                    let mut b = [0u8; 1];
                    while !head.ends_with(b"\r\n\r\n") {
                        match s.read(&mut b).await {
                            Ok(1) => head.push(b[0]),
                            _ => return,
                        }
                    }
                    let head = String::from_utf8_lossy(&head).to_string();
                    seen.lock().unwrap().push(head.clone());
                    let (status, hdrs, body) = f(&head);
                    let resp = format!(
                        "HTTP/1.1 {status} X\r\n{hdrs}Content-Length: {}\r\nContent-Type: application/json\r\n\r\n{body}",
                        body.len()
                    );
                    if s.write_all(resp.as_bytes()).await.is_err() {
                        return;
                    }
                }
            });
        }
    });
    (base, seen)
}

fn header(head: &str, name: &str) -> String {
    head.lines()
        .find_map(|l| l.split_once(':').filter(|(k, _)| k.trim().eq_ignore_ascii_case(name)).map(|(_, v)| v.trim().to_string()))
        .unwrap_or_default()
}

#[tokio::test]
async fn brti_polls_signed_and_backs_off_on_429() {
    for kind in ["ed25519", "rsa"] {
        let d = setup();
        let (pem, pk) = if kind == "ed25519" {
            let (p, v) = ed_key();
            (p, Pub::Ed(v))
        } else {
            let (p, v) = rsa_key(false);
            (p, Pub::Rsa(v))
        };
        let n = Arc::new(Mutex::new(0));
        let n2 = n.clone();
        let (base, seen) = http_server(move |_| {
            let mut g = n2.lock().unwrap();
            *g += 1;
            if *g == 1 {
                (200, String::new(), r#"{"data":{"payload":[{"time":1795622400000,"value":"97000.5"}]}}"#.to_string())
            } else {
                (429, "Retry-After: 1\r\n".to_string(), "{}".to_string())
            }
        })
        .await;
        let profile = d.path().join("kalshi-profile.json");
        std::fs::write(&profile, json!({"key_id": "kid-1", "private_key_pem": pem}).to_string()).unwrap();
        let (rec, wt) = writer(d.path(), "ref_brti_live");
        let stats = rec.stats.clone();
        let cred = doppel_tape::kalshi::load_credential(Some(profile), &rec).unwrap();
        let mut b = KalshiBrti::new();
        b.url = format!("{base}/trade-api/v2/cfbenchmarks/values?id=BRTI");
        let client = doppel_tape::http::signed_client(5);
        assert_eq!(b.poll(&client, &cred, "/trade-api/v2/cfbenchmarks/values", &rec).await, None);
        let wait = b.poll(&client, &cred, "/trade-api/v2/cfbenchmarks/values", &rec).await;
        assert_eq!(wait, Some(Duration::from_secs(2))); // Retry-After 1 clamped to >= 2
        drop(rec);
        wt.join();
        let head = seen.lock().unwrap()[0].clone();
        assert!(head.starts_with("GET /trade-api/v2/cfbenchmarks/values?id=BRTI "));
        assert_eq!(header(&head, "kalshi-access-key"), "kid-1");
        let ts = header(&head, "kalshi-access-timestamp");
        let msg = format!("{ts}GET/trade-api/v2/cfbenchmarks/values");
        assert!(verify(&pk, &msg, &header(&head, "kalshi-access-signature")), "{kind}");
        let recs = tape(d.path(), "ref_brti_live");
        assert_eq!(recs[0]["meta"]["status"], 200);
        assert_eq!(recs[0]["meta"]["new"], json!([[1795622400000i64, "97000.5"]]));
        assert!(recs[0]["meta"]["sent_tw"].as_i64().unwrap() <= recs[0]["tw"].as_i64().unwrap());
        assert_eq!(recs[1]["meta"]["status"], 429);
        assert_eq!(recs[2]["meta"], json!({"ctl": "rate_limited", "wait_s": 2.0}));
        assert_eq!(stats.extra("http_429").unwrap(), 1);
        let text = serde_json::to_string(&recs).unwrap();
        assert!(!text.contains("PRIVATE KEY") && !text.contains(&header(&head, "kalshi-access-signature")));
    }
}

// ------------------------------------------------------------------------------------------------
// Kalshi WS

struct KalshiMock {
    url: String,
    rest: String,
    profile: PathBuf,
    cmds: Arc<Mutex<Vec<Value>>>,
    sig_ok: Arc<Mutex<Vec<(String, bool)>>>,
}

/// A Kalshi-like WS server plus REST universe. `gap`: the first subscription's book skips a seq.
/// `reject_updates`: every update_subscription is answered with an error.
async fn kalshi_mock(d: &Path, gap: bool, reject_updates: bool) -> KalshiMock {
    let (pem, vk) = ed_key();
    let profile = d.join("kalshi-profile.json");
    std::fs::write(&profile, json!({"key_id": "kid-ws", "private_key_pem": pem}).to_string()).unwrap();
    // REST universe: one open market per series; after the first listing BTC rolls to a new ticker
    let calls = Arc::new(Mutex::new(0));
    let c2 = calls.clone();
    let (rest, _seen) = http_server(move |head| {
        let series = head.split("series_ticker=").nth(1).and_then(|s| s.split('&').next()).unwrap_or("").to_string();
        let mut n = c2.lock().unwrap();
        *n += 1;
        let t = if series == "KXBTC15M" && *n > 4 { "KXBTC15M-B".to_string() } else { format!("{series}-A") };
        (200, String::new(), json!({"markets": [{"ticker": t, "close_time": "2026-11-25T16:15:00Z"}]}).to_string())
    })
    .await;
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}/trade-api/ws/v2", l.local_addr().unwrap());
    let cmds: Arc<Mutex<Vec<Value>>> = Arc::default();
    let sig_ok: Arc<Mutex<Vec<(String, bool)>>> = Arc::default();
    let (cmds2, sig2) = (cmds.clone(), sig_ok.clone());
    tokio::spawn(async move {
        let Ok((s, _)) = l.accept().await else { return };
        let cb = |req: &Request, resp: Response| {
            let h = |n: &str| req.headers().get(n).map(|v| v.to_str().unwrap().to_string()).unwrap_or_default();
            let msg = format!("{}GET{}", h("kalshi-access-timestamp"), req.uri().path());
            sig2.lock().unwrap().push((h("kalshi-access-key"), verify(&Pub::Ed(vk), &msg, &h("kalshi-access-signature"))));
            Ok(resp)
        };
        let mut ws = tokio_tungstenite::accept_hdr_async(s, cb).await.unwrap();
        let mut sid = 0;
        while let Some(Ok(Message::Text(t))) = ws.next().await {
            let c: Value = serde_json::from_str(t.as_str()).unwrap();
            cmds2.lock().unwrap().push(c.clone());
            let id = c["id"].as_u64().unwrap();
            let mut send = Vec::new();
            match c["cmd"].as_str().unwrap() {
                "subscribe" => {
                    let first = sid == 0;
                    let (a, b) = (sid + 1, sid + 2);
                    sid += 2;
                    for (s, ch) in [(a, "orderbook_delta"), (b, "trade")] {
                        send.push(json!({"id": id, "type": "subscribed", "msg": {"channel": ch, "sid": s}}));
                    }
                    let mut q = 0;
                    for tk in c["params"]["market_tickers"].as_array().unwrap() {
                        q += 1;
                        send.push(json!({"type": "orderbook_snapshot", "sid": a, "seq": q, "msg": {"market_ticker": tk}}));
                    }
                    q += if gap && first { 2 } else { 1 };
                    send.push(json!({"type": "orderbook_delta", "sid": a, "seq": q, "msg": {"market_ticker": "KXBTC15M-A"}}));
                    send.push(json!({"type": "trade", "sid": b, "seq": 1, "msg": {"market_ticker": "KXBTC15M-A"}}));
                }
                "unsubscribe" => {
                    for s in c["params"]["sids"].as_array().unwrap() {
                        send.push(json!({"id": id, "sid": s, "seq": 99, "type": "unsubscribed"}));
                    }
                }
                "update_subscription" if reject_updates => {
                    send.push(json!({"id": id, "type": "error", "msg": {"code": 13, "msg": "unsupported action"}}));
                }
                _ => {
                    send.push(json!({"id": id, "type": "ok", "msg": {}}));
                    if c["params"]["action"] == "add_markets" && c["params"]["sids"][0].as_u64() == Some(sid - 1) {
                        for tk in c["params"]["market_tickers"].as_array().unwrap() {
                            send.push(json!({"type": "orderbook_snapshot", "sid": sid - 1, "msg": {"market_ticker": tk}}));
                        }
                    }
                }
            }
            for m in send {
                ws.send(Message::Text(m.to_string().into())).await.unwrap();
            }
        }
    });
    KalshiMock { url, rest: format!("{rest}/trade-api/v2"), profile, cmds, sig_ok }
}

async fn run_kalshi(d: &Path, m: &KalshiMock, ms: u64) -> (Vec<Value>, Vec<Value>, Arc<doppel_tape::recorder::Stats>) {
    let (rec, wt) = writer(d, "kalshi_ws");
    let stats = rec.stats.clone();
    let mut v = KalshiWs::new();
    v.url = m.url.clone();
    v.profile_path = Some(m.profile.clone());
    v.universe = doppel_tape::universe::KalshiUniverse::new(&m.rest);
    v.universe.refresh = Duration::from_millis(500);
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(ms)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();
    let cmds = m.cmds.lock().unwrap().clone();
    (cmds, tape(d, "kalshi_ws"), stats)
}

#[tokio::test]
async fn kalshi_ws_signed_subscribe_rollover_and_seq_gap() {
    let d = setup();
    let m = kalshi_mock(d.path(), true, false).await;
    let (cmds, recs, stats) = run_kalshi(d.path(), &m, 3500).await;
    assert_eq!(m.sig_ok.lock().unwrap().clone(), vec![("kid-ws".to_string(), true)]);
    let names: Vec<&str> = cmds.iter().map(|c| c["cmd"].as_str().unwrap()).collect();
    assert_eq!(&names[..3], ["subscribe", "unsubscribe", "subscribe"], "{cmds:?}");
    assert_eq!(cmds[0]["params"]["channels"], json!(["orderbook_delta", "trade"]));
    assert_eq!(cmds[0]["params"]["market_tickers"], json!(["KXBTC15M-A", "KXETH15M-A", "KXSOL15M-A", "KXXRP15M-A"]));
    assert_eq!(cmds[1]["params"]["sids"], json!([1, 2]));
    // the BTC window rolled: added to both subscriptions, old one deleted
    let upd: Vec<&Value> = cmds.iter().filter(|c| c["cmd"] == "update_subscription").collect();
    assert!(upd.iter().any(|c| c["params"]["action"] == "add_markets"
        && c["params"]["market_tickers"] == json!(["KXBTC15M-B"])
        && c["params"]["sids"] == json!([3])));
    assert!(upd.iter().any(|c| c["params"]["action"] == "add_markets" && c["params"]["sids"] == json!([4])));
    assert!(upd.iter().any(|c| c["params"]["action"] == "delete_markets" && c["params"]["market_tickers"] == json!(["KXBTC15M-A"])));
    let c = ctls(&recs);
    assert!(c.contains(&"seq_gap".to_string()) && c.contains(&"resubscribe".to_string()), "{c:?}");
    let gap = recs.iter().find(|r| r["meta"]["ctl"] == "seq_gap").unwrap();
    assert_eq!(gap["meta"], json!({"ctl": "seq_gap", "sid": 1, "channel": "orderbook_delta", "expected": 5, "got": 6}));
    assert_eq!(stats.extra("seq_gaps").unwrap(), 1);
    assert_eq!(stats.extra("resubscribes").unwrap(), 1);
    assert!(!c.contains(&"no_data".to_string()) && !c.contains(&"sub_error".to_string()), "{c:?}");
    // every server frame is recorded raw
    assert!(recs.iter().filter(|r| r["raw"].as_str().unwrap().contains("orderbook_snapshot")).count() >= 9);
    let text = serde_json::to_string(&recs).unwrap();
    assert!(!text.contains("PRIVATE KEY"));
}

#[tokio::test]
async fn kalshi_ws_rejected_update_resubscribes_with_new_set() {
    let d = setup();
    let m = kalshi_mock(d.path(), false, true).await;
    let (cmds, recs, stats) = run_kalshi(d.path(), &m, 2500).await;
    let names: Vec<&str> = cmds.iter().map(|c| c["cmd"].as_str().unwrap()).collect();
    let first_upd = names.iter().position(|n| *n == "update_subscription").expect("an update was sent");
    let unsub = names.iter().position(|n| *n == "unsubscribe").expect("rejected update -> unsubscribe");
    assert!(unsub > first_upd);
    let resub = &cmds[names.iter().rposition(|n| *n == "subscribe").unwrap()];
    assert_eq!(resub["params"]["market_tickers"], json!(["KXBTC15M-B", "KXETH15M-A", "KXSOL15M-A", "KXXRP15M-A"]));
    let c = ctls(&recs);
    assert!(c.contains(&"sub_error".to_string()), "{c:?}");
    let r = recs.iter().find(|r| r["meta"]["ctl"] == "resubscribe").unwrap();
    assert_eq!(r["meta"]["why"], "update_error");
    assert_eq!(stats.extra("resubscribes").unwrap(), 1);
}

#[test]
fn kalshi_ws_deadlines() {
    let d = setup();
    let (rec, _wt) = writer(d.path(), "k");
    let mut v = KalshiWs::new();
    v.ack_timeout = Duration::ZERO;
    let mut out = Out::new();
    let want: std::collections::BTreeSet<String> = ["T-1".to_string()].into();
    v.update_tickers(&rec, &mut out, want.clone());
    assert_eq!(out.len(), 1);
    // only one of the two acks arrives: reconnect instead of waiting forever
    v.handle(&json!({"id": 1, "type": "subscribed", "msg": {"channel": "trade", "sid": 7}}).to_string(), &rec, &mut out);
    v.check_deadlines(&rec, &mut out);
    assert_eq!(v.take_reconnect().as_deref(), Some("subscribe_ack_timeout"));

    // both acks but no snapshot for a ticker within the deadline: no_data -> resubscribe
    let mut v = KalshiWs::new();
    v.snapshot_timeout = Duration::ZERO;
    let mut out = Out::new();
    v.update_tickers(&rec, &mut out, want);
    for (sid, ch) in [(1, "orderbook_delta"), (2, "trade")] {
        v.handle(&json!({"id": 1, "type": "subscribed", "msg": {"channel": ch, "sid": sid}}).to_string(), &rec, &mut out);
    }
    out.clear();
    v.check_deadlines(&rec, &mut out);
    let sent: Vec<Value> = out.iter().map(|s| serde_json::from_str(s).unwrap()).collect();
    assert_eq!(sent, vec![json!({"id": 2, "cmd": "unsubscribe", "params": {"sids": [1, 2]}})]);
    assert_eq!(rec.stats.extra("resubscribes").unwrap(), 1);
}

// ------------------------------------------------------------------------------------------------
// spot L2: Coinbase sequence gap -> reconnect -> fresh snapshot, and the tape decodes

#[tokio::test]
async fn spot_l2_coinbase_gap_reconnects_and_tape_decodes() {
    let d = setup();
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", l.local_addr().unwrap());
    let subs: Arc<Mutex<Vec<Value>>> = Arc::default();
    let s2 = subs.clone();
    tokio::spawn(async move {
        let mut conn = 0;
        while let Ok((s, _)) = l.accept().await {
            conn += 1;
            let mut ws = tokio_tungstenite::accept_async(s).await.unwrap();
            for _ in 0..2 {
                if let Some(Ok(Message::Text(t))) = ws.next().await {
                    s2.lock().unwrap().push(serde_json::from_str(t.as_str()).unwrap());
                }
            }
            let upd = |side: &str, p: &str, q: &str| json!({"side": side, "event_time": "2026-11-25T16:00:00Z", "price_level": p, "new_quantity": q});
            let l2 = |seq: u64, typ: &str, u: Vec<Value>| {
                json!({"channel": "l2_data", "client_id": "", "timestamp": "2026-11-25T16:00:00.5Z", "sequence_num": seq,
                       "events": [{"type": typ, "product_id": "BTC-USD", "updates": u}]})
            };
            let mut frames = vec![
                json!({"channel": "subscriptions", "timestamp": "2026-11-25T16:00:00Z", "sequence_num": 0, "events": []}),
                l2(1, "snapshot", vec![upd("bid", "100", "1"), upd("offer", "101", "1")]),
                json!({"channel": "heartbeats", "timestamp": "2026-11-25T16:00:01Z", "sequence_num": 2, "events": [{"heartbeat_counter": 1}]}),
            ];
            // the first connection skips sequence 3
            frames.push(l2(if conn == 1 { 4 } else { 3 }, "update", vec![upd("bid", "100.5", "2")]));
            for f in frames {
                ws.send(Message::Text(f.to_string().into())).await.unwrap();
            }
            tokio::spawn(async move { while let Some(Ok(_)) = ws.next().await {} });
        }
    });
    let (rec, wt) = writer(d.path(), "spot_l2_coinbase");
    let stats = rec.stats.clone();
    let mut v = doppel_tape::l2::SpotL2::new("coinbase").unwrap();
    v.url = url;
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(2500)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();

    let subs = subs.lock().unwrap().clone();
    assert!(subs.len() >= 4, "{subs:?}");
    assert_eq!(subs[0], json!({"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "level2"}));
    assert_eq!(subs[1]["channel"], "heartbeats");
    let recs = tape(d.path(), "spot_l2_coinbase");
    let c = ctls(&recs);
    assert_eq!(&c[..6], ["connect", "subscribe", "seq_gap", "reconnect", "disconnect", "connect"], "{c:?}");
    let gap = recs.iter().find(|r| r["meta"]["ctl"] == "seq_gap").unwrap();
    assert_eq!(gap["meta"], json!({"ctl": "seq_gap", "expected": 3, "got": 4}));
    assert_eq!(stats.extra("seq_gaps").unwrap(), 1);
    // heartbeats are recorded: they carry the sequence
    assert!(recs.iter().any(|r| r["raw"].as_str().unwrap().contains("heartbeats")));

    // the offline decoder sees the same gap, then rebuilds from the second connection's snapshot
    let mut book = doppel_fair::venue::VenueBook::new(doppel_fair::venue::Venue::Coinbase);
    let (mut gaps, mut last) = (0, None);
    for r in &recs {
        if book.apply_record(r["raw"].as_str().unwrap(), r["tw"].as_i64().unwrap(), r.get("meta")).is_err() {
            gaps += 1;
        }
        if r.get("meta").is_none() {
            last = book.snapshot();
        }
    }
    assert_eq!(gaps, 1);
    let s = last.expect("valid after the fresh snapshot");
    assert_eq!((s.bids, s.asks), (vec![(100.5, 2.0), (100.0, 1.0)], vec![(101.0, 1.0)]));
    // the shutdown `disconnect` ends continuity
    assert!(book.snapshot().is_none());
}

// ------------------------------------------------------------------------------------------------
// spot L2 over REST: LMAX polls, failures back off and reset the decoded book

#[tokio::test]
async fn lmax_rest_poll_records_backs_off_and_decodes() {
    let d = setup();
    let book = r#"{"instrument_id":"btc-usd","timestamp":"2026-11-26T15:28:49.000Z","status":"OPEN","bids":[{"price":"60208.200000","quantity":"1.0000"}],"asks":[{"price":"60216.000000","quantity":"0.0800"}]}"#;
    let n = Arc::new(Mutex::new(0));
    let n2 = n.clone();
    let (base, seen) = http_server(move |_| {
        let mut g = n2.lock().unwrap();
        *g += 1;
        match *g {
            1 | 5 => (200, String::new(), book.to_string()),
            2 => (429, String::new(), r#"{"error":"rate limited"}"#.to_string()),
            3 => (502, String::new(), "<html>bad gateway</html>".to_string()),
            _ => (200, String::new(), "<html>maintenance</html>".to_string()),
        }
    })
    .await;
    let (rec, wt) = writer(d.path(), "spot_l2_lmax");
    let stats = rec.stats.clone();
    let mut p = doppel_tape::l2::RestL2::new("lmax").unwrap();
    assert_eq!(p.url, "https://public-data-api.london-digital.lmax.com/v1/orderbook/btc-usd");
    p.url = format!("{base}/v1/orderbook/btc-usd");
    let client = doppel_tape::http::client(3);
    let mut waits = Vec::new();
    for _ in 0..5 {
        waits.push(p.poll(&client, &rec).await);
    }
    // PERELMAN rest_poll: 5 s, doubling, reset by a good book
    assert_eq!(waits, [None, Some(5.0), Some(10.0), Some(20.0), None]);
    p.url = "http://127.0.0.1:1/v1/orderbook/btc-usd".into(); // nothing listens: transport error
    assert_eq!(p.poll(&client, &rec).await, Some(5.0));
    drop(rec);
    wt.join();
    assert!(seen.lock().unwrap()[0].starts_with("GET /v1/orderbook/btc-usd "));
    assert_eq!(stats.extra("http_429").unwrap(), 1);
    let recs = tape(d.path(), "spot_l2_lmax");
    let status: Vec<_> = recs.iter().map(|r| r["meta"]["status"].clone()).collect();
    assert_eq!(status[..5], [json!(200), json!(429), json!(502), json!(200), json!(200)]);
    assert_eq!(recs[5]["meta"], json!({"ctl": "poll_error", "err": "Connect"}));
    assert!(recs[0]["meta"]["sent_tw"].as_i64().unwrap() <= recs[0]["tw"].as_i64().unwrap());
    // offline: only the good polls give a book, every failure resets it
    let mut v = doppel_fair::venue::VenueBook::new(doppel_fair::venue::Venue::Lmax);
    let valid: Vec<bool> = recs
        .iter()
        .map(|r| {
            v.apply_record(r["raw"].as_str().unwrap(), r["tw"].as_i64().unwrap(), r.get("meta")).unwrap();
            v.snapshot().is_some()
        })
        .collect();
    assert_eq!(valid, [true, false, false, false, true, false]);
}

// ------------------------------------------------------------------------------------------------
// perps: Kalshi margin WS (signed, seq gap -> reconnect), Coinbase Derivatives WS, public REST poll

#[tokio::test]
async fn perp_kalshi_signed_gap_reconnects_and_tape_decodes() {
    let d = setup();
    let (pem, vk) = ed_key();
    let profile = d.path().join("kalshi-profile.json");
    std::fs::write(&profile, json!({"key_id": "kid-perp", "private_key_pem": pem}).to_string()).unwrap();
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}/trade-api/ws/v2/margin", l.local_addr().unwrap());
    let cmds: Arc<Mutex<Vec<Value>>> = Arc::default();
    let sig_ok: Arc<Mutex<Vec<bool>>> = Arc::default();
    let (cmds2, sig2) = (cmds.clone(), sig_ok.clone());
    tokio::spawn(async move {
        let mut conn = 0;
        while let Ok((s, _)) = l.accept().await {
            conn += 1;
            let sig3 = sig2.clone();
            let cb = move |req: &Request, resp: Response| {
                let h = |n: &str| req.headers().get(n).map(|v| v.to_str().unwrap().to_string()).unwrap_or_default();
                let msg = format!("{}GET{}", h("kalshi-access-timestamp"), req.uri().path());
                let ok = h("kalshi-access-key") == "kid-perp" && verify(&Pub::Ed(vk), &msg, &h("kalshi-access-signature"));
                sig3.lock().unwrap().push(ok);
                Ok(resp)
            };
            let mut ws = tokio_tungstenite::accept_hdr_async(s, cb).await.unwrap();
            let Some(Ok(Message::Text(t))) = ws.next().await else { continue };
            cmds2.lock().unwrap().push(serde_json::from_str(t.as_str()).unwrap());
            let mut send = vec![];
            for (sid, ch) in [(1, "orderbook_delta"), (2, "trade"), (3, "ticker")] {
                send.push(json!({"id": 1, "type": "subscribed", "msg": {"channel": ch, "sid": sid}}));
            }
            send.push(json!({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                "msg": {"market_ticker": "KXBTCPERP", "bid": [["5.9931", "195.00"]], "ask": [["5.9933", "598.00"]]}}));
            // the first connection skips seq 2 on the book
            let seq = if conn == 1 { 3 } else { 2 };
            send.push(json!({"type": "orderbook_delta", "sid": 1, "seq": seq,
                "msg": {"market_ticker": "KXBTCPERP", "price": "5.9932", "delta": "5.00", "side": "bid"}}));
            send.push(json!({"type": "trade", "sid": 2, "seq": 1,
                "msg": {"trade_id": format!("t{conn}"), "market_ticker": "KXBTCPERP", "price": "5.9933", "count": "2.00", "taker_side": "bid"}}));
            for m in send {
                ws.send(Message::Text(m.to_string().into())).await.unwrap();
            }
            tokio::spawn(async move { while let Some(Ok(_)) = ws.next().await {} });
        }
    });
    let (rec, wt) = writer(d.path(), "perp_kalshi_btc");
    let stats = rec.stats.clone();
    let mut v = doppel_tape::perps::KalshiPerpWs::new();
    assert_eq!(v.url, "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin");
    v.url = url;
    v.profile_path = Some(profile);
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(2500)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();

    let sigs = sig_ok.lock().unwrap().clone();
    assert!(sigs.len() >= 2 && sigs.iter().all(|x| *x), "signed handshake on every connect: {sigs:?}");
    let cmds = cmds.lock().unwrap().clone();
    assert_eq!(
        cmds[0],
        json!({"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta", "trade", "ticker"], "market_tickers": ["KXBTCPERP"]}})
    );
    let recs = tape(d.path(), "perp_kalshi_btc");
    let c = ctls(&recs);
    assert_eq!(&c[..5], ["connect", "subscribe", "seq_gap", "reconnect", "disconnect"], "{c:?}");
    let gap = recs.iter().find(|r| r["meta"]["ctl"] == "seq_gap").unwrap();
    assert_eq!(gap["meta"], json!({"ctl": "seq_gap", "sid": 1, "channel": "orderbook_delta", "expected": 2, "got": 3}));
    assert_eq!(stats.extra("seq_gaps").unwrap(), 1);
    assert!(!serde_json::to_string(&recs).unwrap().contains("PRIVATE KEY"));

    // offline: the gap leaves no book, the second connection's snapshot + delta rebuild it
    let mut k = doppel_fair::perp::KalshiPerp::new();
    let mut last = None;
    let mut gaps = 0;
    for r in &recs {
        gaps += k.apply_record(r["raw"].as_str().unwrap(), r["tw"].as_i64().unwrap(), r.get("meta")).is_err() as u32;
        if r.get("meta").is_none() {
            last = k.top();
        }
    }
    assert_eq!(gaps, 1);
    let t = last.expect("book after the reconnect");
    assert!((t.bid - 59932.0).abs() < 1e-6 && (t.bid_qty - 0.0005).abs() < 1e-12 && (t.ask - 59933.0).abs() < 1e-6);
    assert!(k.trades.len() >= 2 && k.trades.iter().all(|x| x.taker_buy == Some(true)));
}

#[tokio::test]
async fn perp_kalshi_without_credential_idles() {
    let d = setup();
    let (rec, wt) = writer(d.path(), "perp_kalshi_btc");
    let stats = rec.stats.clone();
    let mut v = doppel_tape::perps::KalshiPerpWs::new();
    v.profile_path = Some(d.path().join("missing.json"));
    v.url = "ws://127.0.0.1:9/never".into();
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(100)).await;
    tx.send(true).unwrap();
    h.await.unwrap();
    wt.join();
    assert_eq!(stats.extra("idle").unwrap(), "no_credentials");
    assert_eq!(ctls(&tape(d.path(), "perp_kalshi_btc")), ["idle"]);
}

#[tokio::test]
async fn perp_cbde_subscribes_level2_trades_heartbeats() {
    let d = setup();
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("ws://{}", l.local_addr().unwrap());
    let subs: Arc<Mutex<Vec<Value>>> = Arc::default();
    let s2 = subs.clone();
    tokio::spawn(async move {
        let Ok((s, _)) = l.accept().await else { return };
        let mut ws = tokio_tungstenite::accept_async(s).await.unwrap();
        for _ in 0..3 {
            if let Some(Ok(Message::Text(t))) = ws.next().await {
                s2.lock().unwrap().push(serde_json::from_str(t.as_str()).unwrap());
            }
        }
        let fixture = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/fixtures_perps/cbde_ws.jsonl");
        for f in std::fs::read_to_string(fixture).unwrap().lines() {
            let r: Value = serde_json::from_str(f).unwrap();
            if r.get("meta").is_none() {
                ws.send(Message::Text(r["raw"].as_str().unwrap().to_string().into())).await.unwrap();
            }
        }
        while let Some(Ok(_)) = ws.next().await {}
    });
    let (rec, wt) = writer(d.path(), "perp_cbde_btc");
    let stats = rec.stats.clone();
    let mut v = doppel_tape::l2::SpotL2::cbde_btc();
    v.url = url;
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(1500)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();
    let subs = subs.lock().unwrap().clone();
    assert_eq!(
        subs,
        vec![
            json!({"type": "subscribe", "product_ids": ["BIP-20DEC30-CDE"], "channel": "level2"}),
            json!({"type": "subscribe", "product_ids": ["BIP-20DEC30-CDE"], "channel": "market_trades"}),
            json!({"type": "subscribe", "channel": "heartbeats"}),
        ]
    );
    let recs = tape(d.path(), "perp_cbde_btc");
    assert_eq!(ctls(&recs)[..2], ["connect", "subscribe"]);
    assert_eq!(stats.extra("seq_gaps").unwrap(), 0);
    assert_eq!(recs.iter().filter(|r| r.get("meta").is_none()).count(), 80);
}

#[tokio::test]
async fn perp_rest_records_keys_and_backs_off() {
    let d = setup();
    let n = Arc::new(Mutex::new(0));
    let n2 = n.clone();
    let (base, seen) = http_server(move |head| {
        let mut g = n2.lock().unwrap();
        *g += 1;
        if *g == 3 {
            return (429, String::new(), r#"{"error":{"code":"too_many_requests"}}"#.to_string());
        }
        let body = if head.contains("/orderbook") {
            r#"{"orderbook":{"bids":[["5.9931","4.00"]],"asks":[["5.9933","2.00"]]}}"#
        } else {
            r#"{"trades":[]}"#
        };
        (200, String::new(), body.to_string())
    })
    .await;
    let (rec, wt) = writer(d.path(), "perp_kalshi_btc_rest");
    let stats = rec.stats.clone();
    let s = Duration::from_secs;
    let mut p = doppel_tape::perps::PerpRest::new(vec![
        doppel_tape::perps::Target { key: "orderbook", url: format!("{base}/margin/markets/KXBTCPERP/orderbook"), every: s(1) },
        doppel_tape::perps::Target { key: "trades", url: format!("{base}/margin/trades?ticker=KXBTCPERP&limit=100"), every: s(1) },
    ]);
    let client = doppel_tape::http::client(3);
    let t = p.targets.clone();
    let mut waits = Vec::new();
    for i in [0, 1, 0, 0] {
        waits.push(p.poll(&client, &rec, &t[i]).await);
    }
    assert_eq!(waits, [None, None, Some(5.0), None]);
    drop(rec);
    wt.join();
    assert!(seen.lock().unwrap()[1].starts_with("GET /margin/trades?ticker=KXBTCPERP&limit=100 "));
    assert_eq!(stats.extra("http_429").unwrap(), 1);
    let recs = tape(d.path(), "perp_kalshi_btc_rest");
    let km: Vec<_> = recs.iter().map(|r| (r["meta"]["key"].clone(), r["meta"]["status"].clone())).collect();
    let want = [("orderbook", 200), ("trades", 200), ("orderbook", 429), ("orderbook", 200)].map(|(k, s)| (json!(k), json!(s)));
    assert_eq!(km, want);
    // offline: the 429 leaves no book, the next poll restores it
    let mut k = doppel_fair::perp::KalshiPerp::new();
    let valid: Vec<bool> = recs
        .iter()
        .map(|r| {
            k.apply_record(r["raw"].as_str().unwrap(), r["tw"].as_i64().unwrap(), r.get("meta")).unwrap();
            k.top().is_some()
        })
        .collect();
    assert_eq!(valid, [true, true, false, true]);
}
