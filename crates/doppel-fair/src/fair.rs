//! fair-v1: exact port of `doppel.lab.fair.fair_up_raw` (raw-BRTI accrued-average model).
//!
//! PM US settlement is the mean of raw 1 Hz BRTI over the 60 seconds `[end-60-lag, end-1-lag]`.
//! At decision second `d` the samples already received count as-is; each unknown future sample is
//! the last raw value plus a zero-drift random walk whose per-second variance is the mean squared
//! increment over the trailing `vol_window` seconds. `Var(final mean) = sigma^2 * sum_k w_k^2`,
//! `w_k` = fraction of the 60 samples at or after future second `k`.
//!
//! No look-ahead: only `raw[..=d - t0]` is read. Missing seconds are `NaN`.

use crate::pymath::np_pairwise_sum;

/// Samples in the settlement average.
pub const AVG_SECONDS: i64 = 60;
/// Default trailing window for the per-second variance (Python default).
pub const VOL_WINDOW: usize = 300;

/// Standard normal CDF, `0.5 * (1 + erf(z / sqrt 2))` as in `fair.phi`.
#[inline]
pub fn phi(z: f64) -> f64 {
    0.5 * (1.0 + libm::erf(z / 2.0f64.sqrt()))
}

/// Everything `fair_up_raw` computes on the way to `p`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FairDetail {
    /// P(settle >= ptb).
    pub p: f64,
    /// Expected settlement average.
    pub mean: f64,
    /// Variance of the settlement average.
    pub var: f64,
    /// Per-second increment variance from the trailing window.
    pub sig2: f64,
    /// Samples of the average already observed at `d`.
    pub known: usize,
    /// Samples still in the future.
    pub unknown: i64,
}

/// Port of `fair_up_raw(raw, t0, ptb, d, end, lag, vol_window)`. `raw[i]` is raw BRTI at
/// unix second `t0 + i` (`NaN` = missing). Returns `None` exactly where Python returns `None`.
///
/// One deliberate difference: if the averaging window starts before `t0` (possible only for a
/// decision at least 240 s after the window closes), numpy would silently wrap the negative index
/// to the end of the array; here that is `None`.
pub fn fair_up_raw(raw: &[f64], t0: i64, ptb: f64, d: i64, end: i64, lag: i64, vol_window: usize) -> Option<f64> {
    fair_up_raw_detail(raw, t0, ptb, d, end, lag, vol_window).map(|x| x.p)
}

/// As [`fair_up_raw`], with the intermediate quantities.
pub fn fair_up_raw_detail(raw: &[f64], t0: i64, ptb: f64, d: i64, end: i64, lag: i64, vol_window: usize) -> Option<FairDetail> {
    let lo_s = end - AVG_SECONDS - lag;
    let hi_s = end - 1 - lag;
    let i_d = d - t0;
    let vw = vol_window as i64;
    if i_d - vw < 0 || i_d >= raw.len() as i64 {
        return None;
    }
    let id = i_d as usize;
    let last = raw[id];
    if last.is_nan() {
        return None;
    }
    let lo_h = id - vol_window;
    let hist = &raw[lo_h..=id];
    if hist.iter().any(|x| x.is_nan()) {
        return None;
    }
    // np.mean(np.diff(hist) ** 2): numpy squares exactly (x * x) and sums pairwise.
    let n_inc = hist.len() - 1;
    let sq = |i: usize| {
        let x = hist[i + 1] - hist[i];
        x * x
    };
    let sig2 = np_pairwise_sum(&sq, 0, n_inc) / n_inc as f64;

    // sum(known): numpy scalars, so a plain left-to-right sum.
    let mut ksum = 0.0f64;
    let mut known = 0usize;
    let top = d.min(hi_s);
    let mut s = lo_s;
    while s <= top {
        let i = s - t0;
        if i < 0 {
            return None; // numpy would wrap; see doc comment
        }
        let x = raw[i as usize];
        if x.is_nan() {
            return None;
        }
        ksum += x;
        known += 1;
        s += 1;
    }
    let m = 0i64.max(hi_s - d.max(lo_s - 1));
    let mean = (ksum + m as f64 * last) / 60.0;
    let mut var = 0.0f64;
    let mut k = d + 1;
    while k <= hi_s {
        let w = (hi_s - k.max(lo_s) + 1) as f64 / 60.0;
        var += w * w;
        k += 1;
    }
    var *= sig2;
    let diff = mean - ptb;
    let p = if var <= 0.0 {
        if diff >= 0.0 {
            1.0
        } else {
            0.0
        }
    } else {
        phi(diff / var.sqrt())
    };
    Some(FairDetail { p, mean, var, sig2, known, unknown: m })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn walk(n: usize) -> Vec<f64> {
        let mut x = 60000.0;
        let mut seed = 7u64;
        (0..n)
            .map(|_| {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                x += ((seed >> 11) as f64 / (1u64 << 53) as f64 - 0.5) * 4.0;
                x
            })
            .collect()
    }

    #[test]
    fn none_paths() {
        let r = walk(1000);
        assert!(fair_up_raw(&r, 0, 60000.0, 299, 600, 0, 300).is_none()); // not enough history
        assert!(fair_up_raw(&r, 0, 60000.0, 1000, 1200, 0, 300).is_none()); // d beyond data
        let mut h = r.clone();
        h[400] = f64::NAN;
        assert!(fair_up_raw(&h, 0, 60000.0, 500, 600, 0, 300).is_none()); // gap in vol history
        let mut k = r.clone();
        k[545] = f64::NAN;
        assert!(fair_up_raw(&k, 0, 60000.0, 600, 600, 0, 30).is_none()); // gap in the accrued average
    }

    #[test]
    fn sign_rule_after_close_and_no_lookahead() {
        let r = walk(1000);
        let mean: f64 = r[540..600].iter().sum::<f64>() / 60.0;
        assert_eq!(fair_up_raw(&r, 0, mean - 1.0, 700, 600, 0, 300), Some(1.0));
        assert_eq!(fair_up_raw(&r, 0, mean + 1.0, 700, 600, 0, 300), Some(0.0));
        let p = fair_up_raw(&r, 0, 60000.0, 570, 600, 0, 300);
        let mut g = r.clone();
        for x in g.iter_mut().skip(571) {
            *x = f64::NAN;
        }
        assert_eq!(fair_up_raw(&g, 0, 60000.0, 570, 600, 0, 300), p);
    }
}
