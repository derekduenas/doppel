//! Straight-order adapter for the Polymarket US authenticated REST and private order stream.

use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{json, Value};

use super::Request;
use crate::{Effect, Kind, Order, Result, Side, Venue, WireEvent};

pub const REST: &str = "https://api.polymarket.us";
pub const WS: &str = "wss://api.polymarket.us/v1/ws/private";
const WS_PATH: &str = "/v1/ws/private";

pub struct Pmus {
    key_id: String,
    key: SigningKey,
}

impl Pmus {
    pub fn new(key_id: String, key: SigningKey) -> Self {
        Self { key_id, key }
    }

    fn headers(&self, method: &str, path: &str, ts_ms: u64) -> Vec<(String, String)> {
        let signature = self.key.sign(format!("{ts_ms}{method}{path}").as_bytes());
        vec![
            ("X-PM-Access-Key".into(), self.key_id.clone()),
            ("X-PM-Timestamp".into(), ts_ms.to_string()),
            ("X-PM-Signature".into(), STANDARD.encode(signature.to_bytes())),
        ]
    }

    fn request(&self, method: &'static str, path: String, body: Option<Value>, ts_ms: u64) -> Request {
        let headers = self.headers(method, &path, ts_ms);
        Request { method, path, body, headers }
    }

    pub fn submit(&self, order: &Order, ts_ms: u64) -> Result<Request> {
        validate_order(order)?;
        let intent = match (order.side, order.effect) {
            (Side::BuyYes, Effect::Open) => "ORDER_INTENT_BUY_LONG",
            (Side::SellYes, Effect::Close) => "ORDER_INTENT_SELL_LONG",
            (Side::SellYes, Effect::Open) => "ORDER_INTENT_BUY_SHORT",
            (Side::BuyYes, Effect::Close) => "ORDER_INTENT_SELL_SHORT",
        };
        Ok(self.request(
            "POST",
            "/v1/orders".into(),
            Some(json!({
                "marketSlug": order.market.id,
                "type": "ORDER_TYPE_LIMIT",
                "price": price(order.price_bps),
                "quantity": order.quantity,
                "tif": time_in_force(order.kind),
                "participateDontInitiate": matches!(order.kind, Kind::Maker),
                "intent": intent,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "synchronousExecution": false,
            })),
            ts_ms,
        ))
    }

    pub fn cancel(&self, market_id: &str, venue_id: &str, ts_ms: u64) -> Result<Request> {
        validate_identifier(market_id)?;
        validate_identifier(venue_id)?;
        Ok(self.request("POST", format!("/v1/order/{venue_id}/cancel"), Some(json!({"marketSlug": market_id})), ts_ms))
    }

    /// The account used by Canary must be dedicated to its OMS.
    pub fn cancel_all(&self, ts_ms: u64) -> Result<Request> {
        Ok(self.request("POST", "/v1/orders/open/cancel".into(), Some(json!({})), ts_ms))
    }

    pub fn get_order(&self, venue_id: &str, ts_ms: u64) -> Result<Request> {
        validate_identifier(venue_id)?;
        Ok(self.request("GET", format!("/v1/order/{venue_id}"), None, ts_ms))
    }

    pub fn open_orders(&self, ts_ms: u64) -> Request {
        self.request("GET", "/v1/orders/open".into(), None, ts_ms)
    }

    pub fn positions(&self, ts_ms: u64) -> Request {
        self.request("GET", "/v1/portfolio/positions".into(), None, ts_ms)
    }

    /// For reconciliation tooling. The OMS replaces only after a verified cancel.
    /// `order.quantity` is the new total quantity, inclusive of fills on the original.
    pub fn modify(&self, venue_id: &str, order: &Order, ts_ms: u64) -> Result<Request> {
        validate_identifier(venue_id)?;
        validate_order(order)?;
        Ok(self.request(
            "POST",
            format!("/v1/order/{venue_id}/modify"),
            Some(json!({
                "marketSlug": order.market.id,
                "price": price(order.price_bps),
                "quantity": order.quantity,
                "tif": time_in_force(order.kind),
                "participateDontInitiate": matches!(order.kind, Kind::Maker),
            })),
            ts_ms,
        ))
    }

    pub fn ws_headers(&self, ts_ms: u64) -> Vec<(String, String)> {
        self.headers("GET", WS_PATH, ts_ms)
    }

    pub fn subscription(&self) -> Value {
        json!({"subscribe":{"requestId":"canary-orders","subscriptionType":"SUBSCRIPTION_TYPE_ORDER","marketSlugs":[]}})
    }

    /// REST assigns the venue ID. The async V1 payload does not carry a client ID.
    pub fn ack(&self, body: &Value, client_id: &str) -> Result<WireEvent> {
        let venue_id = required_str(body, "id")?;
        validate_identifier(venue_id)?;
        Ok(WireEvent::Ack { client_id: client_id.to_owned(), venue_id: venue_id.to_owned() })
    }

    pub fn events(&self, body: &Value) -> Result<Vec<WireEvent>> {
        let mut events = Vec::new();
        if !body.is_object() {
            return Err("PMUS private stream message is not an object".into());
        }
        if body.get("error").is_some_and(|value| !value.is_null()) {
            return Err("PMUS order stream returned an error".into());
        }
        if let Some(update) = body.get("orderSubscriptionUpdate") {
            execution_events(&update["execution"], &mut events)?;
        } else if let Some(executions) = body.get("executions") {
            for execution in executions.as_array().ok_or("invalid PMUS executions array")? {
                execution_events(execution, &mut events)?;
            }
        }
        Ok(events)
    }
}

fn execution_events(execution: &Value, events: &mut Vec<WireEvent>) -> Result<()> {
    let kind = required_str(execution, "type")?;
    let order = &execution["order"];
    let venue_id = required_str(order, "id")?;
    let state = required_str(order, "state")?;
    validate_identifier(venue_id)?;
    if matches!(kind, "EXECUTION_TYPE_PARTIAL_FILL" | "EXECUTION_TYPE_FILL") {
        let id = execution["tradeId"].as_str().filter(|id| !id.is_empty()).map_or_else(|| required_str(execution, "id"), Ok)?;
        let quantity = whole_shares(&execution["lastShares"])?;
        let price_bps = u16::try_from(usd_units(&execution["lastPx"], 4)?)?;
        if quantity == 0 || price_bps == 0 || price_bps >= 10_000 {
            return Err("invalid PMUS fill size or YES price".into());
        }
        let fee_micros = match execution.get("commissionNotionalCollected") {
            Some(value) if !value.is_null() => {
                // A maker credit is not a fee paid. Fee debits are preserved as micros.
                let amount = usd(value)?;
                let paid = if let Some(credit) = amount.strip_prefix('-') {
                    decimal(credit, 6)?;
                    0
                } else {
                    decimal(amount, 6)?
                };
                Some(paid)
            }
            _ => None,
        };
        events.push(WireEvent::Fill {
            id: id.to_owned(),
            venue_id: venue_id.to_owned(),
            client_id: None,
            market_id: required_str(order, "marketSlug")?.to_owned(),
            quantity,
            price_bps,
            fee_micros,
            ts_ns: timestamp_ns(required_str(execution, "transactTime")?)?,
        });
    }
    let terminal_state = matches!(state, "ORDER_STATE_FILLED" | "ORDER_STATE_CANCELED" | "ORDER_STATE_REJECTED" | "ORDER_STATE_EXPIRED");
    if terminal_state {
        events.push(WireEvent::Terminal { venue_id: venue_id.to_owned(), filled: whole_shares(&order["cumQuantity"])? });
    }
    Ok(())
}

fn validate_order(order: &Order) -> Result<()> {
    if !matches!(order.market.venue, Venue::Pmus) || order.quantity == 0 || !(1..10_000).contains(&order.price_bps) {
        return Err("invalid PMUS limit order".into());
    }
    validate_identifier(&order.market.id)
}

fn validate_identifier(value: &str) -> Result<()> {
    if value.is_empty()
        || value.len() > 256
        || matches!(value, "." | "..")
        || !value.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
    {
        return Err("invalid PMUS order identifier or market slug".into());
    }
    Ok(())
}

fn price(bps: u16) -> Value {
    json!({"value": format!("0.{bps:04}"), "currency": "USD"})
}

fn time_in_force(kind: Kind) -> &'static str {
    match kind {
        Kind::Maker => "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        Kind::Taker => "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    }
}

fn required_str<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key].as_str().filter(|value| !value.is_empty()).ok_or_else(|| format!("missing PMUS {key}").into())
}

fn usd(value: &Value) -> Result<&str> {
    if value["currency"].as_str() != Some("USD") {
        return Err("PMUS amount is not denominated in USD".into());
    }
    required_str(value, "value")
}

fn usd_units(value: &Value, digits: usize) -> Result<u64> {
    decimal(usd(value)?, digits)
}

fn whole_shares(value: &Value) -> Result<u32> {
    let units = match value {
        Value::String(text) => decimal(text, 0)?,
        Value::Number(number) => decimal(&number.to_string(), 0)?,
        _ => return Err("missing PMUS share quantity".into()),
    };
    Ok(units.try_into()?)
}

fn decimal(text: &str, digits: usize) -> Result<u64> {
    let (int, frac) = text.split_once('.').unwrap_or((text, ""));
    if int.is_empty()
        || !int.bytes().chain(frac.bytes()).all(|b| b.is_ascii_digit())
        || frac.as_bytes().iter().skip(digits).any(|&b| b != b'0')
    {
        return Err("PMUS decimal exceeds the supported precision".into());
    }
    let mut units = int.parse::<u64>()?;
    for i in 0..digits {
        let d = frac.as_bytes().get(i).copied().unwrap_or(b'0');
        units = units.checked_mul(10).and_then(|v| v.checked_add(u64::from(d - b'0'))).ok_or("PMUS decimal overflow")?;
    }
    Ok(units)
}

fn timestamp_ns(text: &str) -> Result<u64> {
    // The private stream uses UTC RFC 3339 including up to nanosecond precision.
    let number = |start, end| -> Result<u64> {
        let value = text.get(start..end).ok_or("invalid PMUS timestamp")?;
        if !value.bytes().all(|b| b.is_ascii_digit()) {
            return Err("invalid PMUS timestamp".into());
        }
        Ok(value.parse()?)
    };
    if !text.is_ascii()
        || !text.ends_with('Z')
        || text.len() < 20
        || text.as_bytes().get(4) != Some(&b'-')
        || text.as_bytes().get(7) != Some(&b'-')
        || text.as_bytes().get(10) != Some(&b'T')
        || text.as_bytes().get(13) != Some(&b':')
        || text.as_bytes().get(16) != Some(&b':')
    {
        return Err("invalid PMUS UTC timestamp".into());
    }
    let (year, month, day) = (number(0, 4)?, number(5, 7)?, number(8, 10)?);
    let (hour, min, sec) = (number(11, 13)?, number(14, 16)?, number(17, 19)?);
    let leap = |y: u64| y.is_multiple_of(4) && (!y.is_multiple_of(100) || y.is_multiple_of(400));
    if year < 1970 || !(1..=12).contains(&month) || hour > 23 || min > 59 || sec > 59 {
        return Err("invalid PMUS calendar time".into());
    }
    let lengths = [31, 28 + u64::from(leap(year)), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let month_index = month as usize - 1;
    if day == 0 || day > lengths[month_index] {
        return Err("invalid PMUS day".into());
    }
    let before = |y: u64| 365 * (y - 1) + (y - 1) / 4 - (y - 1) / 100 + (y - 1) / 400;
    let days = before(year) - before(1970) + lengths[..month_index].iter().sum::<u64>() + day - 1;
    let fraction = &text[19..text.len() - 1];
    let ns = if fraction.is_empty() {
        0
    } else {
        if !fraction.starts_with('.') || fraction.len() <= 1 || fraction.len() > 10 {
            return Err("invalid PMUS fractional timestamp".into());
        }
        decimal(&format!("0{fraction}"), 9)?
    };
    (days * 86_400 + hour * 3600 + min * 60 + sec)
        .checked_mul(1_000_000_000)
        .and_then(|value| value.checked_add(ns))
        .ok_or_else(|| "PMUS timestamp overflow".into())
}

#[cfg(test)]
mod tests {
    use ed25519_dalek::{Signature, VerifyingKey};
    use futures_util::{SinkExt, StreamExt};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
    };
    use tokio_tungstenite::{
        accept_hdr_async, connect_async,
        tungstenite::{client::IntoClientRequest, handshake::server, Message},
    };

    use super::*;
    use crate::Market;

    const TS: u64 = 1_795_814_400_000;

    fn key() -> SigningKey {
        SigningKey::from_bytes(&[7; 32])
    }

    fn credentials() -> Pmus {
        Pmus::new("fixture-pm-key".into(), key())
    }

    fn signed(key: &VerifyingKey, signature: &str, method: &str, path: &str) {
        let bytes = STANDARD.decode(signature).unwrap();
        let signature = Signature::from_slice(&bytes).unwrap();
        key.verify_strict(format!("{TS}{method}{path}").as_bytes(), &signature).unwrap();
    }

    fn order(side: Side, effect: Effect, kind: Kind) -> Order {
        Order {
            market: Market { venue: Venue::Pmus, id: "cpc-btc-fixture".into() },
            client_id: "local-42".into(),
            side,
            effect,
            kind,
            quantity: 4,
            price_bps: 4200,
            shadow_price_bps: 4202,
            intent_ts_ns: TS * 1_000_000,
        }
    }

    fn lifecycle(kind: &str, state: &str, filled: u32) -> Value {
        json!({"orderSubscriptionUpdate":{"execution":{
            "id":"execution-7", "tradeId":"trade-41", "type":kind,
            "order":{"id":"pm-order-42","marketSlug":"cpc-btc-fixture","state":state,"cumQuantity":filled},
            "lastShares":"2.00","lastPx":{"value":"0.425","currency":"USD"},
            "commissionNotionalCollected":{"value":"0.03","currency":"USD"},
            "transactTime":"2026-11-28T00:00:00.100000001Z"
        }}})
    }

    async fn receive_http(stream: &mut TcpStream) -> (String, Value) {
        let mut header = Vec::new();
        while !header.ends_with(b"\r\n\r\n") {
            header.push(stream.read_u8().await.unwrap());
            assert!(header.len() <= 16_384);
        }
        let text = String::from_utf8(header).unwrap();
        let lowered = text.to_ascii_lowercase();
        let len = lowered.lines().find_map(|s| s.strip_prefix("content-length:")).unwrap().trim().parse::<usize>().unwrap();
        let mut bytes = vec![0; len];
        stream.read_exact(&mut bytes).await.unwrap();
        (text, serde_json::from_slice(&bytes).unwrap())
    }

    #[test]
    fn short_entry_and_short_close_use_yes_prices() {
        let pm = credentials();
        let sell_yes = pm.submit(&order(Side::SellYes, Effect::Open, Kind::Maker), TS).unwrap().body.unwrap();
        assert_eq!(sell_yes["price"]["value"], "0.4200");
        assert_eq!(sell_yes["intent"], "ORDER_INTENT_BUY_SHORT");
        assert_eq!(sell_yes["participateDontInitiate"], true);
        let buy_yes = pm.submit(&order(Side::BuyYes, Effect::Close, Kind::Taker), TS).unwrap().body.unwrap();
        assert_eq!(buy_yes["intent"], "ORDER_INTENT_SELL_SHORT");
        assert_eq!(buy_yes["tif"], "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL");
        assert!(pm.cancel("btc", "../../open/cancel", TS).is_err());
    }

    #[test]
    fn malformed_private_fills_fail_closed() {
        let pm = credentials();
        let mut value = lifecycle("EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED", 2);
        value["orderSubscriptionUpdate"]["execution"]["lastShares"] = json!("0.5");
        assert!(pm.events(&value).is_err());
        assert_eq!(timestamp_ns("1970-01-01T00:00:01.123456789Z").unwrap(), 1_123_456_789);
        assert!(timestamp_ns("2026-02-29T00:00:00Z").is_err());
    }

    #[allow(clippy::result_large_err)] // The tungstenite callback has a fixed error type.
    #[tokio::test]
    async fn signed_rest_submit_private_fill_and_cancel_lifecycle() {
        let pm = credentials();
        let market = "cpc-btc-fixture";
        let server = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let rest_addr = server.local_addr().unwrap();
        let (canceled, cancel_notice) = tokio::sync::oneshot::channel();
        let rest = tokio::spawn(async move {
            for (path, expected, reply) in [
                (
                    "/v1/orders",
                    json!({"marketSlug":market,"type":"ORDER_TYPE_LIMIT",
                    "price":{"value":"0.4200","currency":"USD"},"quantity":4,
                    "tif":"TIME_IN_FORCE_GOOD_TILL_CANCEL","participateDontInitiate":true,
                    "intent":"ORDER_INTENT_BUY_LONG","manualOrderIndicator":"MANUAL_ORDER_INDICATOR_AUTOMATIC",
                    "synchronousExecution":false}),
                    r#"{"id":"pm-order-42"}"#,
                ),
                ("/v1/order/pm-order-42/cancel", json!({"marketSlug":market}), "{}"),
            ] {
                let (mut stream, _) = server.accept().await.unwrap();
                let (head, body) = receive_http(&mut stream).await;
                assert!(head.starts_with(&format!("POST {path} HTTP/1.1")));
                let (_, signature) = head
                    .lines()
                    .filter_map(|line| line.split_once(':'))
                    .find(|(name, _)| name.eq_ignore_ascii_case("x-pm-signature"))
                    .unwrap();
                signed(&key().verifying_key(), signature.trim(), "POST", path);
                assert_eq!(body, expected);
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
                    reply.len()
                );
                stream.write_all(response.as_bytes()).await.unwrap();
            }
            canceled.send(()).unwrap();
        });
        let socket = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let ws_addr = socket.local_addr().unwrap();
        let ws = tokio::spawn(async move {
            let (stream, _) = socket.accept().await.unwrap();
            let mut socket = accept_hdr_async(stream, |req: &server::Request, response: server::Response| {
                assert_eq!(req.uri().path(), WS_PATH);
                signed(&key().verifying_key(), req.headers()["x-pm-signature"].to_str().unwrap(), "GET", WS_PATH);
                Ok(response)
            })
            .await
            .unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(socket.next().await.unwrap().unwrap().to_text().unwrap()).unwrap(),
                credentials().subscription()
            );
            let fill = lifecycle("EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED", 2);
            socket.send(Message::Text(fill.to_string().into())).await.unwrap();
            cancel_notice.await.unwrap();
            let terminal = lifecycle("EXECUTION_TYPE_CANCELED", "ORDER_STATE_CANCELED", 2);
            socket.send(Message::Text(terminal.to_string().into())).await.unwrap();
        });
        let raw_client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .unwrap();
        let send = |request: Request| {
            let mut builder = raw_client
                .request(request.method.parse().unwrap(), format!("http://{rest_addr}{}", request.path))
                .header("Content-Type", "application/json");
            for (key, value) in request.headers {
                builder = builder.header(key, value);
            }
            builder.body(request.body.unwrap().to_string()).send()
        };
        let submit = pm.submit(&order(Side::BuyYes, Effect::Open, Kind::Maker), TS).unwrap();
        let raw = send(submit).await.unwrap().error_for_status().unwrap().bytes().await.unwrap();
        let ack = pm.ack(&serde_json::from_slice(&raw).unwrap(), "local-42").unwrap();
        assert!(matches!(ack, WireEvent::Ack { client_id, venue_id } if client_id == "local-42" && venue_id == "pm-order-42"));
        let mut request = format!("ws://{ws_addr}{WS_PATH}").into_client_request().unwrap();
        for (name, value) in pm.ws_headers(TS) {
            use tokio_tungstenite::tungstenite::http::{HeaderName, HeaderValue};
            request.headers_mut().insert(HeaderName::from_bytes(name.as_bytes()).unwrap(), HeaderValue::from_str(&value).unwrap());
        }
        let (mut socket, _) = connect_async(request).await.unwrap();
        socket.send(Message::Text(pm.subscription().to_string().into())).await.unwrap();
        let next = socket.next().await.unwrap().unwrap();
        let parsed = pm.events(&serde_json::from_str(next.to_text().unwrap()).unwrap()).unwrap();
        assert!(
            matches!(&parsed[..], [WireEvent::Fill {market_id, quantity:2, price_bps:4250, fee_micros:Some(30_000), ..}] if market_id == market)
        );
        send(pm.cancel(market, "pm-order-42", TS).unwrap()).await.unwrap().error_for_status().unwrap();
        let next = socket.next().await.unwrap().unwrap();
        let parsed = pm.events(&serde_json::from_str(next.to_text().unwrap()).unwrap()).unwrap();
        assert!(matches!(&parsed[..], [WireEvent::Terminal {venue_id, filled:2}] if venue_id == "pm-order-42"));
        ws.await.unwrap();
        rest.await.unwrap();
    }
}
