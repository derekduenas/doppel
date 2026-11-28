//! Offline book materialization for sealed Intl CLOB hours.

use std::path::Path;

fn main() {
    let mut args = std::env::args_os().skip(1);
    let (Some(command), Some(input), Some(output), None) = (args.next(), args.next(), args.next(), args.next()) else {
        eprintln!("usage: doppel-tape materialize INPUT.dtape.zst OUTPUT.parquet");
        std::process::exit(2);
    };
    if command != "materialize" {
        eprintln!("usage: doppel-tape materialize INPUT.dtape.zst OUTPUT.parquet");
        std::process::exit(2);
    }
    if let Err(error) = doppel_tape::materialize::materialize(Path::new(&input), Path::new(&output)) {
        eprintln!("doppel-tape materialize: {error}");
        std::process::exit(1);
    }
}
