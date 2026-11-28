//! Single-writer canary OMS. Venue prices are normalized to YES basis points.
use serde::{Deserialize, Serialize};

pub mod admission;
pub mod ledger;
pub mod netting;
pub mod oms;
pub mod risk;
pub mod venues;

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const MILLIS: u64 = 1_000_000;
pub const DAY_NS: u64 = 86_400_000 * MILLIS;

#[derive(Debug, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Venue {
    Pmus,
    Kalshi,
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Market {
    pub venue: Venue,
    pub id: String,
}

impl Market {
    pub fn valid(&self) -> bool {
        !self.id.is_empty() && self.id.len() <= 128 && self.id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
    }
}

#[derive(Debug, Copy, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    BuyYes,
    SellYes,
}

impl Side {
    pub fn signed(self, qty: u32) -> i64 {
        match self {
            Self::BuyYes => i64::from(qty),
            Self::SellYes => -i64::from(qty),
        }
    }

    pub fn directional_price(self, yes_bps: u16) -> u16 {
        match self {
            Self::BuyYes => yes_bps,
            Self::SellYes => 10_000 - yes_bps,
        }
    }
}

#[derive(Debug, Copy, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Effect {
    Open,
    Close,
}

#[derive(Debug, Copy, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Maker,
    Taker,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Order {
    pub market: Market,
    pub client_id: String,
    pub side: Side,
    pub effect: Effect,
    pub kind: Kind,
    pub quantity: u32,
    pub price_bps: u16,
    pub shadow_price_bps: u16,
    pub intent_ts_ns: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum WireEvent {
    Ack {
        client_id: String,
        venue_id: String,
    },
    Fill {
        id: String,
        venue_id: String,
        client_id: Option<String>,
        market_id: String,
        quantity: u32,
        price_bps: u16,
        fee_micros: Option<u64>,
        ts_ns: u64,
    },
    Terminal {
        venue_id: String,
        filled: u32,
    },
}

pub fn wall_ns() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| u64::try_from(d.as_nanos()).unwrap_or(u64::MAX))
}
