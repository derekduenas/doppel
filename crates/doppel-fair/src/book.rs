//! In-memory constituent L2 book: port of the `Book` container in PERELMAN `books.py`
//! (`put`, `replace`, `reset`, `best`). The per-venue WebSocket frame decoders in `books.py` are
//! feed wiring and are not ported here.

use std::collections::BTreeMap;

use crate::brti::{BookSnapshot, Level};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Bid,
    Ask,
}

/// Total-order key for a non-NaN price (`-0.0` folded into `0.0`, as Python dict keys compare).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct PriceKey(i64);

impl PriceKey {
    fn new(p: f64) -> Self {
        let p = if p == 0.0 { 0.0 } else { p };
        let b = p.to_bits() as i64;
        PriceKey(b ^ ((((b >> 63) as u64) >> 1) as i64))
    }
    fn price(self) -> f64 {
        let b = self.0;
        f64::from_bits((b ^ ((((b >> 63) as u64) >> 1) as i64)) as u64)
    }
}

/// One venue's book. `valid` means a snapshot with both sides has been applied.
#[derive(Clone, Debug, Default)]
pub struct L2Book {
    bids: BTreeMap<PriceKey, f64>,
    asks: BTreeMap<PriceKey, f64>,
    pub valid: bool,
    /// Our receive time of the latest applied message (unix ns); the replica's staleness clock.
    pub last_update_ns: i64,
    /// Exchange timestamp of the latest applied message (unix ns), 0 if unknown.
    pub venue_ts_ns: i64,
    pub snapshots: u64,
    pub updates: u64,
}

impl L2Book {
    pub fn new() -> Self {
        Self::default()
    }

    /// Clear both sides and mark invalid.
    pub fn reset(&mut self) {
        self.bids.clear();
        self.asks.clear();
        self.valid = false;
    }

    /// Set one level to an absolute size; size <= 0 deletes it. NaN prices are ignored (the
    /// replica's cleaning would drop them anyway).
    pub fn put(&mut self, side: Side, price: f64, size: f64) {
        if price.is_nan() {
            return;
        }
        let levels = match side {
            Side::Bid => &mut self.bids,
            Side::Ask => &mut self.asks,
        };
        let k = PriceKey::new(price);
        if size <= 0.0 {
            levels.remove(&k);
        } else {
            levels.insert(k, size);
        }
    }

    /// Replace the whole book from a snapshot; non-positive sizes are dropped.
    pub fn replace(&mut self, bids: &[Level], asks: &[Level]) {
        let side = |xs: &[Level]| xs.iter().filter(|l| l.1 > 0.0 && !l.0.is_nan()).map(|&(p, s)| (PriceKey::new(p), s)).collect();
        self.bids = side(bids);
        self.asks = side(asks);
        self.valid = !self.bids.is_empty() && !self.asks.is_empty();
        self.snapshots += 1;
    }

    /// (best bid, best ask), or `None` if a side is empty.
    pub fn best(&self) -> Option<(f64, f64)> {
        Some((self.bids.keys().next_back()?.price(), self.asks.keys().next()?.price()))
    }

    pub fn depth(&self) -> (usize, usize) {
        (self.bids.len(), self.asks.len())
    }

    /// Snapshot for the replica: bids descending, asks ascending, stamped `last_update_ns`.
    pub fn snapshot(&self) -> BookSnapshot {
        self.snapshot_depth(usize::MAX)
    }

    /// The first `depth` levels of each side, in the same order as a full snapshot.
    pub fn snapshot_depth(&self, depth: usize) -> BookSnapshot {
        BookSnapshot {
            bids: self.bids.iter().rev().take(depth).map(|(k, &s)| (k.price(), s)).collect(),
            asks: self.asks.iter().take(depth).map(|(k, &s)| (k.price(), s)).collect(),
            retrieved_ns: self.last_update_ns,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn put_replace_best_snapshot() {
        let mut b = L2Book::new();
        b.replace(&[(100.0, 1.0), (99.0, 0.0), (98.5, 2.0)], &[(101.0, 1.0), (102.0, 3.0)]);
        assert!(b.valid);
        assert_eq!(b.best(), Some((100.0, 101.0)));
        b.put(Side::Bid, 100.5, 0.7);
        b.put(Side::Ask, 101.0, 0.0);
        assert_eq!(b.best(), Some((100.5, 102.0)));
        b.last_update_ns = 7;
        let s = b.snapshot();
        assert_eq!(s.bids, vec![(100.5, 0.7), (100.0, 1.0), (98.5, 2.0)]);
        assert_eq!(s.asks, vec![(102.0, 3.0)]);
        assert_eq!(s.retrieved_ns, 7);
        b.reset();
        assert!(!b.valid);
        assert_eq!(b.best(), None);
    }

    #[test]
    fn key_order_matches_float_order() {
        let xs = [-5.0, -0.0, 0.0, 1e-300, 1.0, 60000.01, f64::INFINITY];
        for w in xs.windows(2) {
            assert!(PriceKey::new(w[0]) <= PriceKey::new(w[1]));
            assert_eq!(PriceKey::new(w[1]).price(), if w[1] == 0.0 { 0.0 } else { w[1] });
        }
    }
}
