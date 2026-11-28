//! DOPPEL tape recorder (Rust port of `src/doppel/tape`). Segments are byte-compatible with the
//! Python reader (`doppel.tape.segment.read_segment`).

pub mod clock;
pub mod http;
pub mod kalshi;
pub mod l2;
pub mod lead;
pub mod perps;
pub mod pmus;
pub mod recorder;
pub mod replay;
pub mod run;
pub mod segment;
pub mod universe;
pub mod venues;
pub mod ws;

/// Install the ring crypto provider for rustls (idempotent).
pub fn install_crypto() {
    let _ = rustls::crypto::ring::default_provider().install_default();
}
