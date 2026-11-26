//! `doppel-replica-replay --data TAPE_ROOT --from TS --to TS --out FILE.jsonl [--start TS]`: the
//! BRTI replica over the recorded `spot_l2_<venue>` streams, one JSON row per second (see
//! `doppel_tape::replay`). Timings and per-venue counts go to stderr as one JSON line.

use std::io::{BufWriter, Write};

use doppel_tape::replay::{parse_args, replay, tape_sources};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let a = match parse_args(&args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    let sources = tape_sources(&a.data, a.start_ns, a.to_s);
    if sources.is_empty() {
        eprintln!("doppel-replica-replay: no spot_l2_* streams under {}", a.data.display());
        std::process::exit(1);
    }
    let run = || -> std::io::Result<_> {
        let mut out = BufWriter::new(std::fs::File::create(&a.out)?);
        let sum = replay(sources, a.from_s, a.to_s, &mut |r| writeln!(out, "{}", r.to_json()))?;
        out.flush()?;
        Ok(sum)
    };
    match run() {
        Ok(sum) => eprintln!("{}", sum.to_json()),
        Err(e) => {
            eprintln!("doppel-replica-replay: {e}");
            std::process::exit(1);
        }
    }
}
