//! Local mock tests: WebSocket watchdog, PM US signer headers per connect and no redirects,
//! PM US rollover, Kalshi signing (generated keys), BRTI signed poll + 429, Kalshi WS seq gap.
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

#[tokio::test]
async fn kalshi_ws_signed_subscribe_rollover_and_seq_gap() {
    let d = setup();
    let (pem, vk) = ed_key();
    let profile = d.path().join("kalshi-profile.json");
    std::fs::write(&profile, json!({"key_id": "kid-ws", "private_key_pem": pem}).to_string()).unwrap();
    // REST universe: one open market per series; after a few calls BTC rolls to a new ticker
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
    let sig_ok = Arc::new(Mutex::new(Vec::new()));
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
            match c["cmd"].as_str().unwrap() {
                "subscribe" => {
                    let first = sid == 0;
                    let (a, b) = (sid + 1, sid + 2);
                    sid += 2;
                    for (s, ch) in [(a, "orderbook_delta"), (b, "trade")] {
                        let m = json!({"id": id, "type": "subscribed", "msg": {"channel": ch, "sid": s}});
                        ws.send(Message::Text(m.to_string().into())).await.unwrap();
                    }
                    let t = "KXBTC15M-A";
                    let seqs: &[u64] = if first { &[1, 2, 4] } else { &[1, 2, 3] }; // first one has a gap
                    for (i, q) in seqs.iter().enumerate() {
                        let typ = if i == 0 { "orderbook_snapshot" } else { "orderbook_delta" };
                        let m = json!({"type": typ, "sid": a, "seq": q, "msg": {"market_ticker": t}});
                        ws.send(Message::Text(m.to_string().into())).await.unwrap();
                    }
                    let m = json!({"type": "trade", "sid": b, "seq": 1, "msg": {"market_ticker": t}});
                    ws.send(Message::Text(m.to_string().into())).await.unwrap();
                }
                "unsubscribe" => {
                    for s in c["params"]["sids"].as_array().unwrap() {
                        let m = json!({"id": id, "sid": s, "seq": 99, "type": "unsubscribed"});
                        ws.send(Message::Text(m.to_string().into())).await.unwrap();
                    }
                }
                _ => {
                    let m = json!({"id": id, "type": "ok", "msg": {}});
                    ws.send(Message::Text(m.to_string().into())).await.unwrap();
                }
            }
        }
    });
    let (rec, wt) = writer(d.path(), "kalshi_ws");
    let stats = rec.stats.clone();
    let mut v = KalshiWs::new();
    v.url = url;
    v.profile_path = Some(profile);
    v.universe = doppel_tape::universe::KalshiUniverse::new(&format!("{rest}/trade-api/v2"));
    v.universe.refresh = Duration::from_millis(500);
    let (tx, rx) = watch::channel(false);
    let h = tokio::spawn(run_ws(v, rec, rx, fast()));
    tokio::time::sleep(Duration::from_millis(3500)).await;
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), h).await.unwrap().unwrap();
    wt.join();

    assert_eq!(sig_ok.lock().unwrap().clone(), vec![("kid-ws".to_string(), true)]);
    let cmds = cmds.lock().unwrap().clone();
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
    let recs = tape(d.path(), "kalshi_ws");
    let c = ctls(&recs);
    assert!(c.contains(&"seq_gap".to_string()) && c.contains(&"resubscribe".to_string()), "{c:?}");
    let gap = recs.iter().find(|r| r["meta"]["ctl"] == "seq_gap").unwrap();
    assert_eq!(gap["meta"], json!({"ctl": "seq_gap", "sid": 1, "channel": "orderbook_delta", "expected": 3, "got": 4}));
    assert_eq!(stats.extra("seq_gaps").unwrap(), 1);
    // every server frame is recorded raw
    assert!(recs.iter().filter(|r| r["raw"].as_str().unwrap().contains("orderbook_delta")).count() >= 4);
    let text = serde_json::to_string(&recs).unwrap();
    assert!(!text.contains("PRIVATE KEY"));
}
