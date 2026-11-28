//! Combine target inventory before any order crosses the spread.
use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::{Kind, Market, MILLIS};

pub const SIGNAL_AGE_NS: u64 = 250 * MILLIS;
const VAR_FLOOR: f64 = 1e-6;

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Signal {
    pub genome_id: String,
    pub market: Market,
    pub p_hat: f64,
    pub epistemic_var: f64,
    pub target_contracts: i32,
    pub shadow_expected_price_bps: Option<u16>,
    pub kind: Kind,
    pub ts_ns: u64,
}

impl Signal {
    pub fn valid(&self, now: u64) -> bool {
        self.market.valid()
            && !self.genome_id.is_empty()
            && self.genome_id.len() <= 128
            && (0.0..=1.0).contains(&self.p_hat)
            && (0.0..=1.0).contains(&self.epistemic_var)
            && self.shadow_expected_price_bps.is_none_or(|p| (1..10_000).contains(&p))
            && fresh(self.ts_ns, now)
    }
}

pub(crate) fn fresh(ts: u64, now: u64) -> bool {
    ts > 0 && now.checked_sub(ts).is_some_and(|age| age <= SIGNAL_AGE_NS)
}

#[derive(Debug, Copy, Clone, Eq, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum WatchState {
    Healthy,
    WarnThrottle50,
    KillDemoteShadow,
}

#[derive(Debug, Clone, Copy)]
pub struct Net {
    pub p_meta: f64,
    pub q_target: i64,
    pub epistemic_var: f64,
    pub size_scale: f64,
    pub shadow_expected_price_bps: Option<u16>,
    pub kind: Kind,
    pub ts_ns: u64,
}

#[derive(Default)]
pub struct Netter {
    by_market: BTreeMap<Market, BTreeMap<String, Signal>>,
    watch: BTreeMap<String, (u64, WatchState)>,
}

impl Netter {
    pub fn upsert(&mut self, signal: Signal, now: u64) -> bool {
        if !signal.valid(now) {
            return false;
        }
        let slot = self.by_market.entry(signal.market.clone()).or_default();
        if slot.get(&signal.genome_id).is_some_and(|s| s.ts_ns >= signal.ts_ns) {
            return false;
        }
        slot.insert(signal.genome_id.clone(), signal);
        true
    }

    pub fn watch(&mut self, genome: String, state: WatchState, ts: u64) -> bool {
        if self.watch.get(&genome).is_some_and(|(last, prev)| ts <= *last || *prev == WatchState::KillDemoteShadow) {
            return false;
        }
        self.watch.insert(genome, (ts, state));
        true
    }

    pub fn net(&self, market: &Market, now: u64, allowed: impl Fn(&str) -> bool) -> Option<Net> {
        let used: Vec<_> = self
            .by_market
            .get(market)?
            .values()
            .filter_map(|s| {
                let state = self.watch.get(&s.genome_id).map_or(WatchState::Healthy, |(_, state)| *state);
                if !allowed(&s.genome_id) || !fresh(s.ts_ns, now) || state == WatchState::KillDemoteShadow {
                    return None;
                }
                let q = i64::from(s.target_contracts) / if state == WatchState::WarnThrottle50 { 2 } else { 1 };
                Some((s, q, 1.0 / s.epistemic_var.max(VAR_FLOOR), state))
            })
            .collect();
        let precision: f64 = used.iter().map(|(_, _, w, _)| w).sum();
        if precision == 0.0 {
            return None;
        }
        let p_meta = used.iter().map(|(s, _, w, _)| s.p_hat * w).sum::<f64>() / precision;
        let disagreement = used.iter().map(|(s, _, w, _)| w * (s.p_hat - p_meta).powi(2)).sum::<f64>() / precision;
        let q_target: i64 = used.iter().map(|(_, q, _, _)| *q).sum();
        let same_side: Vec<_> = used.iter().filter(|(_, q, _, _)| q.signum() == q_target.signum() && *q != 0).collect();
        let expected = if !same_side.is_empty() && same_side.iter().all(|(s, _, _, _)| s.shadow_expected_price_bps.is_some()) {
            let qty: u64 = same_side.iter().map(|(_, q, _, _)| q.unsigned_abs()).sum();
            Some(
                (same_side.iter().map(|(s, q, _, _)| u64::from(s.shadow_expected_price_bps.unwrap_or(0)) * q.unsigned_abs()).sum::<u64>()
                    / qty) as u16,
            )
        } else {
            None
        };
        let kind = if used.iter().any(|(s, q, _, _)| *q != 0 && s.kind == Kind::Maker) { Kind::Maker } else { Kind::Taker };
        Some(Net {
            p_meta,
            q_target,
            epistemic_var: 1.0 / precision + disagreement,
            shadow_expected_price_bps: expected,
            size_scale: if used.iter().any(|(_, _, _, state)| *state == WatchState::WarnThrottle50) { 0.5 } else { 1.0 },
            kind,
            ts_ns: used.iter().map(|(s, _, _, _)| s.ts_ns).min()?,
        })
    }

    pub fn prune(&mut self, now: u64) {
        self.by_market.retain(|_, bucket| {
            bucket.retain(|_, signal| fresh(signal.ts_ns, now));
            !bucket.is_empty()
        });
    }
}
