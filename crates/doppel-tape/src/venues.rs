//! Public WebSocket recorders (Python `sources/venues.py`): RTDS, intl CLOB shards, spot.

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde_json::json;

use crate::clock::wall_s;
use crate::recorder::Recorder;
use crate::universe::IntlUniverse;
use crate::ws::{Frame, Out, WsVenue};

/// Polymarket RTDS `activity/trades`. Run two (rtds_a/rtds_b) for redundancy.
pub struct RtdsTrades {
    pub url: String,
}

impl RtdsTrades {
    pub fn new() -> Self {
        Self { url: "wss://ws-live-data.polymarket.com".into() }
    }
}

impl Default for RtdsTrades {
    fn default() -> Self {
        Self::new()
    }
}

fn rtds_classify(raw: &str) -> Frame {
    if matches!(raw, "" | "PONG" | "pong") {
        Frame::Keepalive
    } else {
        Frame::Data
    }
}

#[async_trait]
impl WsVenue for RtdsTrades {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        10.0
    }
    fn keepalive(&self) -> Option<(&'static str, f64)> {
        Some(("ping", 5.0))
    }
    fn classifier(&self) -> fn(&str) -> Frame {
        rtds_classify
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        let sub = json!({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]});
        out.push(sub.to_string());
        rec.ctl("subscribe", json!({"sub": sub}));
        Ok(())
    }
}

/// Intl CLOB market channel for one asset x horizon shard (one socket per shard: the server drops
/// a single slow consumer). New windows are added in place; expired ones are unsubscribed.
pub struct IntlClobMarket {
    pub url: String,
    pub universe: IntlUniverse,
    client: reqwest::Client,
    subscribed: BTreeSet<String>,
    next_refresh: Instant,
}

impl IntlClobMarket {
    pub fn new(universe: IntlUniverse) -> Self {
        Self {
            url: "wss://ws-subscriptions-clob.polymarket.com/ws/market".into(),
            universe,
            client: crate::http::client(10),
            subscribed: BTreeSet::new(),
            next_refresh: Instant::now(),
        }
    }

    async fn desired(&mut self, rec: &Recorder) -> BTreeSet<String> {
        let wins = self.universe.current(&self.client, wall_s()).await;
        rec.stats.set_extra("windows", wins.len());
        wins.into_iter().flat_map(|w| w.tokens).collect()
    }
}

#[async_trait]
impl WsVenue for IntlClobMarket {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        20.0
    }
    fn keepalive(&self) -> Option<(&'static str, f64)> {
        Some(("PING", 10.0))
    }
    fn expect_traffic(&self) -> bool {
        !self.subscribed.is_empty()
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.subscribed.clear();
        let want = self.desired(rec).await;
        if !want.is_empty() {
            let assets: Vec<_> = want.iter().cloned().collect();
            out.push(json!({"assets_ids": assets, "type": "market"}).to_string());
            rec.ctl("subscribe", json!({"n": assets.len(), "assets": assets}));
            self.subscribed = want;
        }
        self.next_refresh = Instant::now() + Duration::from_secs(15);
        Ok(())
    }
    async fn on_tick(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        if Instant::now() < self.next_refresh {
            return Ok(());
        }
        self.next_refresh = Instant::now() + Duration::from_secs(15);
        let want = self.desired(rec).await;
        let stale: Vec<_> = self.subscribed.difference(&want).cloned().collect();
        if !stale.is_empty() {
            out.push(json!({"assets_ids": stale, "operation": "unsubscribe"}).to_string());
            for s in &stale {
                self.subscribed.remove(s);
            }
            rec.ctl("unsubscribe", json!({"n": stale.len()}));
        }
        let new: Vec<_> = want.difference(&self.subscribed).cloned().collect();
        if !new.is_empty() {
            out.push(json!({"assets_ids": new, "operation": "subscribe"}).to_string());
            self.subscribed.extend(new.iter().cloned());
            rec.ctl("subscribe_add", json!({"n": new.len(), "assets": new}));
        }
        rec.stats.set_extra("subscribed", self.subscribed.len());
        Ok(())
    }
}

pub struct CoinbaseTrades;
pub struct KrakenTrades;
pub struct BitstampTrades;

#[async_trait]
impl WsVenue for CoinbaseTrades {
    fn url(&self) -> String {
        "wss://ws-feed.exchange.coinbase.com".into()
    }
    fn stall_s(&self) -> f64 {
        30.0
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        let products = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"];
        out.push(json!({"type": "subscribe", "product_ids": products, "channels": ["matches", "heartbeat"]}).to_string());
        rec.ctl("subscribe", json!({"products": products}));
        Ok(())
    }
}

#[async_trait]
impl WsVenue for KrakenTrades {
    fn url(&self) -> String {
        "wss://ws.kraken.com/v2".into()
    }
    fn stall_s(&self) -> f64 {
        30.0
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        out.push(
            json!({"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"]}})
                .to_string(),
        );
        rec.ctl("subscribe", json!({}));
        Ok(())
    }
}

#[async_trait]
impl WsVenue for BitstampTrades {
    fn url(&self) -> String {
        "wss://ws.bitstamp.net".into()
    }
    fn stall_s(&self) -> f64 {
        60.0
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        for pair in ["btcusd", "ethusd", "solusd", "xrpusd"] {
            out.push(json!({"event": "bts:subscribe", "data": {"channel": format!("live_trades_{pair}")}}).to_string());
        }
        rec.ctl("subscribe", json!({}));
        Ok(())
    }
}
