//! Read receipt-clock market events from a trusted local JSONL producer.
use base64::{engine::general_purpose::STANDARD, Engine as _};
use doppel_exec::{
    admission::Approval,
    netting::{Signal, WatchState},
    oms::{Action, Feed, Oms},
    risk::{Book, Limits, MAX_API_RTT_MS},
    venues::{
        kalshi::{self, Kalshi, RateLimit},
        pmus::{self, Pmus},
        Request,
    },
    wall_ns, Market, Result, Venue, WireEvent, MILLIS,
};
use ed25519_dalek::{pkcs8::DecodePrivateKey, SigningKey, VerifyingKey};
use futures_util::{SinkExt, StreamExt};
use reqwest::{header::HeaderMap, Client};
use serde::Deserialize;
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::{BufRead, Read},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    venues: Vec<VenueConfig>,
    ledger: PathBuf,
    #[serde(default)]
    risk: Limits,
    approval: Option<ApprovalFiles>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct VenueConfig {
    venue: Venue,
    key_id: Option<String>,
    key_file: Option<PathBuf>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ApprovalFiles {
    manifest: PathBuf,
    signature: PathBuf,
    trusted_public_key_hex: String,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Input {
    Signal { signal: Signal },
    Signals { signals: Vec<Signal> },
    Book { market: Market, book: Book },
    Feed { feed: Feed, received_ns: u64 },
    Watchdog { genome_id: String, state: WatchState, ts_ns: u64 },
    BinanceSweep { active: bool, ts_ns: u64 },
    MockRtt { venue: Venue, rtt_ms: u64 },
}

enum Api {
    Pmus(Pmus),
    Kalshi(Kalshi),
}

impl Api {
    fn origin(&self) -> &'static str {
        match self {
            Self::Pmus(_) => pmus::REST,
            Self::Kalshi(_) => kalshi::REST_ORIGIN,
        }
    }

    fn ws_url(&self) -> &'static str {
        match self {
            Self::Pmus(_) => pmus::WS,
            Self::Kalshi(_) => kalshi::WS_URL,
        }
    }

    fn positions(&self, ms: u64) -> Request {
        match self {
            Self::Pmus(p) => p.positions(ms),
            Self::Kalshi(k) => k.positions(ms),
        }
    }

    fn open_orders(&self, ms: u64) -> Result<Request> {
        match self {
            Self::Pmus(p) => Ok(p.open_orders(ms)),
            Self::Kalshi(k) => k.cancel_all(ms),
        }
    }

    fn request(&self, action: &Action, ms: u64) -> Result<Request> {
        match (self, action) {
            (Self::Pmus(p), Action::Submit(o)) => p.submit(o, ms),
            (Self::Kalshi(k), Action::Submit(o)) => k.submit(o, ms),
            (Self::Pmus(p), Action::Cancel { market, venue_id }) => p.cancel(&market.id, venue_id, ms),
            (Self::Kalshi(k), Action::Cancel { market, venue_id }) => k.cancel(&market.id, venue_id, ms),
            (Self::Pmus(p), Action::CancelAll { .. }) => p.cancel_all(ms),
            (Self::Kalshi(k), Action::CancelAll { .. }) => k.cancel_all(ms),
        }
    }

    fn events(&self, frame: &Value) -> Result<Vec<WireEvent>> {
        match self {
            Self::Pmus(p) => p.events(frame),
            Self::Kalshi(k) => k.events(frame),
        }
    }

    fn ws_auth(&self, ms: u64) -> (Vec<(String, String)>, Value) {
        match self {
            Self::Pmus(p) => (p.ws_headers(ms), p.subscription()),
            Self::Kalshi(k) => (k.ws_headers(ms), k.subscription()),
        }
    }

    fn flat(&self, positions: &Value, orders: &Value) -> bool {
        match self {
            Self::Pmus(_) => {
                positions["positions"].as_object().is_some_and(|p| p.is_empty())
                    && positions["eof"].as_bool() == Some(true)
                    && orders["orders"].as_array().is_some_and(|o| o.is_empty())
            }
            Self::Kalshi(_) => {
                positions["market_positions"]
                    .as_array()
                    .is_some_and(|p| p.iter().all(|row| row["position_fp"].as_str().and_then(|s| s.parse::<f64>().ok()) == Some(0.0)))
                    && positions["cursor"].as_str() == Some("")
            }
        }
    }
}

struct Reply {
    status: u16,
    headers: HeaderMap,
    data: Value,
    elapsed_ms: u64,
}
async fn transport(client: &Client, api: &Api, request: Request) -> Result<Reply> {
    let start = Instant::now();
    let url = format!("{}{}", api.origin(), request.path);
    let mut builder = client.request(request.method.parse()?, url);
    for (key, value) in request.headers {
        builder = builder.header(key, value);
    }
    if let Some(body) = request.body {
        builder = builder.header("content-type", "application/json").body(serde_json::to_vec(&body)?);
    }
    let mut response = builder.send().await.map_err(|_| "HTTP response unavailable")?;
    let status = response.status().as_u16();
    let headers = response.headers().clone();
    let mut raw = Vec::new();
    if (200..300).contains(&status) {
        while let Some(chunk) = response.chunk().await.map_err(|_| "HTTP body unavailable")? {
            if raw.len() + chunk.len() > 1_048_576 {
                return Err("HTTP body limit".into());
            }
            raw.extend_from_slice(&chunk);
        }
    }
    let data = if raw.is_empty() { Value::Null } else { serde_json::from_slice(&raw)? };
    Ok(Reply { status, headers, data, elapsed_ms: u64::try_from(start.elapsed().as_micros().div_ceil(1000)).unwrap_or(u64::MAX) })
}

enum Remote {
    Ready(Venue),
    Frame(Venue, Value),
    Disconnected(Venue),
}

async fn socket(venue: Venue, api: Arc<Api>, outbound: mpsc::Sender<Remote>) -> Result<()> {
    let mut request = api.ws_url().into_client_request()?;
    let (headers, subscription) = api.ws_auth(wall_ns() / MILLIS);
    for (key, value) in headers {
        request.headers_mut().insert(key.parse::<reqwest::header::HeaderName>()?, value.parse()?);
    }
    let (mut ws, _) = tokio::time::timeout(Duration::from_secs(3), tokio_tungstenite::connect_async(request))
        .await
        .map_err(|_| "private handshake timed out")?
        .map_err(|_| "private handshake failed")?;
    ws.send(Message::Text(subscription.to_string().into())).await?;
    let mut subscriptions = BTreeSet::new();
    let mut ready = false;
    let mut ping = tokio::time::interval(Duration::from_secs(1));
    let mut last_packet = Instant::now();
    loop {
        tokio::select! {
            _ = ping.tick() => {
                if last_packet.elapsed() > Duration::from_secs(3) {
                    return Err("private stream stopped responding".into());
                }
                ws.send(Message::Ping(Vec::new().into())).await?;
            }
            msg = ws.next() => {
                let msg = msg.ok_or("private stream closed")??;
                last_packet = Instant::now();
                if let Message::Text(text) = msg {
                    let frame: Value = serde_json::from_str(&text)?;
                    if venue == Venue::Kalshi && frame["type"] == "subscribed" {
                        if let Some(channel) = frame["msg"]["channel"].as_str() {
                            subscriptions.insert(channel.to_owned());
                        }
                    }
                    if !ready && match venue {
                        Venue::Pmus => frame["orderSubscriptionSnapshot"]["orders"].as_array()
                            .is_some_and(|orders| orders.is_empty()),
                        Venue::Kalshi => subscriptions.contains("fill") && subscriptions.contains("user_orders"),
                    } {
                        ready = true;
                        outbound.send(Remote::Ready(venue)).await?;
                    }
                    if !ready && frame["orderSubscriptionSnapshot"]["orders"].as_array()
                        .is_some_and(|orders| !orders.is_empty()) {
                        return Err("unexpected preexisting private orders".into());
                    }
                    outbound.send(Remote::Frame(venue, frame)).await?;
                } else if matches!(msg, Message::Close(_)) {
                    return Err("private stream closed".into());
                }
            }
        }
    }
}

fn protected_bytes(path: &Path, private: bool, operator_owned: bool) -> Result<Vec<u8>> {
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || (operator_owned && meta.uid() != 0)
        || meta.mode() & if private { 0o077 } else { 0o022 } != 0
        || meta.len() > 65_536
    {
        return Err("unsafe canary configuration or credential file".into());
    }
    let mut data = Vec::new();
    file.take(65_537).read_to_end(&mut data)?;
    if data.len() > 65_536 {
        return Err("canary input size limit".into());
    }
    Ok(data)
}

fn setup(config: &Config, live: bool) -> Result<BTreeMap<Venue, Arc<Api>>> {
    let mut apis = BTreeMap::new();
    for venue in &config.venues {
        let (id, key) = if live {
            let id = venue.key_id.clone().filter(|s| !s.is_empty()).ok_or("missing live key ID")?;
            let pem = protected_bytes(venue.key_file.as_deref().ok_or("missing live key file")?, true, false)?;
            let encoded = std::str::from_utf8(&pem)?.trim();
            let key = if encoded.starts_with("-----BEGIN") {
                SigningKey::from_pkcs8_pem(encoded).map_err(|_| "invalid Ed25519 PEM")?
            } else {
                let raw = STANDARD.decode(encoded).map_err(|_| "invalid Ed25519 key encoding")?;
                if !matches!(raw.len(), 32 | 64) {
                    return Err("invalid Ed25519 seed length".into());
                }
                let seed: [u8; 32] = raw[..32].try_into()?;
                let key = SigningKey::from_bytes(&seed);
                if raw.len() == 64 && raw[32..] != key.verifying_key().to_bytes() {
                    return Err("Ed25519 seed and public half disagree".into());
                }
                key
            };
            (id, key)
        } else {
            ("offline-fixture".to_owned(), SigningKey::from_bytes(&[42; 32]))
        };
        let api = match venue.venue {
            Venue::Pmus => Api::Pmus(Pmus::new(id, key)),
            Venue::Kalshi => Api::Kalshi(Kalshi::new(id, key)),
        };
        if apis.insert(venue.venue, Arc::new(api)).is_some() {
            return Err("duplicate venue".into());
        }
    }
    Ok(apis)
}

fn apply_input(oms: &mut Oms, item: Input, now: u64, live: bool, sweep_clock: &mut u64) -> Result<()> {
    match item {
        Input::Signal { signal } => {
            oms.signal(signal, now);
        }
        Input::Signals { signals } => {
            if signals.len() > 256 {
                return Err("too many signals in one frame".into());
            }
            for signal in signals {
                oms.signal(signal, now);
            }
        }
        Input::Book { market, book } => {
            oms.book(market, book, now)?;
        }
        Input::Feed { feed, received_ns } => {
            oms.feed(feed, received_ns, now);
        }
        Input::Watchdog { genome_id, state, ts_ns } => oms.watchdog(genome_id, state, ts_ns, now)?,
        Input::BinanceSweep { active, ts_ns } => {
            if ts_ns > *sweep_clock && ts_ns <= now {
                *sweep_clock = ts_ns;
                oms.set_sweep(active);
            }
        }
        Input::MockRtt { venue, rtt_ms } if !live => {
            oms.rtt(venue, rtt_ms, now);
        }
        Input::MockRtt { .. } => return Err("simulated RTT in live input".into()),
    }
    Ok(())
}

async fn dispatch(
    oms: &mut Oms,
    action: Action,
    client: &Client,
    api: &Api,
    live: bool,
    rate: &mut RateLimit,
    kalshi_quiet_until: &mut u64,
) -> Result<()> {
    let venue = match &action {
        Action::Submit(o) => o.market.venue,
        Action::Cancel { market, .. } => market.venue,
        Action::CancelAll { venue } => *venue,
    };
    let now = wall_ns();
    let allows_dispatch = !live
        || venue != Venue::Kalshi
        || rate.allow(
            now / MILLIS,
            match action {
                Action::Submit(_) => kalshi::CREATE_COST,
                _ => kalshi::CANCEL_COST,
            },
        );
    if !allows_dispatch || (venue == Venue::Kalshi && now < *kalshi_quiet_until && matches!(action, Action::Submit(_))) {
        if let Action::Submit(order) = action {
            oms.reject_submission(&order.client_id, now)?;
        }
        return Ok(());
    }
    let request = api.request(&action, now / MILLIS)?;
    if let Err(err) = oms.request(&action, request.method, &request.path, request.body.as_ref(), wall_ns()) {
        // A freshness refusal never reached the wire. Ledger failures terminate the loop.
        if err.to_string() == "entry circuit opened before submit" {
            return Ok(());
        }
        return Err(err);
    }
    if !live {
        return match action {
            Action::Submit(o) => {
                let venue_id = format!("dry-{}", o.client_id);
                oms.event(venue, WireEvent::Ack { client_id: o.client_id, venue_id: venue_id.clone() }, wall_ns())?;
                oms.event(venue, WireEvent::Terminal { venue_id, filled: 0 }, wall_ns())
            }
            _ => oms.cancellation_ack(&action, wall_ns()),
        };
    }
    let reply = match transport(client, api, request).await {
        Ok(reply) => reply,
        Err(_) => {
            oms.rtt(venue, MAX_API_RTT_MS + 1, wall_ns());
            return oms.fault("ambiguous_transport_outcome", wall_ns());
        }
    };
    let now = wall_ns();
    oms.rtt(venue, reply.elapsed_ms, now);
    if venue == Venue::Kalshi {
        rate.observe(reply.status, &reply.headers, now / MILLIS);
    }
    if !(200..300).contains(&reply.status) {
        match &action {
            Action::Submit(order) if matches!(reply.status, 400 | 401 | 403 | 422 | 429) => {
                oms.reject_submission(&order.client_id, now)?;
                oms.rtt(venue, MAX_API_RTT_MS + 1, now);
            }
            _ if reply.status == 429 => {
                oms.rtt(venue, MAX_API_RTT_MS + 1, now);
            }
            _ => oms.fault("ambiguous_venue_outcome", now)?,
        }
        return Ok(());
    }
    let events = match (&action, api) {
        (Action::Submit(o), Api::Pmus(p)) => p.ack(&reply.data, &o.client_id).map(|e| vec![e]),
        (Action::Submit(o), Api::Kalshi(k)) => k.submit_events(&reply.data, o),
        (Action::Cancel { venue_id, .. }, Api::Kalshi(k)) => match oms.working_order(venue, venue_id) {
            Some(o) => k.cancel_event(&reply.data, venue_id, o).map(|e| vec![e]),
            None => Ok(vec![]),
        },
        _ => Ok(vec![]),
    };
    match events {
        Ok(events) => {
            for event in events {
                oms.event(venue, event, now)?;
            }
        }
        Err(_) => return oms.fault("invalid_order_response", now),
    }
    if !matches!(action, Action::Submit(_)) {
        if venue == Venue::Kalshi && matches!(action, Action::CancelAll { .. }) {
            *kalshi_quiet_until = now.saturating_add(60_000 * MILLIS);
        }
        oms.cancellation_ack(&action, now)?;
    }
    Ok(())
}

async fn run(config: Config, live: bool, reset: bool) -> Result<()> {
    let apis = setup(&config, live)?;
    let client =
        Client::builder().timeout(Duration::from_millis(MAX_API_RTT_MS)).redirect(reqwest::redirect::Policy::none()).no_proxy().build()?;
    let approval = if live {
        let files = config.approval.as_ref().ok_or("live approval is required")?;
        let bytes: [u8; 32] = hex::decode(&files.trusted_public_key_hex)?.try_into().map_err(|_| "invalid approval key")?;
        let approval = Approval::verify(
            &protected_bytes(&files.manifest, false, false)?,
            &protected_bytes(&files.signature, false, false)?,
            &VerifyingKey::from_bytes(&bytes)?,
            wall_ns(),
        )?;
        if !approval.matches_account(&config.ledger, config.venues.iter().filter_map(|v| v.key_id.as_deref().map(|key| (v.venue, key)))) {
            return Err("approval does not match the configured account keys or ledger".into());
        }
        Some(approval)
    } else {
        None
    };
    let mut oms = Oms::open(&config.ledger, config.risk, approval, apis.keys().copied().collect(), wall_ns())?;
    let (remote_tx, mut remote_rx) = mpsc::channel(256);
    let (probe_tx, mut probe_rx) = mpsc::channel(8);
    let mut tasks = Vec::new();
    let mut quiet_until = 0;
    for (&venue, api) in &apis {
        if !live {
            oms.venue_ready(venue, true);
            continue;
        }
        let orders_request = api.open_orders(wall_ns() / MILLIS)?;
        if venue == Venue::Kalshi {
            oms.request(
                &Action::CancelAll { venue },
                orders_request.method,
                &orders_request.path,
                orders_request.body.as_ref(),
                wall_ns(),
            )?;
        }
        let orders = transport(&client, api, orders_request).await?;
        let positions = transport(&client, api, api.positions(wall_ns() / MILLIS)).await?;
        if venue == Venue::Kalshi && (200..300).contains(&orders.status) {
            quiet_until = wall_ns().saturating_add(60_000 * MILLIS);
            oms.cancellation_ack(&Action::CancelAll { venue }, wall_ns())?;
        }
        if !(200..300).contains(&positions.status) || !(200..300).contains(&orders.status) || !api.flat(&positions.data, &orders.data) {
            return Err("account startup reconciliation requires zero positions and resting orders".into());
        }
        oms.rtt(venue, positions.elapsed_ms.max(orders.elapsed_ms), wall_ns());
        let signer = api.clone();
        let tx = remote_tx.clone();
        tasks.push(tokio::spawn(async move {
            let _ = socket(venue, signer, tx.clone()).await;
            let _ = tx.send(Remote::Disconnected(venue)).await;
        }));
        let signer = api.clone();
        let http = client.clone();
        let tx = probe_tx.clone();
        tasks.push(tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(1));
            loop {
                interval.tick().await;
                let ms = match transport(&http, &signer, signer.positions(wall_ns() / MILLIS)).await {
                    Ok(r) if (200..300).contains(&r.status) => r.elapsed_ms,
                    _ => MAX_API_RTT_MS + 1,
                };
                if tx.send((venue, ms, wall_ns())).await.is_err() {
                    break;
                }
            }
        }));
    }
    let (tx, mut input) = mpsc::channel(256);
    std::thread::spawn(move || {
        let mut reader = std::io::stdin().lock();
        loop {
            let mut line = Vec::new();
            match (&mut reader).take(65_537).read_until(b'\n', &mut line) {
                Ok(0) => break,
                Ok(_) if line.len() <= 65_536 => {
                    if tx.blocking_send(serde_json::from_slice::<Input>(&line).map_err(|_| ())).is_err() {
                        break;
                    }
                }
                _ => {
                    let _ = tx.blocking_send(Err(()));
                    break;
                }
            }
        }
    });
    let mut timer = tokio::time::interval(Duration::from_millis(25));
    timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut rate = RateLimit::new();
    let mut sweep_clock = 0;
    let mut ready_venues = BTreeSet::new();
    let mut reset_pending = reset;
    let signal = tokio::signal::ctrl_c();
    tokio::pin!(signal);
    let result: Result<()> = async {
        loop {
            tokio::select! {
                biased;
                _ = &mut signal => break,
                Some(message) = remote_rx.recv(), if live => match message {
                    Remote::Ready(venue) => { oms.venue_ready(venue, true); ready_venues.insert(venue); }
                    Remote::Disconnected(venue) => {
                        oms.venue_ready(venue, false);
                        oms.fault("private_stream_disconnected", wall_ns())?;
                    }
                    Remote::Frame(venue, frame) => match apis[&venue].events(&frame) {
                        Ok(events) => for event in events { oms.event(venue, event, wall_ns())?; },
                        Err(_) => oms.fault("unreconciled_private_frame", wall_ns())?,
                    }
                },
                Some((venue, rtt, now)) = probe_rx.recv(), if live => oms.rtt(venue, rtt, now),
                _ = timer.tick() => {},
                item = input.recv() => match item {
                    Some(Ok(item)) => apply_input(&mut oms, item, wall_ns(), live, &mut sweep_clock)?,
                    Some(Err(())) => return Err("invalid execution input".into()),
                    None => break,
                }
            }
            if reset_pending && ready_venues.len() == apis.len() {
                oms.reset_daily_stop(wall_ns())?;
                reset_pending = false;
            }
            if let Some(action) = oms.next(wall_ns())? {
                let venue = match &action {
                    Action::Submit(o) => o.market.venue,
                    Action::Cancel { market, .. } => market.venue,
                    Action::CancelAll { venue } => *venue,
                };
                dispatch(&mut oms, action, &client, &apis[&venue], live, &mut rate, &mut quiet_until).await?;
            }
        }
        Ok(())
    }
    .await;
    if live {
        // Entry transport is stopped. Best-effort venue cancellation also covers ledger I/O failure.
        for api in apis.values() {
            let action = Action::CancelAll {
                venue: match api.as_ref() {
                    Api::Pmus(_) => Venue::Pmus,
                    Api::Kalshi(_) => Venue::Kalshi,
                },
            };
            if let Ok(request) = api.request(&action, wall_ns() / MILLIS) {
                let _ = transport(&client, api, request).await;
            }
        }
    }
    for task in tasks {
        task.abort();
    }
    result
}

#[tokio::main]
async fn main() {
    if let Err(err) = main_result().await {
        eprintln!("doppel-exec: {err}");
        std::process::exit(2);
    }
}

async fn main_result() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--help"] {
        println!("doppel-exec --mode dry-run|canary-live --config config.toml [--reset-daily-stop]");
        return Ok(());
    }
    if !(4..=5).contains(&args.len()) || args[0] != "--mode" || args[2] != "--config" {
        return Err("expected --mode dry-run|canary-live --config config.toml".into());
    }
    let live = match args[1].as_str() {
        "dry-run" => false,
        "canary-live" => true,
        _ => return Err("unknown execution mode".into()),
    };
    let reset = args.get(4).is_some_and(|v| v == "--reset-daily-stop" && live);
    if args.len() == 5 && !reset {
        return Err("unknown or unauthorized execution switch".into());
    }
    let config = if live {
        let path = Path::new(&args[3]);
        let bytes = protected_bytes(path, false, true)?;
        toml::from_str(std::str::from_utf8(&bytes)?)?
    } else {
        toml::from_str(&fs::read_to_string(&args[3])?)?
    };
    run(config, live, reset).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use doppel_exec::{risk::Level, Kind};

    #[tokio::test]
    async fn dry_run_records_request_and_uses_no_live_transport() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("canary.jsonl");
        let now = wall_ns();
        let mut oms = Oms::open(&path, Limits::default(), None, [Venue::Pmus].into(), now).unwrap();
        let market = Market { venue: Venue::Pmus, id: "fixture-btc-up".into() };
        oms.venue_ready(Venue::Pmus, true);
        oms.rtt(Venue::Pmus, 5, now);
        for feed in [Feed::Brti, Feed::Binance, Feed::Hyperliquid] {
            assert!(oms.feed(feed, now, now));
        }
        assert!(oms
            .book(
                market.clone(),
                Book {
                    received_ns: now,
                    bids: vec![Level { price_bps: 4900, quantity: 40 }],
                    asks: vec![Level { price_bps: 5100, quantity: 40 }]
                },
                now
            )
            .unwrap());
        assert!(oms.signal(
            Signal {
                genome_id: "fixture".into(),
                market,
                p_hat: 0.8,
                epistemic_var: 0.,
                target_contracts: 3,
                shadow_expected_price_bps: Some(5100),
                kind: Kind::Taker,
                ts_ns: now
            },
            now
        ));
        let action = oms.next(now).unwrap().unwrap();
        let api = Api::Pmus(Pmus::new("fixture-id-not-logged".into(), SigningKey::from_bytes(&[42; 32])));
        dispatch(&mut oms, action, &Client::new(), &api, false, &mut RateLimit::new(), &mut 0).await.unwrap();
        let raw = fs::read_to_string(path).unwrap();
        assert!(!raw.contains("fixture-id-not-logged"));
        assert!(!raw.contains("X-PM-Signature"));
        let request = raw
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .find(|row| row["payload"]["event"]["type"] == "request")
            .unwrap();
        let event = &request["payload"]["event"];
        assert_eq!(event["path"], "/v1/orders");
        assert_eq!(event["body"]["marketSlug"], "fixture-btc-up");
        assert_eq!(event["body"]["quantity"], 3);
        assert!(!raw.contains("\"type\":\"fill\""));
    }
}
