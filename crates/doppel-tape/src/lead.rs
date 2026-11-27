//! Public BTC lead feeds. Keep the original wire payload and recorder receive clocks.
//! Binance sends partial top-20 depth and native spot or aggregate futures trades.
//! Hyperliquid sends L2 snapshots and trade batches. Books and trades have separate
//! sockets, so working trade delivery cannot hide a stalled book feed.

use async_trait::async_trait;
use serde_json::{json, Value};

use crate::recorder::Recorder;
use crate::ws::{Frame, Out, WsVenue};

struct Feed {
    name: &'static str,
    url: &'static str,
    classify: fn(&str) -> Frame,
    subscription: Option<&'static str>,
}

static FEEDS: [Feed; 6] = [
    Feed {
        name: "lead_binance_spot_l2",
        url: "wss://data-stream.binance.vision/ws/btcusdt@depth20@100ms",
        classify: |raw| classify(raw, |m| m["lastUpdateId"].is_u64() && m["bids"].is_array() && m["asks"].is_array()),
        subscription: None,
    },
    Feed {
        name: "lead_binance_spot_trades",
        url: "wss://data-stream.binance.vision/ws/btcusdt@trade",
        classify: |raw| classify(raw, |m| binance_event(m, "trade") && m["t"].is_u64() && trade(m)),
        subscription: None,
    },
    Feed {
        name: "lead_binance_perp_l2",
        url: "wss://fstream.binance.com/public/ws/btcusdt@depth20@100ms",
        classify: |raw| classify(raw, |m| binance_event(m, "depthUpdate") && m["u"].is_u64() && m["b"].is_array() && m["a"].is_array()),
        subscription: None,
    },
    Feed {
        name: "lead_binance_perp_trades",
        url: "wss://fstream.binance.com/market/ws/btcusdt@aggTrade",
        classify: |raw| classify(raw, |m| binance_event(m, "aggTrade") && m["a"].is_u64() && trade(m)),
        subscription: None,
    },
    Feed {
        name: "lead_hyperliquid_l2",
        url: "wss://api.hyperliquid.xyz/ws",
        classify: |raw| {
            classify(raw, |m| {
                let book = &m["data"];
                m["channel"] == "l2Book"
                    && book["coin"] == "BTC"
                    && book["time"].is_u64()
                    && book["levels"].as_array().is_some_and(|sides| sides.len() == 2 && sides.iter().all(Value::is_array))
            })
        },
        subscription: Some("l2Book"),
    },
    Feed {
        name: "lead_hyperliquid_trades",
        url: "wss://api.hyperliquid.xyz/ws",
        classify: |raw| {
            classify(raw, |m| {
                m["channel"] == "trades"
                    && m["data"].as_array().is_some_and(|trades| {
                        !trades.is_empty()
                            && trades.iter().all(|t| t["coin"] == "BTC" && t["tid"].is_u64() && t["time"].is_u64() && t["side"].is_string())
                    })
            })
        },
        subscription: Some("trades"),
    },
];

fn binance_event(m: &Value, event: &str) -> bool {
    // st is optional in the native format before the USD-M / coin-margined futures migration.
    m["e"] == event && m["s"] == "BTCUSDT" && m.get("st").is_none_or(|st| st.as_u64() == Some(1))
}

fn trade(m: &Value) -> bool {
    m["T"].is_u64() && m["p"].is_string() && m["q"].is_string() && m["m"].is_boolean()
}

fn classify(raw: &str, is_data: impl FnOnce(&Value) -> bool) -> Frame {
    if serde_json::from_str(raw).is_ok_and(|value| is_data(&value)) {
        Frame::Data
    } else {
        Frame::Keepalive
    }
}

pub fn stream_names() -> impl Iterator<Item = &'static str> {
    FEEDS.iter().map(|feed| feed.name)
}

pub struct LeadFeed {
    feed: &'static Feed,
    pub url: String,
}

impl LeadFeed {
    pub fn new(name: &str) -> Option<Self> {
        let feed = FEEDS.iter().find(|feed| feed.name == name)?;
        Some(Self { feed, url: feed.url.to_owned() })
    }
}

#[async_trait]
impl WsVenue for LeadFeed {
    fn url(&self) -> String {
        self.url.clone()
    }

    fn stall_s(&self) -> f64 {
        60.0
    }

    fn keepalives_are_liveness(&self) -> bool {
        false
    }

    fn keepalive(&self) -> Option<(&'static str, f64)> {
        self.feed.subscription.map(|_| (r#"{"method":"ping"}"#, 25.0))
    }

    fn classifier(&self) -> fn(&str) -> Frame {
        self.feed.classify
    }

    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        if let Some(channel) = self.feed.subscription {
            let sub = json!({"method": "subscribe", "subscription": {"type": channel, "coin": "BTC"}});
            out.push(sub.to_string());
            rec.ctl("subscribe", json!({"sub": sub}));
        }
        Ok(())
    }
}
