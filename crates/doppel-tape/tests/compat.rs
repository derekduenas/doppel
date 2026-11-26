//! Cross-language compatibility: the Rust writer produces the committed fixture segment that
//! `tests/test_rust_tape_compat.py` reads with the Python reader.
//!
//! Regenerate (on a host with the toolchain):
//!   DOPPEL_WRITE_FIXTURE=<dir> cargo test --release --test compat
//! then copy `<dir>/rust_s/` to `tests/fixtures_rust_tape/rust_s/` in the repo.

use std::fs;
use std::path::{Path, PathBuf};

use doppel_tape::segment::{read_segment, segment_path, SegmentWriter};
use serde_json::{json, Value};

const T0: i64 = 1_795_622_400_000_000_000; // 2026-11-25 16:00:00Z
const TM0: i64 = 123_456_789;
const STREAM: &str = "rust_s";

/// (raw, meta) of every fixture record, in order. Mirrored in the pytest.
fn records() -> Vec<(String, Option<Value>)> {
    vec![
        (String::new(), Some(json!({"ctl": "connect", "url": "wss://example.test/ws"}))),
        (r#"{"a":1,"b":"x"}"#.to_string(), None),
        ("q\"uote\\back\nnl\ttab\r\u{1}\u{1f}\u{7f} é 漢 😀 \u{2028} </script>".to_string(), None),
        (
            "{\"data\":{\"payload\":[]}}".to_string(),
            Some(
                json!({"key": "KXBTC15M-26NOV251615-15", "status": 200, "sent_tw": T0, "sent_tm": TM0, "new": [[1795622400000i64, "97000.5"]], "f": 10.5, "neg": -3, "nested": {"z": [true, false, null]}}),
            ),
        ),
        (String::new(), Some(json!({"ctl": "backpressure_drop", "count": 3}))),
        ("empty meta is omitted".to_string(), Some(json!({}))),
        (String::new(), Some(json!({"ctl": "seq_gap", "sid": 1, "channel": "orderbook_delta", "expected": 5, "got": 7}))),
    ]
}

fn write_fixture(root: &Path) {
    let mut w = SegmentWriter::new(root, STREAM, 6).unwrap();
    for (i, (raw, meta)) in records().iter().enumerate() {
        w.append(raw, T0 + i as i64, TM0 + i as i64, meta.as_ref()).unwrap();
        if i == 3 {
            w.flush().unwrap(); // at least three frames: first append, this flush, close
        }
    }
    w.close().unwrap();
}

fn decompressed(p: &Path) -> Vec<u8> {
    zstd::stream::decode_all(fs::File::open(p).unwrap()).unwrap()
}

fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures_rust_tape")
}

#[test]
fn fixture_roundtrip_and_matches_committed() {
    let d = tempfile::tempdir().unwrap();
    write_fixture(d.path());
    let seg = segment_path(d.path(), STREAM, T0);
    let recs = read_segment(&seg, true).unwrap();
    assert_eq!(recs.len(), records().len());
    for (i, (r, (raw, meta))) in recs.iter().zip(records()).enumerate() {
        assert_eq!(r["seq"], i as u64 + 1);
        assert_eq!(r["stream"], STREAM);
        assert_eq!(r["tw"], T0 + i as i64);
        assert_eq!(r["tm"], TM0 + i as i64);
        assert_eq!(r["raw"], raw.as_str());
        assert_eq!(r["crc"], crc32fast::hash(raw.as_bytes()));
        let meta = meta.filter(|m| m.as_object().is_none_or(|o| !o.is_empty()));
        assert_eq!(r.get("meta").cloned(), meta);
    }
    if let Some(out) = std::env::var_os("DOPPEL_WRITE_FIXTURE") {
        let out = PathBuf::from(out);
        let _ = fs::remove_dir_all(out.join(STREAM));
        write_fixture(&out);
    }
    let committed = segment_path(&fixture_dir(), STREAM, T0);
    if committed.exists() {
        // zstd frame bytes may legitimately differ across zstd versions; the records may not
        assert_eq!(decompressed(&committed), decompressed(&seg));
        assert_eq!(read_segment(&committed, true).unwrap(), recs);
    }
}
