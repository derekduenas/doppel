//! BRTI constituent replica: port of PERELMAN `brti.py` (CME CF Real Time Indices Methodology
//! v17.0: 4.1 calculation, 5 contingency rules, 6 parameters). `brti.py` is the oracle; this
//! follows it step for step, including its float arithmetic (CPython `sum`, libm `pow` for `** 2`).
//! Speedups never change results (see `docs/fast-fair.md`): venue books are merged rather than
//! re-sorted, the price-volume walk uses a cursor, the KDE mode is searched with certified bounds
//! before the reference arithmetic picks the winner, and `x * x` stands in for libm `pow(x, 2)`
//! only where pow's measured error bound (0.515 ulp) forces the same double.
//!
//! Two entry points: [`Replica::calculate`] (exact, from a full snapshot) and
//! [`Replica::apply`] (incremental: one venue's new book, and only the 5% band the methodology
//! reads; it falls back to the full books whenever the band cannot decide the result).
//!
//! Pure functions over in-memory books. No I/O, no clock: the caller passes `calc_ns`.

use std::borrow::Cow;
use std::cmp::Ordering;
use std::collections::BTreeSet;

use crate::pymath::{py_pow, py_pow2, py_sum, Neumaier};

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
    let bids: Vec<Cow<[Level]>> = books.iter().map(|b| sorted_side(b.0, true)).collect();
    let asks: Vec<Cow<[Level]>> = books.iter().map(|b| sorted_side(b.1, false)).collect();
    let (rb, ra): (Vec<&[Level]>, Vec<&[Level]>) = (bids.iter().map(|c| c.as_ref()).collect(), asks.iter().map(|c| c.as_ref()).collect());
    (merge_sides(&rb, true), merge_sides(&ra, false))
}

fn price_order(desc: bool) -> impl Fn(&Level, &Level) -> Ordering + Copy {
    move |a: &Level, b: &Level| if desc { b.0.total_cmp(&a.0) } else { a.0.total_cmp(&b.0) }
}

/// One venue side in price order: borrowed if already ordered, else a stable-sorted copy.
fn sorted_side(levels: &[Level], desc: bool) -> Cow<'_, [Level]> {
    let ord = price_order(desc);
    if levels.windows(2).all(|w| ord(&w[0], &w[1]) != Ordering::Greater) {
        Cow::Borrowed(levels)
    } else {
        let mut v = levels.to_vec();
        v.sort_by(&ord);
        Cow::Owned(v)
    }
}

/// Merge venue sides that are each in price order, then fold equal prices. Ties go to the earlier
/// venue, so the order (and each price's summation order) is exactly that of a stable sort of the
/// concatenated books, which is what Python's dict accumulation produces.
fn merge_sides(sides: &[&[Level]], desc: bool) -> Vec<Level> {
    let merged = if desc { merge_tree(sides, price_order(true)) } else { merge_tree(sides, price_order(false)) };
    let mut out: Vec<Level> = Vec::with_capacity(merged.len());
    for (p, sz) in merged.iter().copied() {
        match out.last_mut() {
            Some(last) if last.0 == p => last.1 += sz,
            _ => out.push((p, 0.0 + sz)),
        }
    }
    out
}

/// Stable k-way merge as a balanced tree of two-way merges (left wins ties).
fn merge_tree<'a, F: Fn(&Level, &Level) -> Ordering + Copy>(sides: &[&'a [Level]], ord: F) -> Cow<'a, [Level]> {
    if sides.is_empty() {
        return Cow::Owned(Vec::new());
    }
    if sides.len() == 1 {
        return Cow::Borrowed(sides[0]);
    }
    let (l, r) = sides.split_at(sides.len() / 2);
    let (a, b) = (merge_tree(l, ord), merge_tree(r, ord));
    let mut out = Vec::with_capacity(a.len() + b.len());
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        if ord(&b[j], &a[i]) == Ordering::Less {
            out.push(b[j]);
            j += 1;
        } else {
            out.push(a[i]);
            i += 1;
        }
    }
    out.extend_from_slice(&a[i..]);
    out.extend_from_slice(&b[j..]);
    Cow::Owned(out)
}

/// Eq. 4a/4b: levels within 5% of best, at least min(|side|, 50). Returns (ac, bc). Sides are in
/// price order, so the levels inside the band are a prefix (NaN prices, possible only on the
/// uncleaned direct path, can sit at either end in total order; then the reference scan is used).
fn sample_counts(bids: &[Level], asks: &[Level]) -> (usize, usize) {
    let lim_a = (1.0 + CAP_BAND) * asks[0].0;
    let lim_b = (1.0 - CAP_BAND) * bids[0].0;
    let count = |side: &[Level], inside: &dyn Fn(f64) -> bool| {
        if side[0].0.is_nan() || side[side.len() - 1].0.is_nan() {
            side.iter().enumerate().filter(|(_, l)| inside(l.0)).map(|(i, _)| i + 1).max().unwrap_or(0)
        } else {
            side.partition_point(|l| inside(l.0))
        }
    };
    let ac = count(asks, &|p| p <= lim_a);
    let bc = count(bids, &|p| p >= lim_b);
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

/// Ascending in `f64::total_cmp` order. For larger inputs: one counting pass over the top 16
/// bits of an order-preserving key (sign, exponent, 4 mantissa bits), then a sort of each small
/// bucket. Equal keys are identical values, so any correct sort gives the same array.
fn sort_f64(v: &mut [f64]) {
    if v.len() < 256 {
        v.sort_by(|a, b| a.total_cmp(b));
        return;
    }
    let key = |x: f64| {
        let b = x.to_bits();
        b ^ ((((b as i64) >> 63) as u64) | (1 << 63))
    };
    let keys: Vec<u64> = v.iter().map(|&x| key(x)).collect();
    let top = |k: u64| (k >> 48) as usize;
    let (lo, hi) = keys.iter().fold((usize::MAX, 0), |(lo, hi), &k| (lo.min(top(k)), hi.max(top(k))));
    let mut sorted: Vec<u64>;
    if hi - lo >= 1 << 14 {
        sorted = keys;
        sorted.sort_unstable();
    } else {
        let mut start = vec![0usize; hi - lo + 2];
        for &k in &keys {
            start[top(k) - lo + 1] += 1;
        }
        for i in 1..start.len() {
            start[i] += start[i - 1];
        }
        let mut next = start.clone();
        sorted = vec![0u64; keys.len()];
        for &k in &keys {
            let d = top(k) - lo;
            sorted[next[d]] = k;
            next[d] += 1;
        }
        for w in start.windows(2) {
            if w[1] - w[0] > 1 {
                sorted[w[0]..w[1]].sort_unstable();
            }
        }
    }
    for (x, k) in v.iter_mut().zip(sorted) {
        *x = f64::from_bits(if k >> 63 == 1 { k ^ (1 << 63) } else { !k });
    }
}

/// `xs.iter().map(f)`, computing `f` once per run of bit-identical inputs (sorted samples of
/// order sizes repeat values a lot); the sequence of outputs is unchanged.
fn reuse_equal<'a, F: Fn(f64) -> f64 + 'a>(xs: &'a [f64], f: F) -> impl Iterator<Item = f64> + 'a {
    let mut last: Option<(u64, f64)> = None;
    xs.iter().map(move |&x| match last {
        Some((b, y)) if b == x.to_bits() => y,
        _ => {
            let y = f(x);
            last = Some((x.to_bits(), y));
            y
        }
    })
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
    let sigma = if n > 1 { (py_sum(reuse_equal(&w, |x| py_pow2(x - wmean))) / (n - 1) as f64).sqrt() } else { 0.0 };
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
    let sd = (py_sum(reuse_equal(&w, |x| py_pow2(x - m))) / (n - 1) as f64).sqrt();
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
    let exact = |x: f64, terms: &[f64]| py_sum(reuse_equal(terms, |xi| (-0.5 * py_pow2((x - xi) / h)).exp())) / norm;
    // Window arithmetic needs 40 h far above the float spacing of the sample values.
    let windowed = 40.0 * h > 1e-9 * lo.abs().max(hi.abs()) && n <= 1_000_000;
    if !windowed {
        return grid_argmax((0..KDE_GRID).map(|j| (grid_x(j), exact(grid_x(j), &w))));
    }
    // Pass 1 finds every grid point whose reference density could be the maximum, with rigorous
    // error bounds (see `kde_candidates`); pass 2 evaluates only those with the reference
    // arithmetic, in grid order, so the result is the reference's first maximum.
    let cands = kde_candidates(&w, h, &grid_x);
    let mut win = Window::new(&w, 40.0 * h);
    grid_argmax(cands.into_iter().map(|j| {
        let x = grid_x(j);
        (x, exact(x, win.at(x)))
    }))
}

/// Upper bound on `max_{0 <= u <= umax} exp(-u^2/2 + t u) (t u)^p / p!`: the Taylor remainder of
/// `exp(t u)` after `p` terms, weighted by the kernel factor `exp(-u^2/2)`.
fn taylor_tail(p: usize, t: f64, umax: f64) -> f64 {
    if t <= 0.0 {
        return 0.0;
    }
    let u = ((t + (t * t + 4.0 * p as f64).sqrt()) / 2.0).min(umax);
    let ln_fact: f64 = (2..=p).map(|k| (k as f64).ln()).sum();
    (-0.5 * u * u + t * u + p as f64 * (t * u).ln() - ln_fact).exp()
}

/// Grid indices (ascending) whose reference KDE value `F(x) = sum_i K((x - xi) / h)` may equal
/// the maximum over the grid.
///
/// The grid is cut into blocks of half-width <= h / 2. Each block gets a cheap upper bound from
/// the sample binned at width h / 4 (count x kernel at the bin-to-block distance). Blocks are
/// then expanded in decreasing bound order: around the block centre c, with t = (x - c) / h and
/// u_i = (xi - c) / h, `K = exp(-t^2/2) exp(-u_i^2/2) exp(t u_i)`, so F is `exp(-t^2/2)` times a
/// degree-p polynomial in t whose coefficients cost one pass over the sample. That gives F at
/// every grid point of the block with an explicit error bound (Taylor remainder, float rounding,
/// the reference's own rounding, dropped tails beyond 12 h). Expansion stops when the next
/// block's upper bound is below the best certified lower bound; survivors are the grid points
/// whose value plus error reaches that lower bound.
fn kde_candidates<G: Fn(usize) -> f64>(w: &[f64], h: f64, grid_x: &G) -> Vec<usize> {
    const Z: f64 = 12.0; // kernel terms beyond 12 h are < exp(-72)
    const T: f64 = 0.5; // block half-width in bandwidths
    const EPS: f64 = f64::EPSILON;
    let n = w.len();
    let nf = n as f64;
    let (lo, hi) = (grid_x(0), grid_x(KDE_GRID - 1));
    let scale = lo.abs().max(hi.abs()).max(w[0].abs()).max(w[n - 1].abs());
    let slack = 8.0 * EPS * scale;
    let tail = nf * (-70.0f64).exp();
    // the reference's own rounding (y, pow, exp, Neumaier) relative to exact math: < 1e-12 F <= 1e-12 n
    let ref_err = 1e-11 * nf;
    let delta = (hi - lo) / (KDE_GRID - 1) as f64;
    let per = if delta > 0.0 { ((2.0 * T * h / delta).floor() as usize + 1).clamp(1, KDE_GRID) } else { KDE_GRID };

    let beta = h / 4.0;
    let mut bins: Vec<(f64, f64, f64)> = Vec::new(); // (min, max, count)
    for &x in w {
        match bins.last_mut() {
            Some(b) if x - b.0 <= beta => {
                b.1 = x;
                b.2 += 1.0;
            }
            _ => bins.push((x, x, 1.0)),
        }
    }
    let mut blocks: Vec<(usize, usize, f64)> = (0..KDE_GRID.div_ceil(per))
        .map(|bi| {
            let (a, b) = (bi * per, ((bi + 1) * per).min(KDE_GRID));
            let (x0, x1) = (grid_x(a), grid_x(b - 1));
            let (c, r) = ((x0 + x1) / 2.0, (x1 - x0) / 2.0);
            let reach = Z * h + r + slack;
            let i0 = bins.partition_point(|bn| bn.1 < c - reach);
            let i1 = bins.partition_point(|bn| bn.0 <= c + reach);
            let up: f64 = bins[i0..i1]
                .iter()
                .map(|bn| {
                    let y = ((bn.0 - (c + r)).max((c - r) - bn.1) - slack).max(0.0) / h;
                    bn.2 * (-0.5 * (y * y)).exp()
                })
                .sum();
            (a, b, up * (1.0 + 1e-10) + 2.0 * tail + ref_err)
        })
        .collect();
    blocks.sort_by(|x, y| y.2.total_cmp(&x.2));

    let mut lower = f64::NEG_INFINITY;
    let mut vals: Vec<(usize, f64, f64)> = Vec::new(); // (grid index, estimate, error bound)
    let (mut coef, mut us, mut pw): (Vec<f64>, Vec<f64>, Vec<f64>) = (Vec::new(), Vec::new(), Vec::new());
    for &(a, b, up) in &blocks {
        if up < lower {
            break;
        }
        let (x0, x1) = (grid_x(a), grid_x(b - 1));
        let c = (x0 + x1) / 2.0;
        let t_max = ((x1 - x0) / 2.0 + slack) / h;
        let umax = Z + t_max;
        let i0 = w.partition_point(|&x| x < c - umax * h);
        let i1 = w.partition_point(|&x| x <= c + umax * h);
        let nw = (i1 - i0) as f64;
        let mut p = 1usize;
        while p < 64 && taylor_tail(p, t_max, umax) > 1e-13 {
            p += 1;
        }
        // coef[k] = sum_i exp(-u_i^2/2) u_i^k, terms by power with four independent partial sums
        // (summation order is free: the float error bound below holds for any order).
        us.clear();
        us.extend(w[i0..i1].iter().map(|&xi| (xi - c) / h));
        pw.clear();
        pw.extend(us.iter().map(|&u| (-0.5 * (u * u)).exp()));
        coef.clear();
        for _ in 0..p {
            let mut acc = [0.0f64; 4];
            let (pq, pr) = pw.as_chunks_mut::<4>();
            let (uq, ur) = us.as_chunks::<4>();
            for (p4, u4) in pq.iter_mut().zip(uq) {
                for l in 0..4 {
                    acc[l] += p4[l];
                    p4[l] *= u4[l];
                }
            }
            for (p1, u1) in pr.iter_mut().zip(ur) {
                acc[0] += *p1;
                *p1 *= u1;
            }
            coef.push((acc[0] + acc[1]) + (acc[2] + acc[3]));
        }
        let mut fact = 1.0f64;
        for (k, ck) in coef.iter_mut().enumerate() {
            if k > 0 {
                fact *= k as f64;
            }
            *ck /= fact;
        }
        let bound_b = nw * (0.5 * t_max * t_max).exp(); // >= sum_i exp(-u^2/2 + |u| t_max)
        let err = nw * taylor_tail(p, t_max, umax)
            + (nw + 2.0 * p as f64 + 8.0) * EPS * bound_b
            + nf * 0.61 / h * 8.0 * EPS * (scale + h * t_max)
            + ref_err
            + 2.0 * tail;
        for j in a..b {
            let t = (grid_x(j) - c) / h;
            let poly = coef.iter().rev().fold(0.0f64, |acc, &ck| acc * t + ck);
            let est = (-0.5 * (t * t)).exp() * poly;
            lower = lower.max(est - err);
            vals.push((j, est, err));
        }
    }
    let mut out: Vec<usize> = vals.into_iter().filter(|v| v.1 + v.2 >= lower).map(|v| v.0).collect();
    out.sort_unstable();
    out
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
    index_staged(books, &mut |_| {})
}

/// Wall-clock ns per named stage.
pub type Laps = Vec<(&'static str, u64)>;

/// [`brti_from_books`] with wall-clock ns per stage, for profiling.
pub fn brti_from_books_profiled(books: &[(&[Level], &[Level])]) -> (Result<(f64, IndexDiag), Failure>, Laps) {
    let mut laps = Vec::new();
    let mut t = std::time::Instant::now();
    let res = index_staged(books, &mut |name| {
        laps.push((name, t.elapsed().as_nanos() as u64));
        t = std::time::Instant::now();
    });
    (res, laps)
}

fn index_staged(books: &[(&[Level], &[Level])], lap: &mut dyn FnMut(&'static str)) -> Result<(f64, IndexDiag), Failure> {
    if books.is_empty() {
        return Err(Failure::NoUsableBooks);
    }
    let (bids, asks) = consolidate(books);
    lap("consolidate");
    if bids.is_empty() || asks.is_empty() {
        return Err(Failure::EmptySide);
    }
    match index_core(bids, asks, Deep::default(), lap) {
        Ok(r) => r,
        Err(NeedFull) => unreachable!("a full book never needs more depth"),
    }
}

/// What a near-band computation knows about the consolidated levels beyond its band, per side:
/// `None` when the band holds the whole side, else an upper bound on the side's capped total.
#[derive(Clone, Copy, Debug, Default)]
struct Deep {
    bids: Option<f64>,
    asks: Option<f64>,
}

/// The near band could not decide the result exactly; recompute on full books.
#[derive(Debug)]
struct NeedFull;

/// Everything after consolidation. With `deep` empty, `bids`/`asks` are the full consolidated
/// sides and this is `brti.py` step for step. Otherwise they are a price-ordered prefix (band) of
/// each side that contains every level the methodology reads except the side totals; those only
/// enter through comparisons (static vs KDE spacing, the curve's depth limit, the step cap), which
/// are decided from bounds, or `NeedFull` when a bound cannot decide them.
fn index_core(
    mut bids: Vec<Level>,
    mut asks: Vec<Level>,
    deep: Deep,
    lap: &mut dyn FnMut(&'static str),
) -> Result<Result<(f64, IndexDiag), Failure>, NeedFull> {
    const EPS: f64 = f64::EPSILON;
    let (db, da) = (deep.bids.is_some(), deep.asks.is_some());
    // `min(len(side), 50)` in Eq. 4a/4b uses the full side length.
    if (db && bids.len() < CAP_SAMPLE_MIN) || (da && asks.len() < CAP_SAMPLE_MIN) {
        return Err(NeedFull);
    }
    let cap = order_size_cap(&bids, &asks);
    lap("order_size_cap");
    for l in bids.iter_mut().chain(asks.iter_mut()) {
        l.1 = l.1.min(cap);
    }
    uncross(&mut bids, &mut asks);
    lap("apply_cap+uncross");
    if (db && bids.len() < CAP_SAMPLE_MIN) || (da && asks.len() < CAP_SAMPLE_MIN) {
        return Err(NeedFull);
    }
    if bids.is_empty() || asks.is_empty() {
        return Ok(Err(Failure::SideConsumed));
    }
    // Section 4.1.4, Eq. 6a-7 (spacing on the capped, uncrossed book: `brti.py` AMBIGUITY, kept).
    let ta = py_sum(asks.iter().map(|l| l.1));
    let tb = py_sum(bids.iter().map(|l| l.1));
    let (ac, bc) = sample_counts(&bids, &asks);
    let bid_sum = py_sum(bids[..bc].iter().map(|l| l.1));
    let ask_sum = py_sum(asks[..ac].iter().map(|l| l.1));
    let side = if bid_sum < ask_sum { &bids[..bc] } else { &asks[..ac] };
    let mut sample: Vec<f64> = side.iter().map(|l| l.1).collect();
    sort_f64(&mut sample);
    lap("spacing_sums+sample");
    let s_kde = kde_mode(&sample);
    lap("kde_mode");
    // Full-side totals: exact, or [lower, upper] bounds (a prefix of non-negative sizes).
    let lo = |t: f64, d: bool| if d { t * (1.0 - 8.0 * EPS) } else { t };
    let hi = |t: f64, d: Option<f64>| d.unwrap_or(t);
    let exact = !db && !da;
    let s = if exact {
        (ta.min(tb) / TARGET_LEVELS).min(s_kde)
    } else if s_kde < lo(ta, da).min(lo(tb, db)) / TARGET_LEVELS * (1.0 - 4.0 * EPS) {
        s_kde
    } else {
        return Err(NeedFull);
    };
    let (d_lo, d_hi) = (lo(tb, db).min(lo(ta, da)), hi(tb, deep.bids).min(hi(ta, deep.asks)));
    lap("depth_sums");
    let max_steps = MAX_CURVE_STEPS as f64;
    if exact {
        if d_lo / s > max_steps {
            return Ok(Err(Failure::CurveTooFine));
        }
    } else if d_lo / s > max_steps * (1.0 + 1e-9) {
        return Ok(Err(Failure::CurveTooFine));
    } else if d_hi / s > max_steps * (1.0 - 1e-9) {
        return Err(NeedFull);
    }
    // `i * s <= depth + 1e-12`, decided from the bounds when the depth is not exact.
    let within = |v: f64| -> Result<bool, NeedFull> {
        if exact || v <= d_lo + 1e-12 {
            Ok(v <= d_lo + 1e-12)
        } else if v > d_hi + 1e-12 {
            Ok(false)
        } else {
            Err(NeedFull)
        }
    };
    let (mut wa, mut wb) = (Walker::new(&asks), Walker::new(&bids));
    // The curve as runs of steps [i0, i1) sharing one mid; step i sits at volume v = i * s.
    let mut runs: Vec<(u64, u64, f64)> = Vec::new();
    let mut v_t = s;
    let mut i = 1u64;
    // (walker positions, their mid): the deviation test already passed for this pair of levels.
    let mut cur: Option<(usize, usize, f64)> = None;
    loop {
        let v = i as f64 * s;
        if !within(v)? {
            break;
        }
        // If both cumulative sizes exceed any possible target (tgt <= (v + s)(1 + 2.1 eps)),
        // neither walker moves, the levels and their mid are unchanged, and the test passes again.
        let mid = match cur {
            Some((_, _, m)) if wa.cum.min(wb.cum) >= (v + s) * (1.0 + 4.0 * EPS) => m,
            _ => {
                let tgt = s * (v / s - 1e-12).ceil();
                let (pa, pb) = (wa.price_at(tgt), wb.price_at(tgt));
                let (a, b) = match (pa, pb) {
                    (Some(a), Some(b)) => (a, b),
                    _ if (pa.is_none() && !da) || (pb.is_none() && !db) => break,
                    _ => return Err(NeedFull), // ran past the band
                };
                match cur {
                    Some((ia, ib, m)) if ia == wa.i && ib == wb.i => m,
                    _ => {
                        let mid = (a + b) / 2.0;
                        if a / mid - 1.0 > DEVIATION_FROM_MID {
                            break; // Eq. 2: stop at the first breach
                        }
                        cur = Some((wa.i, wb.i, mid));
                        mid
                    }
                }
            }
        };
        // Steps after this one stay on the same levels while the fast-path test holds and the
        // depth limit is surely not reached; both tests are monotone in the step, so the run's
        // last step `k` is found by a short local search around its estimate.
        let m = wa.cum.min(wb.cum);
        let batch = |k: u64| {
            let vk = k as f64 * s;
            vk <= d_lo + 1e-12 && m >= (vk + s) * (1.0 + 4.0 * EPS)
        };
        let est = ((m / (1.0 + 4.0 * EPS) - s).min(d_lo + 1e-12) / s).floor();
        let mut k = if est.is_finite() && est > i as f64 { (est as u64).max(i) } else { i };
        while batch(k + 1) {
            k += 1;
        }
        while k > i && !batch(k) {
            k -= 1;
        }
        match runs.last_mut() {
            Some(r) if r.2.to_bits() == mid.to_bits() => r.1 = k + 1,
            _ => runs.push((i, k + 1, mid)),
        }
        v_t = k as f64 * s;
        i = k + 1;
    }
    let levels_used = (i - 1) as usize;
    v_t = v_t.max(s);
    // Grid points (v, mid) in order; an empty curve is floored at the spacing (index = mid at s).
    let floor_mid = if levels_used == 0 {
        let (pa, pb) = (Walker::new(&asks).price_at(s), Walker::new(&bids).price_at(s));
        match (pa, pb) {
            (Some(a), Some(b)) => Some((a + b) / 2.0),
            _ if (pa.is_none() && !da) || (pb.is_none() && !db) => return Ok(Err(Failure::SideConsumed)),
            _ => return Err(NeedFull),
        }
    } else {
        None
    };
    lap("curve_walk");
    let lam = 1.0 / (LAMBDA_FACTOR * v_t);
    let (mut nf, mut num) = (Neumaier::default(), Neumaier::default());
    let mut add = |v: f64, m: f64| {
        let w = lam * (-lam * v).exp();
        nf.add(w);
        num.add(m * w);
    };
    match floor_mid {
        Some(m) => add(s, m),
        None => {
            for &(i0, i1, m) in &runs {
                for k in i0..i1 {
                    add(k as f64 * s, m);
                }
            }
        }
    }
    let value = num.total() / nf.total();
    lap("weights");
    Ok(Ok((value, IndexDiag { cap, spacing: s, utilized_depth: v_t, levels_used: levels_used.max(1) })))
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

/// A venue's cleaned book as the contingency rules see it.
struct VenueView<'a> {
    venue: &'a str,
    retrieved_ns: i64,
    bids: &'a [Level],
    asks: &'a [Level],
    /// (best bid, best ask) of the cleaned book, if both sides are non-empty.
    best: Option<(f64, f64)>,
}

/// Outcome of the section 5 rules: per-venue diagnostics, the kept venues (indices into the
/// views, in order) and the median mid, or the failure.
struct Screen {
    exchanges: Vec<VenueDiag>,
    kept: Vec<usize>,
    median_mid: f64,
}

/// Sections 5.1-5.3 (`Replica.calculate` up to `brti_from_books`), updating `excluded`.
fn screen(views: &[VenueView], calc_ns: i64, excluded: &mut BTreeSet<String>) -> Result<Screen, ReplicaError> {
    let mut exchanges: Vec<VenueDiag> = Vec::with_capacity(views.len());
    let mut usable: Vec<(usize, f64)> = Vec::new(); // (view index, mid)
    for (vi, v) in views.iter().enumerate() {
        let lag_ns = calc_ns - v.retrieved_ns;
        let venue = v.venue.to_string();
        if lag_ns > RETRIEVAL_LAG_THRESHOLD_NS {
            exchanges.push(VenueDiag { venue, status: VenueStatus::Delayed { lag_ns }, mid: None, levels: (0, 0) });
            continue;
        }
        let levels = (v.bids.len(), v.asks.len());
        let Some((best_bid, best_ask)) = v.best else {
            exchanges.push(VenueDiag { venue, status: VenueStatus::EmptySide, mid: None, levels });
            continue;
        };
        if best_bid >= best_ask {
            exchanges.push(VenueDiag { venue, status: VenueStatus::Crossed, mid: None, levels });
            continue;
        }
        let mid = (best_bid + best_ask) / 2.0;
        exchanges.push(VenueDiag { venue, status: VenueStatus::Used, mid: Some(mid), levels });
        usable.push((vi, mid));
    }
    if usable.is_empty() {
        return Err(ReplicaError { failure: Failure::AllDelayedOrErroneous, exchanges });
    }
    let med = median(&usable.iter().map(|u| u.1).collect::<Vec<_>>());
    for &(vi, mid) in &usable {
        let dev = (mid / med - 1.0).abs();
        let d = &mut exchanges[vi];
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
    let kept: Vec<usize> = usable.iter().map(|u| u.0).filter(|&vi| exchanges[vi].status.used()).collect();
    if kept.is_empty() {
        return Err(ReplicaError { failure: Failure::AllPotentiallyErroneous, exchanges });
    }
    Ok(Screen { exchanges, kept, median_mid: med })
}

fn finish(sc: Screen, index: Result<(f64, IndexDiag), Failure>) -> Result<ReplicaOutput, ReplicaError> {
    match index {
        Ok((value, index)) => {
            let mut venues: Vec<String> = sc.exchanges.iter().filter(|d| d.status.used()).map(|d| d.venue.clone()).collect();
            venues.sort();
            Ok(ReplicaOutput { value, index, venues, median_mid: sc.median_mid, exchanges: sc.exchanges })
        }
        Err(failure) => Err(ReplicaError { failure, exchanges: sc.exchanges }),
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
    // Delayed books are never cleaned (their diagnostics report no levels, as before).
    let cleaned: Vec<(Vec<Level>, Vec<Level>)> = books
        .iter()
        .map(|(_, b)| {
            if calc_ns - b.retrieved_ns > RETRIEVAL_LAG_THRESHOLD_NS {
                (Vec::new(), Vec::new())
            } else {
                (clean(&b.bids), clean(&b.asks))
            }
        })
        .collect();
    let views: Vec<VenueView> = books
        .iter()
        .zip(&cleaned)
        .map(|((venue, b), (bids, asks))| {
            let best = (!bids.is_empty() && !asks.is_empty()).then(|| {
                (bids.iter().map(|l| l.0).fold(f64::NEG_INFINITY, f64::max), asks.iter().map(|l| l.0).fold(f64::INFINITY, f64::min))
            });
            VenueView { venue, retrieved_ns: b.retrieved_ns, bids, asks, best }
        })
        .collect();
    let sc = match screen(&views, calc_ns, &mut excluded) {
        Ok(sc) => sc,
        Err(e) => return (Err(e), excluded),
    };
    let kept: Vec<(&[Level], &[Level])> = sc.kept.iter().map(|&vi| (views[vi].bids, views[vi].asks)).collect();
    let index = brti_from_books(&kept);
    (finish(sc, index), excluded)
}

/// A venue's latest book, cleaned and in price order (bids descending), kept by [`Replica`].
#[derive(Clone, Debug)]
struct VenueState {
    venue: String,
    retrieved_ns: i64,
    bids: Vec<Level>,
    asks: Vec<Level>,
    /// Upper bounds on the total size of each side (sizes are positive after cleaning).
    bid_total_hi: f64,
    ask_total_hi: f64,
}

impl VenueState {
    fn new(venue: &str, book: &BookSnapshot) -> Self {
        let side = |levels: &[Level], desc: bool| {
            let v = clean(levels);
            match sorted_side(&v, desc) {
                Cow::Owned(sorted) => sorted,
                Cow::Borrowed(_) => v,
            }
        };
        let (bids, asks) = (side(&book.bids, true), side(&book.asks, false));
        let hi = |v: &[Level]| v.iter().map(|l| l.1).sum::<f64>() * (1.0 + 1e-9);
        VenueState {
            venue: venue.to_string(),
            retrieved_ns: book.retrieved_ns,
            bid_total_hi: hi(&bids),
            ask_total_hi: hi(&asks),
            bids,
            asks,
        }
    }

    fn view(&self) -> VenueView<'_> {
        let best = match (self.bids.first(), self.asks.first()) {
            (Some(b), Some(a)) => Some((b.0, a.0)),
            _ => None,
        };
        VenueView { venue: &self.venue, retrieved_ns: self.retrieved_ns, bids: &self.bids, asks: &self.asks, best }
    }
}

/// The index from the kept venues' price-ordered books, reading only the band the methodology
/// can use: every ask up to 1.05 x the first consolidated ask above the best bid (which bounds
/// both 5% sample bands, before and after uncrossing, and the curve walk), and symmetrically for
/// bids. Falls back to the full books when the band cannot decide the result exactly.
fn index_banded(kept: &[&VenueState], fallbacks: &mut u64, lap: &mut dyn FnMut(&'static str)) -> Result<(f64, IndexDiag), Failure> {
    let best_bid = kept.iter().map(|v| v.bids[0].0).fold(f64::NEG_INFINITY, f64::max);
    let best_ask = kept.iter().map(|v| v.asks[0].0).fold(f64::INFINITY, f64::min);
    // First consolidated ask above the best bid / bid below the best ask: never uncrossed away.
    let first_ask =
        kept.iter().filter_map(|v| v.asks.get(v.asks.partition_point(|l| l.0 <= best_bid))).map(|l| l.0).fold(f64::INFINITY, f64::min);
    let first_bid =
        kept.iter().filter_map(|v| v.bids.get(v.bids.partition_point(|l| l.0 >= best_ask))).map(|l| l.0).fold(f64::NEG_INFINITY, f64::max);
    let lim_a = first_ask * (1.0 + CAP_BAND) * (1.0 + 1e-9);
    let lim_b = first_bid * (1.0 - CAP_BAND) * (1.0 - 1e-9);
    let near_b: Vec<&[Level]> = kept.iter().map(|v| &v.bids[..v.bids.partition_point(|l| l.0 >= lim_b)]).collect();
    let near_a: Vec<&[Level]> = kept.iter().map(|v| &v.asks[..v.asks.partition_point(|l| l.0 <= lim_a)]).collect();
    let deep_b = kept.iter().zip(&near_b).any(|(v, n)| n.len() < v.bids.len());
    let deep_a = kept.iter().zip(&near_a).any(|(v, n)| n.len() < v.asks.len());
    let deep = Deep {
        bids: deep_b.then(|| kept.iter().map(|v| v.bid_total_hi).sum::<f64>() * (1.0 + 1e-9)),
        asks: deep_a.then(|| kept.iter().map(|v| v.ask_total_hi).sum::<f64>() * (1.0 + 1e-9)),
    };
    let (bids, asks) = (merge_sides(&near_b, true), merge_sides(&near_a, false));
    lap("band+merge");
    if let Ok(r) = index_core(bids, asks, deep, lap) {
        return r;
    }
    *fallbacks += 1;
    let full: Vec<(&[Level], &[Level])> = kept.iter().map(|v| (v.bids.as_slice(), v.asks.as_slice())).collect();
    brti_from_books(&full)
}

/// Stateful replica (`brti.Replica`): the 5.3 hysteresis set, plus each venue's latest book for
/// the incremental [`Replica::apply`]. [`Replica::calculate`] is the exact full computation.
#[derive(Clone, Debug, Default)]
pub struct Replica {
    pub excluded: BTreeSet<String>,
    venues: Vec<VenueState>,
    /// `apply` calls whose band could not decide the result and recomputed on full books.
    pub fallbacks: u64,
}

impl Replica {
    /// Exact computation from a full snapshot of every venue's book.
    pub fn calculate(&mut self, books: &[(String, BookSnapshot)], calc_ns: i64) -> Result<ReplicaOutput, ReplicaError> {
        let (res, next) = calculate(books, calc_ns, &self.excluded);
        self.excluded = next;
        res
    }

    /// Replace one venue's book and recalculate at `calc_ns`. Only that venue is re-cleaned and
    /// re-sorted. The result, including every bit of the value, is what [`Replica::calculate`]
    /// returns on all current books in first-seen venue order, with the same hysteresis state.
    pub fn apply(&mut self, venue: &str, book: &BookSnapshot, calc_ns: i64) -> Result<ReplicaOutput, ReplicaError> {
        self.apply_staged(venue, book, calc_ns, &mut |_| {})
    }

    /// [`Replica::apply`] with wall-clock ns per stage, for profiling.
    pub fn apply_profiled(&mut self, venue: &str, book: &BookSnapshot, calc_ns: i64) -> (Result<ReplicaOutput, ReplicaError>, Laps) {
        let mut laps = Vec::new();
        let mut t = std::time::Instant::now();
        let res = self.apply_staged(venue, book, calc_ns, &mut |name| {
            laps.push((name, t.elapsed().as_nanos() as u64));
            t = std::time::Instant::now();
        });
        (res, laps)
    }

    fn apply_staged(
        &mut self,
        venue: &str,
        book: &BookSnapshot,
        calc_ns: i64,
        lap: &mut dyn FnMut(&'static str),
    ) -> Result<ReplicaOutput, ReplicaError> {
        let state = VenueState::new(venue, book);
        match self.venues.iter_mut().find(|v| v.venue == venue) {
            Some(v) => *v = state,
            None => self.venues.push(state),
        }
        lap("venue_update");
        let views: Vec<VenueView> = self.venues.iter().map(|v| v.view()).collect();
        let sc = screen(&views, calc_ns, &mut self.excluded)?;
        lap("screen");
        let kept: Vec<&VenueState> = sc.kept.iter().map(|&vi| &self.venues[vi]).collect();
        let index = index_banded(&kept, &mut self.fallbacks, lap);
        finish(sc, index)
    }

    /// The books `apply` holds, cleaned and in price order, in venue order.
    pub fn books(&self) -> Vec<(String, BookSnapshot)> {
        self.venues
            .iter()
            .map(|v| (v.venue.clone(), BookSnapshot { bids: v.bids.clone(), asks: v.asks.clone(), retrieved_ns: v.retrieved_ns }))
            .collect()
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
        let sd = (py_sum(w.iter().map(|&x| (x - m).abs().powf(std::hint::black_box(2.0)))) / (n - 1) as f64).sqrt();
        let h = 1.06 * sd * py_pow(n as f64, -0.2);
        let (lo, hi) = (w[0], w[n - 1]);
        let norm = n as f64 * h * (2.0 * std::f64::consts::PI).sqrt();
        let (mut best, mut best_f) = (lo, -1.0);
        for j in 0..KDE_GRID {
            let x = lo + (hi - lo) * j as f64 / 4000.0;
            let f = py_sum(w.iter().map(|&xi| (-0.5 * ((x - xi) / h).abs().powf(std::hint::black_box(2.0))).exp())) / norm;
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
    fn sort_f64_matches_total_cmp_sort() {
        let mut seed = 7u64;
        let mut rnd = || {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            seed
        };
        for case in 0..40 {
            let n = 1 + case * 97;
            let mut v: Vec<f64> = (0..n)
                .map(|_| match case % 4 {
                    0 => f64::from_bits(rnd()), // any bits, NaNs and all
                    1 => ((rnd() >> 11) as f64 / (1u64 << 53) as f64 * 20.0 - 14.0).exp(),
                    2 => ((rnd() % 50) as f64) * 0.25 - 3.0, // many duplicates, signs, zeros
                    _ => -(((rnd() >> 11) as f64) * 1e-300),
                })
                .collect();
            let mut want = v.clone();
            want.sort_by(|a, b| a.total_cmp(b));
            sort_f64(&mut v);
            assert_eq!(
                v.iter().map(|x| x.to_bits()).collect::<Vec<_>>(),
                want.iter().map(|x| x.to_bits()).collect::<Vec<_>>(),
                "case {case}"
            );
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
