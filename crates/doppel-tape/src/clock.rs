//! Receive clocks and UTC calendar helpers.
//!
//! `wall_ns` is `CLOCK_REALTIME` and `mono_ns` is `CLOCK_MONOTONIC`, the same clocks Python's
//! `time.time_ns()` and `time.monotonic_ns()` read on Linux, so `tm` values written by the Rust and
//! the Python recorder on one host are directly comparable.

pub fn wall_ns() -> i64 {
    clock(libc::CLOCK_REALTIME)
}

pub fn mono_ns() -> i64 {
    clock(libc::CLOCK_MONOTONIC)
}

fn clock(id: libc::clockid_t) -> i64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: `ts` is a valid, writable timespec and both clock ids exist on every supported OS.
    unsafe {
        libc::clock_gettime(id, &mut ts);
    }
    ts.tv_sec * 1_000_000_000 + ts.tv_nsec
}

/// Wall clock in seconds (float), for window arithmetic.
pub fn wall_s() -> f64 {
    wall_ns() as f64 / 1e9
}

/// UTC calendar fields of a unix time in seconds: (year, month, day, hour, minute).
pub fn utc_parts(secs: i64) -> (i64, u32, u32, u32, u32) {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = yoe + era * 400 + i64::from(m <= 2);
    (y, m, d, (rem / 3600) as u32, ((rem % 3600) / 60) as u32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_parts_known_dates() {
        assert_eq!(utc_parts(0), (1970, 1, 1, 0, 0));
        assert_eq!(utc_parts(1_795_622_400), (2026, 11, 25, 16, 0));
        assert_eq!(utc_parts(951_782_400 + 3_660), (2000, 2, 29, 1, 1));
        assert_eq!(utc_parts(1_798_761_599), (2026, 12, 31, 23, 59));
    }

    #[test]
    fn clocks_advance() {
        let (a, b) = (mono_ns(), mono_ns());
        assert!(b >= a);
        assert!(wall_ns() > 1_700_000_000_000_000_000);
    }
}
