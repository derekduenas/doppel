//! BRTI constituent replica: port of PERELMAN `brti.py` (CME CF Real Time Indices Methodology
//! v17.0: 4.1 calculation, 5 contingency rules, 6 parameters). `brti.py` is the oracle; this
//! follows it step for step, including its float arithmetic (CPython `sum`, libm `pow` for `** 2`).
//! Speedups never change results: the price-volume walk uses a moving pointer instead of
//! rescanning, and the KDE mode skips kernel terms that are exactly `0.0` in double precision.
//!
//! Pure functions over in-memory snapshots. No I/O, no clock: the caller passes `calc_ns`.

use std::collections::BTreeSet;

use crate::pymath::{py_pow, py_pow2, py_sum};

/// D, maximum mid deviation along the price-volume curve, BTC (6.2).
pub const DEVIATION_FROM_MID: f64 = 0.005;
/// Venue mid deviation from the median that marks a book potentially erroneous (6.2).
pub const POTENTIALLY_ERRONEOUS: f64 = 0.05;
/// A book older than this at calculation time is delayed (6.2), in nanoseconds (10 s).
pub const RETRIEVAL_LAG_THRESHOLD_NS: i64 = 10_000_000_000;
/// tau, target number of levels for the static spacing (6.1).
pub const TARGET_LEVELS: f64 = 100.0;
/// lambda = 1 / (0.3 * utilized depth) (6.1).
pub const LAMBDA_FACTOR: f64 = 0.3;
/// Eq. 4a/4b: at least min(side length, 50) levels in the cap sample.
pub const CAP_SAMPLE_MIN: usize = 50;
/// Eq. 4a/4b: levels within 5% of the best price.
pub const CAP_BAND: f64 = 0.05;
/// KDE grid points in the reference's argmax search (`brti.py` AMBIGUITY: 4001 points).
pub const KDE_GRID: usize = 4001;
/// Safety valve absent from `brti.py`: a spacing so fine that the price-volume walk would need
/// more steps than this fails instead of running for minutes (Python would just be slow).
pub const MAX_CURVE_STEPS: usize = 10_000_000;

/// `(price, size)`.
pub type Level = (f64, f64);

/// One venue's book at retrieval time.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BookSnapshot {
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
    /// When we last received an update for this book (our clock, unix ns).
    pub retrieved_ns: i64,
}

/// Why a calculation produced no value (`CalculationFailure` in `brti.py`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// `brti_from_books([])`.
    NoUsableBooks,
    /// A consolidated side is empty (Python raises `IndexError` here; unreachable via the replica).
    EmptySide,
    /// Uncrossing consumed a whole side.
    SideConsumed,
    /// Every book delayed or erroneous (5.1, 5.2).
    AllDelayedOrErroneous,
    /// Every usable book excluded as potentially erroneous (5.3).
    AllPotentiallyErroneous,
    /// Spacing so small the curve walk exceeds [`MAX_CURVE_STEPS`] (not in `brti.py`).
    CurveTooFine,
}

impl Failure {
    /// The `CalculationFailure` message `brti.py` raises.
    pub fn message(self) -> &'static str {
        match self {
            Failure::NoUsableBooks => "no usable books",
            Failure::EmptySide => "a consolidated side is empty",
            Failure::SideConsumed => "a side was consumed while uncrossing",
            Failure::AllDelayedOrErroneous => "all books delayed or erroneous",
            Failure::AllPotentiallyErroneous => "all books potentially erroneous",
            Failure::CurveTooFine => "price-volume curve needs too many steps",
        }
    }
}

/// Index-level diagnostics (the dict `brti_from_books` returns).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct IndexDiag {
    /// Order size cap (4.1.3).
    pub cap: f64,
    /// Volume spacing s (4.1.4).
    pub spacing: f64,
    /// Utilized depth v_T.
    pub utilized_depth: f64,
    /// Points on the mid-price-volume curve.
    pub levels_used: usize,
}

/// What happened to one venue in a replica calculation.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum VenueStatus {
    /// Contributed to the index.
    Used,
    /// Was excluded, deviation fell below half the threshold: contributes again.
    Reinstated { dev: f64 },
    /// Older than the retrieval-lag threshold (5.1).
    Delayed { lag_ns: i64 },
    /// After cleaning, a side is empty (5.2.1).
    EmptySide,
    /// After cleaning, best bid >= best ask (5.2.1).
    Crossed,
    /// Mid deviates more than 5% from the median of mids: excluded now (5.3).
    Outlier { dev: f64 },
    /// Still excluded: deviation not yet below 2.5% (5.3 re-instatement hysteresis).
    HeldOut { dev: f64 },
}

impl VenueStatus {
    pub fn used(&self) -> bool {
        matches!(self, VenueStatus::Used | VenueStatus::Reinstated { .. })
    }
}

/// Per-venue diagnostics, in input order.
#[derive(Clone, Debug, PartialEq)]
pub struct VenueDiag {
    pub venue: String,
    pub status: VenueStatus,
    /// Mid of the cleaned book, when it was usable.
    pub mid: Option<f64>,
    /// Cleaned (bids, asks) level counts.
    pub levels: (usize, usize),
}

/// A replica value with diagnostics.
#[derive(Clone, Debug, PartialEq)]
pub struct ReplicaOutput {
    pub value: f64,
    pub index: IndexDiag,
    /// Venues used, sorted (the `venues` list in `brti.py`'s diagnostics).
    pub venues: Vec<String>,
    pub median_mid: f64,
    pub exchanges: Vec<VenueDiag>,
}

/// A failed replica calculation, with whatever per-venue diagnostics were reached.
#[derive(Clone, Debug, PartialEq)]
pub struct ReplicaError {
    pub failure: Failure,
    pub exchanges: Vec<VenueDiag>,
}

/// Section 5.2.2: drop entries whose price or size is not a positive number.
pub fn clean(levels: &[Level]) -> Vec<Level> {
    levels.iter().copied().filter(|&(p, s)| p > 0.0 && s > 0.0).collect()
}

/// Merge all books per side; equal prices sum in book order. Bids descending, asks ascending.
fn consolidate(books: &[(&[Level], &[Level])]) -> (Vec<Level>, Vec<Level>) {
    (merge_side(books.iter().flat_map(|b| b.0.iter().copied()), true), merge_side(books.iter().flat_map(|b| b.1.iter().copied()), false))
}

fn merge_side<I: Iterator<Item = Level>>(levels: I, desc: bool) -> Vec<Level> {
    let mut all: Vec<Level> = levels.collect();
    // Stable sort keeps encounter order among equal prices, so sums accumulate as in Python.
    if desc {
        all.sort_by(|a, b| b.0.total_cmp(&a.0));
    } else {
        all.sort_by(|a, b| a.0.total_cmp(&b.0));
    }
    let mut out: Vec<Level> = Vec::with_capacity(all.len());
    for (p, s) in all {
        match out.last_mut() {
            Some(last) if last.0 == p => last.1 += s,
            _ => out.push((p, 0.0 + s)),
        }
    }
    out
}

/// Eq. 4a/4b: levels within 5% of best, at least min(|side|, 50). Returns (ac, bc).
fn sample_counts(bids: &[Level], asks: &[Level]) -> (usize, usize) {
    let lim_a = (1.0 + CAP_BAND) * asks[0].0;
    let lim_b = (1.0 - CAP_BAND) * bids[0].0;
    let ac = asks.iter().enumerate().filter(|(_, l)| l.0 <= lim_a).map(|(i, _)| i + 1).max().unwrap_or(0);
    let bc = bids.iter().enumerate().filter(|(_, l)| l.0 >= lim_b).map(|(i, _)| i + 1).max().unwrap_or(0);
    (ac.max(asks.len().min(CAP_SAMPLE_MIN)), bc.max(bids.len().min(CAP_SAMPLE_MIN)))
}

/// 1% winsorization of an ascending sample: (k, winsorized copy).
fn winsorize(sorted: &[f64]) -> (usize, Vec<f64>) {
    let n = sorted.len();
    let k = (0.01 * n as f64).floor() as usize;
    let w = (0..n)
        .map(|i| {
            if i < k {
                sorted[k]
            } else if i >= n - k {
                sorted[n - k - 1]
            } else {
                sorted[i]
            }
        })
        .collect();
    (k, w)
}

fn sort_f64(v: &mut [f64]) {
    v.sort_by(|a, b| a.total_cmp(b));
}

/// Section 4.1.3, from the uncapped consolidated book: trimmed mean + 5 sigma (winsorized).
pub fn order_size_cap(bids: &[Level], asks: &[Level]) -> f64 {
    let (ac, bc) = sample_counts(bids, asks);
    let mut sizes: Vec<f64> = bids[..bc].iter().chain(asks[..ac].iter()).map(|l| l.1).collect();
    sort_f64(&mut sizes);
    let n = sizes.len();
    let (k, w) = winsorize(&sizes);
    let trimmed_mean = py_sum(sizes[k..n - k].iter().copied()) / (n - 2 * k) as f64;
    let wmean = py_sum(w.iter().copied()) / n as f64;
    let sigma = if n > 1 { (py_sum(w.iter().map(|&x| py_pow2(x - wmean))) / (n - 1) as f64).sqrt() } else { 0.0 };
    trimmed_mean + 5.0 * sigma
}

/// Section 4.1.1 step 2a: match crossed size off the top until bid < ask.
fn uncross(bids: &mut Vec<Level>, asks: &mut Vec<Level>) {
    let (mut bi, mut ai) = (0usize, 0usize);
    while bi < bids.len() && ai < asks.len() && bids[bi].0 >= asks[ai].0 {
        let q = bids[bi].1.min(asks[ai].1);
        bids[bi].1 -= q;
        asks[ai].1 -= q;
        if bids[bi].1 <= 0.0 {
            bi += 1;
        }
        if asks[ai].1 <= 0.0 {
            ai += 1;
        }
    }
    bids.drain(..bi);
    asks.drain(..ai);
}

/// Eq. 6g-6j: mode of a Gaussian KDE (Silverman bandwidth) by a 4001-point grid search over the
/// winsorized sample range, first maximum wins. `sample` is ascending.
pub fn kde_mode(sample: &[f64]) -> f64 {
    let n = sample.len();
    if n < 4 {
        return sample[0];
    }
    let (_, w) = winsorize(sample);
    let m = py_sum(w.iter().copied()) / n as f64;
    let sd = (py_sum(w.iter().map(|&x| py_pow2(x - m))) / (n - 1) as f64).sqrt();
    if sd == 0.0 {
        return w[0];
    }
    let h = 1.06 * sd * py_pow(n as f64, -0.2);
    let (lo, hi) = (w[0], w[n - 1]);
    let norm = n as f64 * h * (2.0 * std::f64::consts::PI).sqrt();
    let grid_x = |j: usize| lo + (hi - lo) * j as f64 / (KDE_GRID - 1) as f64;
    // The reference density at grid point x: CPython sum of exp(-0.5 * ((x - xi) / h) ** 2).
    // Terms with |x - xi| > 40 h are exp(-800) = 0.0 exactly and leave Neumaier's sum and
    // compensation untouched, so summing only the sorted sample within 40 h is bit-identical.
    let exact = |x: f64, terms: &[f64]| py_sum(terms.iter().map(|&xi| (-0.5 * py_pow2((x - xi) / h)).exp())) / norm;
    // Window arithmetic needs 40 h far above the float spacing of the sample values.
    let windowed = 40.0 * h > 1e-9 * lo.abs().max(hi.abs()) && n <= 1_000_000;
    if !windowed {
        return grid_argmax((0..KDE_GRID).map(|j| (grid_x(j), exact(grid_x(j), &w))));
    }
    // Pass 1 finds every grid point whose reference density could be the maximum, by
    // branch-and-bound over blocks of the grid with cheap arithmetic (x * x for libm pow, plain
    // sums, 12 h windows). Its error against the reference is < 2e-10 relative for n <= 1e6, plus
    // n * exp(-72) absolute for the dropped tails; `REL` and `tail` cover both with room to spare.
    // Pass 2 evaluates only the survivors with the reference arithmetic, in grid order.
    let cands = kde_candidates(&w, h, &grid_x);
    let mut win = Window::new(&w, 40.0 * h);
    grid_argmax(cands.into_iter().map(|j| {
        let x = grid_x(j);
        (x, exact(x, win.at(x)))
    }))
}

/// Grid indices (ascending) whose reference KDE value may equal the maximum. A block of grid
/// points `[s, e)` with centre `c` and half-width `r` bounds the density anywhere inside it by
/// `U = sum K(max(0, |c - xi| - r) / h)`; the best density seen at any evaluated grid point gives
/// a lower bound `L` on the maximum; blocks with `U < L` cannot hold it.
fn kde_candidates<G: Fn(usize) -> f64>(w: &[f64], h: f64, grid_x: &G) -> Vec<usize> {
    const REL: f64 = 1e-9;
    const Z: f64 = 12.0;
    let tail = w.len() as f64 * (-70.0f64).exp();
    let k = |d: f64| {
        let y = d / h;
        (-0.5 * (y * y)).exp()
    };
    // Sum of k over the sample within reach of [c - r, c + r], by distance to the interval. With
    // r = 0 at a grid point x this is the reference's own y = (x - xi) / h, so only pow differs.
    // For a block, `slack` (a few ulps of the largest value) absorbs rounding in c, r and the grid.
    let slack = 1e-15 * w[0].abs().max(w[w.len() - 1].abs());
    let bound = |c: f64, r: f64| -> f64 {
        let lo_i = w.partition_point(|&xi| xi < c - r - Z * h);
        let hi_i = w.partition_point(|&xi| xi <= c + r + Z * h);
        w[lo_i..hi_i].iter().map(|&xi| k(if r > 0.0 { ((c - xi).abs() - r - slack).max(0.0) } else { (c - xi).abs() })).sum()
    };
    let mut blocks: Vec<(usize, usize)> = vec![(0, KDE_GRID)];
    let mut lower = 0.0f64;
    loop {
        let mut children: Vec<(usize, usize, f64)> = Vec::new();
        for &(s, e) in &blocks {
            let step = (e - s).div_ceil(8);
            let mut a = s;
            while a < e {
                let b = (a + step).min(e);
                let (x0, x1) = (grid_x(a), grid_x(b - 1));
                let up = if b - a == 1 { bound(x0, 0.0) } else { bound((x0 + x1) / 2.0, (x1 - x0) / 2.0) };
                let mid = grid_x((a + b - 1) / 2);
                let at_mid = bound(mid, 0.0);
                lower = lower.max(at_mid * (1.0 - REL) - tail);
                children.push((a, b, up * (1.0 + REL) + tail));
                a = b;
            }
        }
        blocks = children.into_iter().filter(|&(_, _, up)| up >= lower).map(|(a, b, _)| (a, b)).collect();
        if blocks.iter().all(|&(a, b)| b - a == 1) {
            return blocks.into_iter().map(|(a, _)| a).collect();
        }
    }
}

/// First grid point with the strictly greatest density (`if f > best_f` in `brti.py`).
fn grid_argmax<I: Iterator<Item = (f64, f64)>>(points: I) -> f64 {
    let (mut best, mut best_f) = (f64::NAN, -1.0f64);
    for (x, f) in points {
        if f > best_f || best.is_nan() {
            best = x;
            best_f = f;
        }
    }
    best
}

/// Sliding window over an ascending sample: the elements within `reach` of a non-decreasing `x`.
struct Window<'a> {
    w: &'a [f64],
    reach: f64,
    a: usize,
    b: usize,
}

impl<'a> Window<'a> {
    fn new(w: &'a [f64], reach: f64) -> Self {
        Window { w, reach, a: 0, b: 0 }
    }
    fn at(&mut self, x: f64) -> &'a [f64] {
        let w = self.w;
        while self.a < w.len() && w[self.a] < x - self.reach {
            self.a += 1;
        }
        self.b = self.b.max(self.a);
        while self.b < w.len() && w[self.b] <= x + self.reach {
            self.b += 1;
        }
        &w[self.a..self.b]
    }
}

/// Section 4.1.4, Eq. 6a-7: min(static spacing, KDE mode of the thinner side's sizes), on the
/// capped, uncrossed book (`brti.py` AMBIGUITY, kept as the reference has it).
fn spacing(bids: &[Level], asks: &[Level]) -> f64 {
    let s_lev = py_sum(asks.iter().map(|l| l.1)).min(py_sum(bids.iter().map(|l| l.1))) / TARGET_LEVELS;
    let (ac, bc) = sample_counts(bids, asks);
    let bid_sum = py_sum(bids[..bc].iter().map(|l| l.1));
    let ask_sum = py_sum(asks[..ac].iter().map(|l| l.1));
    let side = if bid_sum < ask_sum { &bids[..bc] } else { &asks[..ac] };
    let mut sample: Vec<f64> = side.iter().map(|l| l.1).collect();
    sort_f64(&mut sample);
    s_lev.min(kde_mode(&sample))
}

/// Eq. 1a/1b, `_pv` with a cursor: the first level whose cumulative size reaches `v`. Targets must
/// be non-decreasing between calls (they are: `s * ceil(i s / s - 1e-12)` is monotone in `i`).
struct Walker<'a> {
    levels: &'a [Level],
    i: usize,
    cum: f64,
}

impl<'a> Walker<'a> {
    fn new(levels: &'a [Level]) -> Self {
        let cum = levels.first().map_or(0.0, |l| 0.0 + l.1);
        Walker { levels, i: 0, cum }
    }
    fn price_at(&mut self, v: f64) -> Option<f64> {
        while self.i < self.levels.len() {
            if self.cum >= v - 1e-12 {
                return Some(self.levels[self.i].0);
            }
            self.i += 1;
            if let Some(l) = self.levels.get(self.i) {
                self.cum += l.1;
            }
        }
        None
    }
}

/// `brti_from_books`: consolidate, cap, uncross, space, walk the mid-price-volume curve, and
/// average the mids with exponential depth weights. `books` are (bids, asks) in venue order.
pub fn brti_from_books(books: &[(&[Level], &[Level])]) -> Result<(f64, IndexDiag), Failure> {
    if books.is_empty() {
        return Err(Failure::NoUsableBooks);
    }
    let (mut bids, mut asks) = consolidate(books);
    if bids.is_empty() || asks.is_empty() {
        return Err(Failure::EmptySide);
    }
    let cap = order_size_cap(&bids, &asks);
    for l in bids.iter_mut().chain(asks.iter_mut()) {
        l.1 = l.1.min(cap);
    }
    uncross(&mut bids, &mut asks);
    if bids.is_empty() || asks.is_empty() {
        return Err(Failure::SideConsumed);
    }
    let s = spacing(&bids, &asks);
    let depth = py_sum(bids.iter().map(|l| l.1)).min(py_sum(asks.iter().map(|l| l.1)));
    if depth / s > MAX_CURVE_STEPS as f64 {
        return Err(Failure::CurveTooFine);
    }
    let (mut wa, mut wb) = (Walker::new(&asks), Walker::new(&bids));
    let mut grid: Vec<f64> = Vec::new();
    let mut mids: Vec<f64> = Vec::new();
    let mut v_t = s;
    let mut i = 1u64;
    while i as f64 * s <= depth + 1e-12 {
        let v = i as f64 * s;
        let tgt = s * (v / s - 1e-12).ceil();
        let (a, b) = match (wa.price_at(tgt), wb.price_at(tgt)) {
            (Some(a), Some(b)) => (a, b),
            _ => break,
        };
        let mid = (a + b) / 2.0;
        if a / mid - 1.0 > DEVIATION_FROM_MID {
            break; // Eq. 2: stop at the first breach
        }
        grid.push(v);
        mids.push(mid);
        v_t = v;
        i += 1;
    }
    v_t = v_t.max(s);
    if grid.is_empty() {
        // Utilized depth floored at the spacing: the index is the mid at s.
        let a = Walker::new(&asks).price_at(s).ok_or(Failure::SideConsumed)?;
        let b = Walker::new(&bids).price_at(s).ok_or(Failure::SideConsumed)?;
        grid.push(s);
        mids.push((a + b) / 2.0);
    }
    let lam = 1.0 / (LAMBDA_FACTOR * v_t);
    let weights: Vec<f64> = grid.iter().map(|&v| lam * (-lam * v).exp()).collect();
    let nf = py_sum(weights.iter().copied());
    let value = py_sum(mids.iter().zip(&weights).map(|(m, w)| m * w)) / nf;
    Ok((value, IndexDiag { cap, spacing: s, utilized_depth: v_t, levels_used: grid.len() }))
}

/// Python `statistics.median`.
fn median(xs: &[f64]) -> f64 {
    let mut v = xs.to_vec();
    sort_f64(&mut v);
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

/// One replica calculation (`Replica.calculate`) as a pure function of the books, the clock and
/// the hysteresis state. Returns the result and the next `excluded` set. Venue names must be
/// unique; their order is the consolidation order (it only affects float summation order).
pub fn calculate(
    books: &[(String, BookSnapshot)],
    calc_ns: i64,
    excluded: &BTreeSet<String>,
) -> (Result<ReplicaOutput, ReplicaError>, BTreeSet<String>) {
    let mut excluded = excluded.clone();
    let mut exchanges: Vec<VenueDiag> = Vec::with_capacity(books.len());
    // (index into exchanges, cleaned bids, cleaned asks, mid)
    let mut usable: Vec<(usize, Vec<Level>, Vec<Level>, f64)> = Vec::new();
    for (venue, book) in books {
        let lag_ns = calc_ns - book.retrieved_ns;
        if lag_ns > RETRIEVAL_LAG_THRESHOLD_NS {
            exchanges.push(VenueDiag { venue: venue.clone(), status: VenueStatus::Delayed { lag_ns }, mid: None, levels: (0, 0) });
            continue;
        }
        let (bids, asks) = (clean(&book.bids), clean(&book.asks));
        let levels = (bids.len(), asks.len());
        if bids.is_empty() || asks.is_empty() {
            exchanges.push(VenueDiag { venue: venue.clone(), status: VenueStatus::EmptySide, mid: None, levels });
            continue;
        }
        let best_bid = bids.iter().map(|l| l.0).fold(f64::NEG_INFINITY, f64::max);
        let best_ask = asks.iter().map(|l| l.0).fold(f64::INFINITY, f64::min);
        if best_bid >= best_ask {
            exchanges.push(VenueDiag { venue: venue.clone(), status: VenueStatus::Crossed, mid: None, levels });
            continue;
        }
        let mid = (best_bid + best_ask) / 2.0;
        exchanges.push(VenueDiag { venue: venue.clone(), status: VenueStatus::Used, mid: Some(mid), levels });
        usable.push((exchanges.len() - 1, bids, asks, mid));
    }
    if usable.is_empty() {
        return (Err(ReplicaError { failure: Failure::AllDelayedOrErroneous, exchanges }), excluded);
    }
    let med = median(&usable.iter().map(|u| u.3).collect::<Vec<_>>());
    for (ix, _, _, mid) in &usable {
        let dev = (mid / med - 1.0).abs();
        let d = &mut exchanges[*ix];
        if excluded.contains(&d.venue) {
            if dev < 0.5 * POTENTIALLY_ERRONEOUS {
                excluded.remove(&d.venue);
                d.status = VenueStatus::Reinstated { dev };
            } else {
                d.status = VenueStatus::HeldOut { dev };
            }
        } else if dev > POTENTIALLY_ERRONEOUS {
            excluded.insert(d.venue.clone());
            d.status = VenueStatus::Outlier { dev };
        }
    }
    let kept: Vec<(&[Level], &[Level])> =
        usable.iter().filter(|u| exchanges[u.0].status.used()).map(|u| (u.1.as_slice(), u.2.as_slice())).collect();
    if kept.is_empty() {
        return (Err(ReplicaError { failure: Failure::AllPotentiallyErroneous, exchanges }), excluded);
    }
    match brti_from_books(&kept) {
        Ok((value, index)) => {
            let mut venues: Vec<String> = exchanges.iter().filter(|d| d.status.used()).map(|d| d.venue.clone()).collect();
            venues.sort();
            (Ok(ReplicaOutput { value, index, venues, median_mid: med, exchanges }), excluded)
        }
        Err(failure) => (Err(ReplicaError { failure, exchanges }), excluded),
    }
}

/// Stateful wrapper holding the 5.3 hysteresis set (`brti.Replica`).
#[derive(Clone, Debug, Default)]
pub struct Replica {
    pub excluded: BTreeSet<String>,
}

impl Replica {
    pub fn calculate(&mut self, books: &[(String, BookSnapshot)], calc_ns: i64) -> Result<ReplicaOutput, ReplicaError> {
        let (res, next) = calculate(books, calc_ns, &self.excluded);
        self.excluded = next;
        res
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ladder(mid: f64, half: f64, step: f64, n: usize, size: f64) -> (Vec<Level>, Vec<Level>) {
        ((0..n).map(|i| (mid - half - i as f64 * step, size)).collect(), (0..n).map(|i| (mid + half + i as f64 * step, size)).collect())
    }

    fn kde_full(sample: &[f64]) -> f64 {
        // The reference loop without the window, for bit-identity of the windowed search.
        let n = sample.len();
        let (_, w) = winsorize(sample);
        let m = py_sum(w.iter().copied()) / n as f64;
        let sd = (py_sum(w.iter().map(|&x| py_pow2(x - m))) / (n - 1) as f64).sqrt();
        let h = 1.06 * sd * py_pow(n as f64, -0.2);
        let (lo, hi) = (w[0], w[n - 1]);
        let norm = n as f64 * h * (2.0 * std::f64::consts::PI).sqrt();
        let (mut best, mut best_f) = (lo, -1.0);
        for j in 0..KDE_GRID {
            let x = lo + (hi - lo) * j as f64 / 4000.0;
            let f = py_sum(w.iter().map(|&xi| (-0.5 * py_pow2((x - xi) / h)).exp())) / norm;
            if f > best_f {
                best = x;
                best_f = f;
            }
        }
        best
    }

    #[test]
    fn windowed_kde_is_bit_identical() {
        let mut seed = 42u64;
        let mut rnd = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (seed >> 11) as f64 / (1u64 << 53) as f64
        };
        for case in 0..30 {
            let n = 4 + case * 23;
            let mut v: Vec<f64> = (0..n)
                .map(|i| match case % 5 {
                    0 => (rnd() * 6.0 - 3.0).exp(),
                    4 => {
                        if i % 2 == 0 {
                            0.1
                        } else {
                            0.3
                        }
                    }
                    1 => {
                        if i % 3 == 0 {
                            0.5
                        } else {
                            0.25 + rnd() * 1e-3
                        }
                    }
                    2 => 1e-8 + rnd() * 1e-6,
                    _ => (rnd() * 100.0).round() / 100.0 + 0.01,
                })
                .collect();
            sort_f64(&mut v);
            assert_eq!(kde_mode(&v).to_bits(), kde_full(&v).to_bits(), "case {case}");
        }
    }

    #[test]
    fn symmetric_book_returns_mid() {
        let (b, a) = ladder(90000.0, 1.0, 0.5, 200, 0.4);
        let (v, d) = brti_from_books(&[(&b, &a)]).unwrap();
        assert!((v - 90000.0).abs() < 1e-6);
        assert!(d.levels_used > 1);
    }

    #[test]
    fn delayed_crossed_and_empty_books_are_excluded_with_reasons() {
        let (b, a) = ladder(90000.0, 1.0, 0.5, 100, 0.4);
        let (sb, sa) = ladder(80000.0, 1.0, 0.5, 100, 0.4);
        let s = 1_000_000_000i64;
        let books = vec![
            ("a".to_string(), BookSnapshot { bids: b, asks: a, retrieved_ns: 100 * s }),
            ("b".to_string(), BookSnapshot { bids: sb, asks: sa, retrieved_ns: 80 * s }),
            ("c".to_string(), BookSnapshot { bids: vec![(90010.0, 1.0)], asks: vec![(90000.0, 1.0)], retrieved_ns: 100 * s }),
            ("d".to_string(), BookSnapshot { bids: vec![(0.0, 1.0)], asks: vec![(90000.0, 1.0)], retrieved_ns: 100 * s }),
        ];
        let out = Replica::default().calculate(&books, 100 * s).unwrap();
        assert_eq!(out.venues, vec!["a"]);
        assert!(matches!(out.exchanges[1].status, VenueStatus::Delayed { lag_ns } if lag_ns == 20 * s));
        assert_eq!(out.exchanges[2].status, VenueStatus::Crossed);
        assert_eq!(out.exchanges[3].status, VenueStatus::EmptySide);
    }

    #[test]
    fn side_consumed_and_empty_input() {
        let b = [(101.0, 5.0)];
        let a = [(100.0, 1.0), (100.5, 1.0)];
        assert_eq!(brti_from_books(&[(&b, &a)]), Err(Failure::SideConsumed));
        assert_eq!(brti_from_books(&[]), Err(Failure::NoUsableBooks));
    }
}
