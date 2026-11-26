//! Float primitives that reproduce the Python reference's arithmetic, so the ports can match it
//! to the last bit where the platform libm is shared (same machine), and to ~1 ulp elsewhere.

use std::hint::black_box;

/// CPython >= 3.12 builtin `sum()` over Python floats: Neumaier-compensated summation
/// (`bltinmodule.c`, "Improved Kahan-Babuska algorithm"). `brti.py` sums plain floats this way.
pub fn py_sum<I: IntoIterator<Item = f64>>(items: I) -> f64 {
    let mut acc = Neumaier::default();
    for x in items {
        acc.add(x);
    }
    acc.total()
}

/// The running state of [`py_sum`], for summing several sequences in one pass.
#[derive(Clone, Copy, Debug, Default)]
pub struct Neumaier {
    f: f64,
    c: f64,
}

impl Neumaier {
    #[inline]
    pub fn add(&mut self, x: f64) {
        let t = self.f + x;
        if self.f.abs() >= x.abs() {
            self.c += (self.f - t) + x;
        } else {
            self.c += (x - t) + self.f;
        }
        self.f = t;
    }

    pub fn total(self) -> f64 {
        if self.c != 0.0 && self.c.is_finite() {
            self.f + self.c
        } else {
            self.f
        }
    }
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
///
/// Fast path: libm `pow(a, 2)` is within 0.515 ulp of the true square (measured on 5e7 random
/// inputs on macOS; glibc documents ~0.52), so it can only differ from the correctly rounded
/// `a * a` when `a^2` lies near a rounding midpoint. The exact rounding error of `a * a`
/// (Dekker's two-product, no FMA needed) tells how near: below 0.375 of the gap to the
/// neighbouring double, `a * a` is returned (about 75% of inputs); otherwise libm decides.
#[inline]
pub fn py_pow2(x: f64) -> f64 {
    let a = x.abs();
    if a > 1e-130 && a < 1e130 {
        let t = 134_217_729.0 * a; // Veltkamp split, 2^27 + 1
        let hi = t - (t - a);
        let lo = a - hi;
        let p = a * a;
        let e = ((hi * hi - p) + 2.0 * hi * lo) + lo * lo; // a^2 = p + e exactly
        let bits = p.to_bits();
        let ulp = f64::from_bits(bits & 0x7ff0_0000_0000_0000) * f64::EPSILON;
        let gap = if e < 0.0 && bits & 0x000f_ffff_ffff_ffff == 0 { ulp / 2.0 } else { ulp };
        if e.abs() <= 0.375 * gap {
            return p;
        }
    }
    a.powf(black_box(2.0))
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
    fn pow2_fast_path_equals_libm_pow() {
        let mut s = 0x9e37_79b9_7f4a_7c15u64;
        let mut rnd = || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            s
        };
        let mut fast = 0;
        for i in 0..2_000_000u64 {
            let m = (rnd() >> 12) | 0x3ff0_0000_0000_0000;
            let e = (rnd() % 121) as i32 - 60;
            let x = f64::from_bits(m) * 2f64.powi(e) * if i % 2 == 0 { 1.0 } else { -1.0 };
            let want = x.abs().powf(black_box(2.0));
            assert_eq!(py_pow2(x).to_bits(), want.to_bits(), "x = {x:e}");
            fast += usize::from(x * x == want);
        }
        assert!(fast > 1_990_000);
        for x in [0.0, -0.0, 1e-300, 1e300, f64::INFINITY, f64::NAN, 3.0, 0.1, 1.0 + f64::EPSILON] {
            let want = x.abs().powf(black_box(2.0));
            assert!(py_pow2(x).to_bits() == want.to_bits() || (want.is_nan() && py_pow2(x).is_nan()));
        }
    }

    #[test]
    fn pairwise_small_and_large() {
        let v: Vec<f64> = (0..1000).map(|i| (i as f64) * 0.1).collect();
        let s = np_pairwise_sum(&|i| v[i], 0, v.len());
        assert!((s - 49950.0).abs() < 1e-9);
        assert_eq!(np_pairwise_sum(&|i| v[i], 0, 3), 0.0 + 0.1 + 0.2);
    }
}
