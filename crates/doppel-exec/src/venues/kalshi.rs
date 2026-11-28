//! Kalshi event-contract V2 order wire format, private stream, and write budget.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use ed25519_dalek::{Signer as _, SigningKey};
use reqwest::header::HeaderMap;
use serde_json::{json, Value};

use super::Request;
use crate::{Effect, Kind, Order, Result, Side, Venue, WireEvent};

pub const REST_ORIGIN: &str = "https://external-api.kalshi.com";
pub const WS_URL: &str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2";
const WS_PATH: &str = "/trade-api/ws/v2";
const ORDERS: &str = "/trade-api/v2/portfolio/events/orders";
pub const CREATE_COST: u32 = 10;
pub const CANCEL_COST: u32 = 2;

pub struct Kalshi {
    key_id: String,
    key: SigningKey,
}

impl Kalshi {
    pub fn new(key_id: String, key: SigningKey) -> Self {
        Self { key_id, key }
    }

    fn headers(&self, method: &str, path: &str, ts_ms: u64) -> Vec<(String, String)> {
        let path_only = path.split_once('?').map_or(path, |(p, _)| p);
        let signature = self.key.sign(format!("{ts_ms}{method}{path_only}").as_bytes());
        vec![
            ("KALSHI-ACCESS-KEY".into(), self.key_id.clone()),
            ("KALSHI-ACCESS-TIMESTAMP".into(), ts_ms.to_string()),
            ("KALSHI-ACCESS-SIGNATURE".into(), STANDARD.encode(signature.to_bytes())),
        ]
    }

    fn request(&self, method: &'static str, path: String, body: Option<Value>, ts_ms: u64) -> Request {
        Request { method, headers: self.headers(method, &path, ts_ms), path, body }
    }

    pub fn submit(&self, order: &Order, ts_ms: u64) -> Result<Request> {
        Ok(self.request("POST", ORDERS.into(), Some(order_body(order)?), ts_ms))
    }

    pub fn batch_submit(&self, orders: &[Order], ts_ms: u64) -> Result<Request> {
        if orders.is_empty() {
            return Err("empty Kalshi order batch".into());
        }
        let mut ids = std::collections::BTreeSet::new();
        let mut items = Vec::with_capacity(orders.len());
        for order in orders {
            if !ids.insert(&order.client_id) {
                return Err("duplicate Kalshi client order ID in batch".into());
            }
            items.push(order_body(order)?);
        }
        Ok(self.request("POST", format!("{ORDERS}/batched"), Some(json!({"orders": items})), ts_ms))
    }

    pub fn cancel(&self, market_id: &str, venue_id: &str, ts_ms: u64) -> Result<Request> {
        if !route_component(market_id) || !route_component(venue_id) {
            return Err("invalid Kalshi order or market ID".into());
        }
        Ok(self.request("DELETE", format!("{ORDERS}/{venue_id}?market_ticker={market_id}"), None, ts_ms))
    }

    /// Primary subaccount only, matching the account used to submit orders.
    pub fn cancel_all(&self, ts_ms: u64) -> Result<Request> {
        Ok(self.request("DELETE", format!("{ORDERS}?subaccount=0"), None, ts_ms))
    }

    pub fn positions(&self, ts_ms: u64) -> Request {
        self.request("GET", "/trade-api/v2/portfolio/positions?subaccount=0".into(), None, ts_ms)
    }

    pub fn ws_headers(&self, ts_ms: u64) -> Vec<(String, String)> {
        self.headers("GET", WS_PATH, ts_ms)
    }

    pub fn subscription(&self) -> Value {
        json!({"id": 1, "cmd": "subscribe", "params": {"channels": ["fill", "user_orders"]}})
    }

    pub fn ack(&self, body: &Value, client_id: &str) -> Result<WireEvent> {
        if optional_id(body, "client_order_id")?.is_some_and(|value| value != client_id) {
            return Err("Kalshi response client order ID mismatch".into());
        }
        Ok(WireEvent::Ack { client_id: client_id.into(), venue_id: required_id(body, "order_id")?.into() })
    }

    /// REST reports cumulative quantities. Individual executions are recorded from the fill stream.
    pub fn submit_events(&self, body: &Value, order: &Order) -> Result<Vec<WireEvent>> {
        let ack = self.ack(body, &order.client_id)?;
        let venue_id = required_id(body, "order_id")?;
        let filled = contracts(body, "fill_count")?;
        let remaining = contracts(body, "remaining_count")?;
        if u64::from(filled) + u64::from(remaining) > u64::from(order.quantity) || (order.kind == Kind::Taker && remaining != 0) {
            return Err("invalid Kalshi order response quantity".into());
        }
        let mut events = vec![ack];
        if remaining == 0 {
            events.push(WireEvent::Terminal { venue_id: venue_id.into(), filled });
        }
        Ok(events)
    }

    /// Single-order cancel returns the exact removed remainder. This confirms the cumulative
    /// fill count expected from the private stream even when a fill raced the cancellation.
    pub fn cancel_event(&self, body: &Value, venue_id: &str, order: &Order) -> Result<WireEvent> {
        if required_id(body, "order_id")? != venue_id || optional_id(body, "client_order_id")?.is_some_and(|id| id != order.client_id) {
            return Err("Kalshi cancel response identity mismatch".into());
        }
        let reduced = contracts(body, "reduced_by")?;
        let filled = order.quantity.checked_sub(reduced).ok_or("Kalshi cancel response exceeds ordered quantity")?;
        Ok(WireEvent::Terminal { venue_id: venue_id.into(), filled })
    }

    pub fn events(&self, body: &Value) -> Result<Vec<WireEvent>> {
        match body["type"].as_str() {
            Some("fill") => Ok(vec![fill_event(&body["msg"])?]),
            Some("user_order") => order_events(&body["msg"]),
            Some("subscribed" | "unsubscribed" | "ok" | "heartbeat") => Ok(Vec::new()),
            Some("error") => Err("Kalshi private subscription error".into()),
            _ => Err("unexpected Kalshi private stream event".into()),
        }
    }
}

fn route_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value != "."
        && value != ".."
        && value.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_' | b'.'))
}

fn order_body(order: &Order) -> Result<Value> {
    if order.market.venue != Venue::Kalshi
        || !route_component(&order.market.id)
        || order.client_id.is_empty()
        || order.client_id.len() > 64
        || !order.client_id.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-' | b':' | b'+' | b'=' | b'/'))
        || order.quantity == 0
        || !(1..10_000).contains(&order.price_bps)
    {
        return Err("invalid Kalshi event order".into());
    }
    let maker = order.kind == Kind::Maker;
    let reduce_only = order.effect == Effect::Close;
    if reduce_only && maker {
        return Err("Kalshi reduce-only orders require immediate-or-cancel".into());
    }
    let side = match order.side {
        Side::BuyYes => "bid",
        Side::SellYes => "ask",
    };
    Ok(json!({
        "ticker": order.market.id,
        "client_order_id": order.client_id,
        "side": side,
        "count": format!("{}.00", order.quantity),
        "price": format!("0.{:04}", order.price_bps),
        "time_in_force": if maker { "good_till_canceled" } else { "immediate_or_cancel" },
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": maker,
        "cancel_order_on_pause": true,
        "reduce_only": reduce_only,
    }))
}

fn required_id<'a>(body: &'a Value, name: &str) -> Result<&'a str> {
    body[name].as_str().filter(|s| !s.is_empty()).ok_or_else(|| format!("missing Kalshi {name}").into())
}

fn optional_id<'a>(body: &'a Value, name: &str) -> Result<Option<&'a str>> {
    match &body[name] {
        Value::Null => Ok(None),
        Value::String(id) if id.is_empty() => Ok(None),
        Value::String(id) => Ok(Some(id)),
        _ => Err(format!("invalid Kalshi {name}").into()),
    }
}

fn fixed(body: &Value, name: &str, digits: u32) -> Result<u64> {
    let invalid = || format!("invalid Kalshi fixed-point {name}");
    let raw = body[name].as_str().ok_or_else(invalid)?;
    let (whole, fraction) = raw.split_once('.').unwrap_or((raw, ""));
    if whole.is_empty()
        || !whole.bytes().all(|c| c.is_ascii_digit())
        || !fraction.bytes().all(|c| c.is_ascii_digit())
        || fraction.len() > digits as usize
        || (fraction.is_empty() && raw.ends_with('.'))
    {
        return Err(invalid().into());
    }
    let whole = whole.parse::<u64>()?;
    let fraction = if fraction.is_empty() { 0 } else { fraction.parse::<u64>()? * 10_u64.pow(digits - fraction.len() as u32) };
    whole.checked_mul(10_u64.pow(digits)).and_then(|n| n.checked_add(fraction)).ok_or_else(|| invalid().into())
}

fn contracts(body: &Value, name: &str) -> Result<u32> {
    let count = fixed(body, name, 2)?;
    if !count.is_multiple_of(100) {
        return Err("Kalshi fractional execution requires reconciliation outside whole-contract OMS".into());
    }
    Ok(u32::try_from(count / 100)?)
}

fn fill_event(msg: &Value) -> Result<WireEvent> {
    let quantity = contracts(msg, "count_fp")?;
    let price = fixed(msg, "yes_price_dollars", 4)?;
    let ts_ns =
        msg["ts_ms"].as_u64().filter(|ts| *ts > 0).and_then(|ts| ts.checked_mul(1_000_000)).ok_or("invalid Kalshi fill timestamp")?;
    if quantity == 0 || price > 10_000 {
        return Err("invalid Kalshi fill size or price".into());
    }
    Ok(WireEvent::Fill {
        id: required_id(msg, "trade_id")?.into(),
        venue_id: required_id(msg, "order_id")?.into(),
        client_id: optional_id(msg, "client_order_id")?.map(str::to_owned),
        market_id: required_id(msg, "market_ticker")?.into(),
        quantity,
        price_bps: price as u16,
        fee_micros: Some(fixed(msg, "fee_cost", 6)?),
        ts_ns,
    })
}

fn order_events(msg: &Value) -> Result<Vec<WireEvent>> {
    let venue_id = required_id(msg, "order_id")?;
    let mut events = Vec::new();
    if let Some(client_id) = optional_id(msg, "client_order_id")? {
        events.push(WireEvent::Ack { client_id: client_id.into(), venue_id: venue_id.into() });
    }
    match msg["status"].as_str() {
        Some("resting" | "pending") => {}
        Some("canceled" | "executed") => {
            events.push(WireEvent::Terminal { venue_id: venue_id.into(), filled: contracts(msg, "fill_count_fp")? })
        }
        _ => return Err("unexpected Kalshi order state".into()),
    }
    Ok(events)
}

/// Conservative shared write bucket. Default to the Basic tier and reserve one cancellation.
/// Kalshi headers may refer to different shard buckets so they can lower this limit only.
pub struct RateLimit {
    tokens_milli: u64,
    capacity: u64,
    refill_per_second: u64,
    last_ms: Option<u64>,
    retry_at_ms: u64,
    limited: u32,
}

impl Default for RateLimit {
    fn default() -> Self {
        Self::new()
    }
}

impl RateLimit {
    pub fn new() -> Self {
        Self { tokens_milli: 100_000, capacity: 100, refill_per_second: 100, last_ms: None, retry_at_ms: 0, limited: 0 }
    }

    fn refill(&mut self, now_ms: u64) -> bool {
        let last = self.last_ms.unwrap_or(now_ms);
        let Some(elapsed) = now_ms.checked_sub(last) else {
            return false;
        };
        self.tokens_milli = self.tokens_milli.saturating_add(elapsed.saturating_mul(self.refill_per_second)).min(self.capacity * 1_000);
        self.last_ms = Some(now_ms);
        true
    }

    /// Charge a known venue write cost. A create or batch cannot spend the cancel reserve.
    pub fn allow(&mut self, now_ms: u64, cost: u32) -> bool {
        if !self.refill(now_ms) || cost == 0 || now_ms < self.retry_at_ms {
            return false;
        }
        let charge = u64::from(cost) * 1_000;
        let reserve = u64::from(if cost > CANCEL_COST { CANCEL_COST } else { 0 }) * 1_000;
        if self.tokens_milli < charge + reserve {
            return false;
        }
        self.tokens_milli -= charge;
        true
    }

    pub fn observe(&mut self, status: u16, headers: &HeaderMap, now_ms: u64) {
        if !self.refill(now_ms) {
            return;
        }
        let integer = |name: &str| headers.get(name)?.to_str().ok()?.parse::<u64>().ok();
        let limit = integer("X-RateLimit-Limit").filter(|v| *v > 0);
        let remaining = integer("X-RateLimit-Remaining");
        // Kalshi gives relative seconds to a fully refilled bucket, with subsecond precision.
        let reset = headers
            .get("X-RateLimit-Reset")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|v| v.is_finite() && *v >= 0.0)
            .map(|v| (v * 1_000.0).ceil() as u64);
        if let Some(capacity) = limit {
            self.capacity = self.capacity.min(capacity);
        }
        if let (Some(capacity), Some(left), Some(ms)) = (limit, remaining, reset) {
            if left < capacity && ms > 0 {
                let inferred = capacity.saturating_sub(left).saturating_mul(1_000) / ms;
                self.refill_per_second = self.refill_per_second.min(inferred.max(1));
            }
        }
        self.tokens_milli = self.tokens_milli.min(remaining.unwrap_or(self.capacity).min(self.capacity) * 1_000);
        if status == 429 {
            let backoff = 100 * (1_u64 << self.limited);
            self.limited = self.limited.saturating_add(1).min(6);
            self.retry_at_ms = self.retry_at_ms.max(now_ms.saturating_add(backoff.max(reset.unwrap_or(0))));
        } else if (200..300).contains(&status) {
            self.limited = 0;
        }
    }
}

#[cfg(test)]
mod tests {
    // Tungstenite fixes the callback's HTTP response error type.
    #![allow(clippy::result_large_err)]

    use std::time::Duration;

    use ed25519_dalek::{Signature, VerifyingKey};
    use futures_util::{SinkExt as _, StreamExt as _};
    use tokio::io::{AsyncBufReadExt as _, AsyncReadExt as _, AsyncWriteExt as _, BufReader};
    use tokio::net::{TcpListener, TcpStream};
    use tokio_tungstenite::tungstenite::client::IntoClientRequest as _;
    use tokio_tungstenite::tungstenite::handshake::server;
    use tokio_tungstenite::tungstenite::Message;

    use super::*;
    use crate::Market;

    const T: u64 = 1_800_000_000_000;

    fn keys() -> (Kalshi, VerifyingKey) {
        let key = SigningKey::from_bytes(&[7; 32]);
        let public = key.verifying_key();
        (Kalshi::new("fixture-key".into(), key), public)
    }

    fn order() -> Order {
        Order {
            market: Market { venue: Venue::Kalshi, id: "KXBTC15M-FIXTURE".into() },
            client_id: "test-order-0001".into(),
            side: Side::BuyYes,
            effect: Effect::Open,
            kind: Kind::Maker,
            quantity: 3,
            price_bps: 5_631,
            shadow_price_bps: 5_600,
            intent_ts_ns: T * 1_000_000,
        }
    }

    fn verify(headers: &HeaderMap, public: &VerifyingKey, method: &str, path: &str) {
        assert_eq!(headers["kalshi-access-key"], "fixture-key");
        let timestamp = headers["kalshi-access-timestamp"].to_str().unwrap();
        let signature = STANDARD.decode(headers["kalshi-access-signature"].as_bytes()).unwrap();
        let signature = Signature::from_slice(&signature).unwrap();
        let path_only = path.split_once('?').map_or(path, |(p, _)| p);
        public.verify_strict(format!("{timestamp}{method}{path_only}").as_bytes(), &signature).unwrap();
    }

    async fn read_request(stream: &mut TcpStream) -> (String, String, HeaderMap, Option<Value>) {
        let mut reader = BufReader::new(stream);
        let mut first = String::new();
        reader.read_line(&mut first).await.unwrap();
        let mut parts = first.split_ascii_whitespace();
        let method = parts.next().unwrap().to_owned();
        let path = parts.next().unwrap().to_owned();
        let mut headers = HeaderMap::new();
        loop {
            let mut line = String::new();
            assert!(reader.read_line(&mut line).await.unwrap() > 0);
            if line == "\r\n" {
                break;
            }
            let (name, value) = line.split_once(':').unwrap();
            headers.insert(reqwest::header::HeaderName::from_bytes(name.as_bytes()).unwrap(), value.trim().parse().unwrap());
        }
        let size: usize = headers.get("content-length").map_or(0, |size| size.to_str().unwrap().parse().unwrap());
        let mut body = vec![0; size];
        reader.read_exact(&mut body).await.unwrap();
        (method, path, headers, (!body.is_empty()).then(|| serde_json::from_slice(&body).unwrap()))
    }

    async fn send(client: &reqwest::Client, base: &str, req: Request) -> reqwest::Response {
        let mut request = client.request(req.method.parse().unwrap(), format!("{base}{}", req.path));
        for (name, value) in req.headers {
            request = request.header(name, value);
        }
        if let Some(body) = req.body {
            request = request.header("Content-Type", "application/json").body(body.to_string());
        }
        request.send().await.unwrap()
    }

    #[tokio::test]
    async fn kalshi_v2_http_signing_lifecycle_and_429_backoff() {
        let (kalshi, public) = keys();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            for step in 0..4 {
                let (mut stream, _) = listener.accept().await.unwrap();
                let (method, path, headers, body) = read_request(&mut stream).await;
                verify(&headers, &public, &method, &path);
                let (status, response) = match step {
                    0 | 1 => {
                        assert_eq!((method.as_str(), path.as_str()), ("POST", ORDERS));
                        let body = body.unwrap();
                        assert_eq!(body["side"], "bid");
                        assert_eq!(body["price"], "0.5631");
                        assert_eq!(body["count"], "3.00");
                        assert_eq!(body["post_only"].as_bool(), Some(true));
                        assert_eq!(body["self_trade_prevention_type"], "taker_at_cross");
                        if step == 0 {
                            ("429 Too Many Requests", json!({"error":{"code":"too_many_requests"}}))
                        } else {
                            (
                                "201 Created",
                                json!({"order_id":"venue-001", "client_order_id":"test-order-0001",
                                       "fill_count":"0.00", "remaining_count":"3.00", "ts_ms":T+250}),
                            )
                        }
                    }
                    2 => {
                        assert_eq!(method, "DELETE");
                        assert_eq!(path, format!("{ORDERS}/venue-001?market_ticker=KXBTC15M-FIXTURE"));
                        assert!(body.is_none());
                        ("200 OK", json!({"order_id":"venue-001","client_order_id":"test-order-0001","reduced_by":"2.00"}))
                    }
                    _ => {
                        assert_eq!(method, "POST");
                        assert_eq!(path, format!("{ORDERS}/batched"));
                        let body = body.unwrap();
                        assert_eq!(body["orders"][0]["side"], "ask");
                        assert_eq!(body["orders"][0]["time_in_force"], "immediate_or_cancel");
                        assert_eq!(body["orders"][0]["reduce_only"].as_bool(), Some(true));
                        ("201 Created", json!({"orders": []}))
                    }
                };
                let body = response.to_string();
                let limits =
                    if step == 0 { "X-RateLimit-Limit: 100\r\nX-RateLimit-Remaining: 0\r\nX-RateLimit-Reset: 0.250\r\n" } else { "" };
                let raw = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n{limits}\r\n{body}", body.len());
                stream.write_all(raw.as_bytes()).await.unwrap();
            }
        });
        let client = reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(3)).build().unwrap();
        let order = order();
        let mut budget = RateLimit::default();
        assert!(budget.allow(T, CREATE_COST));
        let rejected = send(&client, &base, kalshi.submit(&order, T).unwrap()).await;
        budget.observe(rejected.status().as_u16(), rejected.headers(), T);
        assert_eq!(rejected.status(), 429);
        assert!(!budget.allow(T + 249, CREATE_COST));
        assert!(budget.allow(T + 250, CREATE_COST));

        let accepted = send(&client, &base, kalshi.submit(&order, T + 250).unwrap()).await;
        let accepted = serde_json::from_slice(&accepted.bytes().await.unwrap()).unwrap();
        assert_eq!(
            kalshi.submit_events(&accepted, &order).unwrap(),
            vec![WireEvent::Ack { client_id: order.client_id.clone(), venue_id: "venue-001".into() }]
        );
        assert!(budget.allow(T + 251, CANCEL_COST));
        let cancel = kalshi.cancel(&order.market.id, "venue-001", T + 251).unwrap();
        let canceled = send(&client, &base, cancel).await;
        let canceled = serde_json::from_slice(&canceled.bytes().await.unwrap()).unwrap();
        assert_eq!(
            kalshi.cancel_event(&canceled, "venue-001", &order).unwrap(),
            WireEvent::Terminal { venue_id: "venue-001".into(), filled: 1 }
        );

        let closing = Order { side: Side::SellYes, effect: Effect::Close, kind: Kind::Taker, ..order };
        assert!(budget.allow(T + 1_000, CREATE_COST));
        let batch = kalshi.batch_submit(&[closing], T + 1_000).unwrap();
        assert_eq!(send(&client, &base, batch).await.status(), 201);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn kalshi_private_ws_authenticates_and_normalizes_fills() {
        let (kalshi, public) = keys();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut ws = tokio_tungstenite::accept_hdr_async(stream, |request: &server::Request, response: server::Response| {
                assert_eq!(request.uri().path(), WS_PATH);
                verify(request.headers(), &public, "GET", WS_PATH);
                Ok(response)
            })
            .await
            .unwrap();
            let sub = ws.next().await.unwrap().unwrap();
            let sub: Value = serde_json::from_str(sub.to_text().unwrap()).unwrap();
            assert_eq!(sub["params"]["channels"], json!(["fill", "user_orders"]));
            let messages = [
                json!({"type":"subscribed","msg":{"channel":"fill","sid":40}}),
                json!({"type":"user_order","msg":{"order_id":"venue-001","client_order_id":"test-order-0001",
                       "status":"resting","fill_count_fp":"0.00"}}),
                json!({"type":"fill","msg":{"order_id":"venue-001","client_order_id":"test-order-0001",
                       "trade_id":"fill-01","market_ticker":"KXBTC15M-FIXTURE",
                       "yes_price_dollars":"0.5631","count_fp":"3.00","fee_cost":"0.140000","ts_ms":T}}),
                json!({"type":"user_order","msg":{"order_id":"venue-001","status":"executed","fill_count_fp":"3.00"}}),
            ];
            for message in messages {
                ws.send(Message::Text(message.to_string().into())).await.unwrap();
            }
        });
        let mut handshake = format!("ws://{addr}{WS_PATH}").into_client_request().unwrap();
        for (key, value) in kalshi.ws_headers(T) {
            handshake.headers_mut().insert(reqwest::header::HeaderName::from_bytes(key.as_bytes()).unwrap(), value.parse().unwrap());
        }
        let (mut ws, _) = tokio::time::timeout(Duration::from_secs(3), tokio_tungstenite::connect_async(handshake)).await.unwrap().unwrap();
        ws.send(Message::Text(kalshi.subscription().to_string().into())).await.unwrap();
        let mut events = Vec::new();
        for _ in 0..4 {
            let msg = tokio::time::timeout(Duration::from_secs(3), ws.next()).await.unwrap().unwrap().unwrap();
            let value = serde_json::from_str(msg.to_text().unwrap()).unwrap();
            events.extend(kalshi.events(&value).unwrap());
        }
        assert_eq!(
            events,
            vec![
                WireEvent::Ack { client_id: "test-order-0001".into(), venue_id: "venue-001".into() },
                WireEvent::Fill {
                    id: "fill-01".into(),
                    venue_id: "venue-001".into(),
                    client_id: Some("test-order-0001".into()),
                    market_id: "KXBTC15M-FIXTURE".into(),
                    quantity: 3,
                    price_bps: 5_631,
                    fee_micros: Some(140_000),
                    ts_ns: T * 1_000_000,
                },
                WireEvent::Terminal { venue_id: "venue-001".into(), filled: 3 },
            ]
        );
        server.await.unwrap();
    }

    #[test]
    fn kalshi_rate_limit_reserves_cancel_tokens_and_exponentiates_without_headers() {
        let mut rate = RateLimit::new();
        for _ in 0..9 {
            assert!(rate.allow(T, CREATE_COST));
        }
        assert!(!rate.allow(T, CREATE_COST));
        assert!(rate.allow(T, CANCEL_COST));
        rate.observe(429, &HeaderMap::new(), T);
        assert!(!rate.allow(T + 99, CANCEL_COST));
        assert!(rate.allow(T + 100, CANCEL_COST));
        rate.observe(429, &HeaderMap::new(), T + 100);
        assert!(!rate.allow(T + 299, CREATE_COST));
        assert!(rate.allow(T + 300, CREATE_COST));
    }

    #[test]
    fn kalshi_refuses_unreconciled_fraction_and_maker_close() {
        let (kalshi, _) = keys();
        let mut order = order();
        order.effect = Effect::Close;
        assert!(kalshi.submit(&order, T).is_err());
        assert!(kalshi.cancel("KXBTC15M-FIXTURE", "../us", T).is_err());
        let body = json!({"type":"fill","msg":{"order_id":"venue-001", "trade_id":"trade-001",
                          "market_ticker":"KXBTC15M-FIXTURE", "ts_ms":T, "count_fp":"0.10",
                          "yes_price_dollars":"0.5611","fee_cost":"0.01"}});
        assert!(kalshi.events(&body).is_err());
        let cancel = kalshi.cancel_all(T).unwrap();
        assert_eq!(cancel.path, format!("{ORDERS}?subaccount=0"));
    }
}
