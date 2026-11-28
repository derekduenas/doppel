//! Risk is priced in integer microdollars. Rebate estimates never fund a reservation.
use crate::{
    netting::{fresh, Net},
    Effect, Kind, Market, Order, Result, Side, Venue,
};
use serde::{Deserialize, Serialize};

pub const MAX_FEED_AGE_MS: u64 = 250;
pub const MAX_API_RTT_MS: u64 = 300;
pub const USD: u64 = 1_000_000;
const PROMO_END_NS: u64 = 1_798_779_600_000_000_000; // 2027-01-01 05:00:00Z, midnight ET

#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub max_risk_per_window_usd: f64,
    pub max_daily_loss_usd: f64,
    pub max_open_positions: usize,
    pub fractional_kelly: f64,
}

impl Default for Limits {
    fn default() -> Self {
        Self { max_risk_per_window_usd: 50.0, max_daily_loss_usd: 250.0, max_open_positions: 3, fractional_kelly: 0.25 }
    }
}

impl Limits {
    pub fn validate(&self) -> Result<()> {
        if !(0.01..=50.0).contains(&self.max_risk_per_window_usd)
            || !(0.01..=250.0).contains(&self.max_daily_loss_usd)
            || !(1..=3).contains(&self.max_open_positions)
            || !(0.001..=0.25).contains(&self.fractional_kelly)
        {
            return Err("invalid canary limit or limit above hard ceiling".into());
        }
        Ok(())
    }

    pub fn window(&self) -> u64 {
        (self.max_risk_per_window_usd * USD as f64).floor() as u64
    }

    pub fn daily(&self) -> u64 {
        (self.max_daily_loss_usd * USD as f64).floor() as u64
    }
}

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Level {
    pub price_bps: u16,
    pub quantity: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Book {
    pub received_ns: u64,
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
}

impl Book {
    pub fn valid(&self) -> bool {
        let levels = |data: &[Level]| {
            !data.is_empty() && data.len() <= 100 && data.iter().all(|p| (1..10_000).contains(&p.price_bps) && p.quantity > 0)
        };
        levels(&self.bids)
            && levels(&self.asks)
            && self.bids[0].price_bps < self.asks[0].price_bps
            && self.bids.windows(2).all(|l| l[0].price_bps > l[1].price_bps)
            && self.asks.windows(2).all(|l| l[0].price_bps < l[1].price_bps)
    }

    pub fn mid(&self) -> u16 {
        (self.bids[0].price_bps + self.asks[0].price_bps) / 2
    }

    fn marketable(&self, side: Side) -> &[Level] {
        match side {
            Side::BuyYes => &self.asks,
            Side::SellYes => &self.bids,
        }
    }

    fn walk(&self, side: Side, quantity: u32) -> Option<(u16, f64)> {
        let mut remaining = quantity;
        let mut cost = 0.0;
        for level in self.marketable(side) {
            let q = remaining.min(level.quantity);
            cost += f64::from(q) * f64::from(side.directional_price(level.price_bps));
            remaining -= q;
            if remaining == 0 {
                return Some((level.price_bps, cost / f64::from(quantity) / 10_000.0));
            }
        }
        None
    }
}

pub fn promotional(now: u64) -> bool {
    now < PROMO_END_NS
}

fn fee_dollars(venue: Venue, quantity: u32, p: f64, kind: Kind, now: u64, gross: bool) -> f64 {
    if kind == Kind::Maker {
        return 0.0;
    }
    let q = f64::from(quantity);
    match venue {
        Venue::Pmus => (if !gross && promotional(now) { 0.02085 } else { 0.0695 }) * q * p * (1.0 - p),
        Venue::Kalshi => (7.0 * q * p * (1.0 - p)).ceil() / 100.0,
    }
}

pub fn estimated_rebate(order: &Order, quantity: u32, price_bps: u16, now: u64) -> u64 {
    if order.market.venue != Venue::Pmus || order.kind != Kind::Taker || !promotional(now) {
        return 0;
    }
    (0.04865 * f64::from(quantity) * f64::from(price_bps) / 10_000.0 * (1.0 - f64::from(price_bps) / 10_000.0) * USD as f64).floor() as u64
}

/// Cover the worst price and peak gross fee. Kalshi can round each partial fill separately.
pub(crate) fn reserve(order: &Order, quantity: u32) -> u64 {
    let per_contract = u64::from(order.side.directional_price(order.price_bps)) * 100 + if order.kind == Kind::Taker { 20_000 } else { 0 };
    per_contract * u64::from(quantity)
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub(crate) struct Position {
    pub quantity: i64,
    pub basis: u64,
}

pub(crate) struct Plan<'a> {
    pub market: &'a Market,
    pub book: &'a Book,
    pub position: &'a Position,
    pub net: Net,
    pub limits: &'a Limits,
    pub remaining_day: u64,
    pub now: u64,
    pub flatten: bool,
    pub sweeping: bool,
}

impl Plan<'_> {
    pub fn order(self) -> Option<Order> {
        let Self { market, book, position, net, limits, remaining_day, now, flatten, sweeping } = self;
        if !book.valid() || !fresh(book.received_ns, now) {
            return None;
        }
        let target = if flatten { 0 } else { net.q_target };
        let delta = target.saturating_sub(position.quantity);
        let side = if delta > 0 {
            Side::BuyYes
        } else if delta < 0 {
            Side::SellYes
        } else {
            return None;
        };
        let closing = position.quantity.signum() == -side.signed(1);
        let effect = if closing { Effect::Close } else { Effect::Open };
        let kind = if closing { Kind::Taker } else { net.kind };
        if kind == Kind::Maker && sweeping {
            return None;
        }
        let touch = book.marketable(side)[0].price_bps;
        let maker_price = match side {
            Side::BuyYes => book.bids[0].price_bps,
            Side::SellYes => book.asks[0].price_bps,
        };
        let theoretical = if side == Side::BuyYes { net.p_meta } else { 1.0 - net.p_meta };
        let entry_bps = if kind == Kind::Maker { maker_price } else { touch };
        let price = f64::from(side.directional_price(entry_bps)) / 10_000.0;
        let spread = f64::from(book.asks[0].price_bps - book.bids[0].price_bps) / 10_000.0;
        let cash = limits.window().saturating_sub(position.basis).min(remaining_day);
        let penalty = net.epistemic_var.sqrt() + spread / 2.0;
        let fraction = (limits.fractional_kelly * net.size_scale * (theoretical - price - penalty).max(0.0) / (1.0 - price)).min(1.0);
        let cap = if closing { position.quantity.unsigned_abs() } else { (cash as f64 * fraction / (price * USD as f64)).floor() as u64 };
        let depth = book.marketable(side).iter().map(|l| u64::from(l.quantity)).sum::<u64>();
        let requested = delta.unsigned_abs().min(cap).min(if kind == Kind::Taker { depth } else { u64::MAX });
        let mut order = Order {
            market: market.clone(),
            client_id: String::new(),
            side,
            effect,
            kind,
            quantity: u32::try_from(requested).ok()?,
            price_bps: entry_bps,
            shadow_price_bps: net.shadow_expected_price_bps.unwrap_or(touch),
            intent_ts_ns: net.ts_ns,
        };
        while order.quantity > 0 {
            if kind == Kind::Taker {
                let (limit, walk) = book.walk(side, order.quantity)?;
                order.price_bps = limit;
                if !closing {
                    let max_fee = fee_dollars(market.venue, order.quantity, walk, kind, now, false) / f64::from(order.quantity);
                    if theoretical - price <= 1.5 * max_fee + (walk - price).max(0.0) {
                        order.quantity /= 2;
                        continue;
                    }
                }
            }
            if closing || reserve(&order, order.quantity) <= cash {
                return Some(order);
            }
            order.quantity /= 2;
        }
        None
    }
}
