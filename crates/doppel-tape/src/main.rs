//! `doppel-taped run --data DIR [--only ...]`: the Rust tape recorder.

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let cfg = match doppel_tape::run::parse_args(&args) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    doppel_tape::install_crypto();
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().expect("tokio runtime");
    if let Err(e) = rt.block_on(doppel_tape::run::run(cfg)) {
        eprintln!("doppel-taped: {e}");
        std::process::exit(1);
    }
}
