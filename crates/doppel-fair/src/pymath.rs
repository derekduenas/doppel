//! Float primitives that reproduce the Python reference's arithmetic, so the ports can match it
//! to the last bit where the platform libm is shared (same machine), and to ~1 ulp elsewhere.

use std::hint::black_box;

/// CPython >= 3.12 builtin `sum()` over Python floats: Neumaier-compensated summation
/// (`bltinmodule.c`, "Improved Kahan-Babuska algorithm"). `brti.py` sums plain floats this way.
pub fn py_sum<I: IntoIterator<Item = f64>>(items: I) -> f64 {
    let mut f = 0.0f64;
    let mut c = 0.0f64;
    for x in items {
        let t = f + x;
        if f.abs() >= x.abs() {
            c += (f - t) + x;
        } else {
            c += (x - t) + f;
        }
        f = t;
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    f
}

/// numpy float64 `add.reduce` (pairwise summation, 8-way unrolled, block 128), evaluated over
/// `get(off..off+n)` without allocating. `np.mean(x)` is this divided by `n`.
pub fn np_pairwise_sum<F: Fn(usize) -> f64>(get: &F, off: usize, n: usize) -> f64 {
    if n < 8 {
        let mut res = 0.0;
        for i in 0..n {
            res += get(off + i);
        }
        res
    } else if n <= 128 {
        let mut r = [0.0f64; 8];
        for (j, rj) in r.iter_mut().enumerate() {
            *rj = get(off + j);
        }
        let mut i = 8;
        while i < n - (n % 8) {
            for (j, rj) in r.iter_mut().enumerate() {
                *rj += get(off + i + j);
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += get(off + i);
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        np_pairwise_sum(get, off, n2) + np_pairwise_sum(get, off + n2, n - n2)
    }
}

/// Python `x ** 2` for a float: CPython calls libm `pow(|x|, 2.0)`, which on some libms (macOS)
/// is not the correctly rounded `x * x`. `black_box` keeps LLVM from folding it into a multiply.
#[inline]
pub fn py_pow2(x: f64) -> f64 {
    x.abs().powf(black_box(2.0))
}

/// Python `float ** y` for a positive base (libm `pow`), never constant-folded.
#[inline]
pub fn py_pow(x: f64, y: f64) -> f64 {
    x.powf(black_box(y))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn neumaier_recovers_cancellation() {
        assert_eq!(py_sum([1e16, 1.0, -1e16]), 1.0);
        assert_eq!(py_sum(std::iter::empty()), 0.0);
        assert_eq!(py_sum([0.1, 0.2, 0.3]), 0.6);
    }

    #[test]
    fn pairwise_small_and_large() {
        let v: Vec<f64> = (0..1000).map(|i| (i as f64) * 0.1).collect();
        let s = np_pairwise_sum(&|i| v[i], 0, v.len());
        assert!((s - 49950.0).abs() < 1e-9);
        assert_eq!(np_pairwise_sum(&|i| v[i], 0, 3), 0.0 + 0.1 + 0.2);
    }
}
