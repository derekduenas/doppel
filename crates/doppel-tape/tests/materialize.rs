#![cfg(feature = "materialize")]

use std::fs::{self, File};
use std::path::Path;
use std::process::Command;

use arrow_array::{Array, FixedSizeListArray, Float64Array, Int32Array, Int64Array, ListArray, RecordBatch, StringArray, StructArray};
use doppel_tape::materialize::{materialize, replay_path};
use doppel_tape::segment::{encode_record, segment_path, SegmentWriter};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::basic::Compression;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const T: i64 = 1_700_000_000_000_000_000;
const STREAM: &str = "clob_btc_5m";

fn snapshot(market: &str, asset: &str) -> Value {
    let bids =
        [0.49, 0.48, 0.47, 0.46, 0.45, 0.44].into_iter().map(|price| json!({"price": price.to_string(), "size": "5"})).collect::<Vec<_>>();
    json!({
        "event_type": "book", "asset_id": asset, "market": market, "timestamp": "1700000000000",
        "bids": bids,
        "asks": [{"price":"0.55","size":"6"}],
    })
}

fn change(px: &str, size: &str) -> Value {
    json!({"event_type": "price_change", "market": "a", "timestamp": 1700000000001_i64,
           "price_changes": [{"asset_id":"a-yes","price":px,"size":size,"side":"BUY"}]})
}

fn trade(size: &str, hash: &str) -> Value {
    json!({"event_type":"last_trade_price", "asset_id":"a-yes", "market":"a",
           "timestamp":"1700000000002","price":"0.55","size":size,"side":"BUY","transaction_hash":hash})
}

fn read(path: &Path) -> RecordBatch {
    let builder = ParquetRecordBatchReaderBuilder::try_new(File::open(path).unwrap()).unwrap();
    assert!(matches!(builder.metadata().row_group(0).column(0).compression(), Compression::ZSTD(_)));
    let mut reader = builder.build().unwrap();
    let batch = reader.next().unwrap().unwrap();
    assert!(reader.next().is_none());
    batch
}

fn col<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> &'a T {
    batch.column_by_name(name).unwrap().as_any().downcast_ref::<T>().unwrap()
}

#[test]
fn command_keeps_deep_venue_events_and_projects_only_top_changes_or_trades() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let mut writer = SegmentWriter::new(root, STREAM, 1).unwrap();
    let fallback = json!([{"event_type": "tick_size_change"}, trade("11", "second")]).to_string();
    let frames = [
        snapshot("z", "z-yes").to_string(),
        snapshot("a", "a-yes").to_string(),
        change("0.44", "99").to_string(),
        json!([change("0.49", "9"), trade("0", "marker")]).to_string(),
        fallback.clone(),
        String::new(),
        change("0.49", "99").to_string(),
        snapshot("a", "a-yes").to_string(),
        "PONG".to_string(),
    ];
    let meta = json!({"ctl":"disconnect"});
    for (i, raw) in frames.iter().enumerate() {
        writer.append(raw, T + i as i64 * 1_000_000, 0, (i == 5).then_some(&meta)).unwrap();
    }
    writer.close().unwrap();
    let input = segment_path(root, STREAM, T);
    let output = root.join("cache").join("test.parquet");
    let command = Command::new(env!("CARGO_BIN_EXE_doppel-tape")).args(["materialize"]).arg(&input).arg(&output).output().unwrap();
    assert!(command.status.success(), "{}", String::from_utf8_lossy(&command.stderr));

    let projection = read(&output);
    assert_eq!(projection.num_rows(), 6);
    assert_eq!(
        col::<StringArray>(&projection, "market_id").iter().map(Option::unwrap).collect::<Vec<_>>(),
        vec!["a", "a", "a", "a", "a", "z"]
    );
    assert_eq!(col::<Int64Array>(&projection, "record_no").values().as_ref(), [1, 3, 3, 4, 7, 0],);
    let sizes = col::<FixedSizeListArray>(&projection, "bid_sz").value(1);
    assert_eq!(sizes.as_any().downcast_ref::<Float64Array>().unwrap().values().as_ref(), [9.0, 5.0, 5.0, 5.0, 5.0]);
    assert_eq!(col::<Float64Array>(&projection, "trade_sz").value(2), 0.0);
    assert_eq!(col::<Float64Array>(&projection, "trade_sz").value(3), 11.0);

    let sidecar = replay_path(&output);
    let reader = ParquetRecordBatchReaderBuilder::try_new(File::open(&sidecar).unwrap()).unwrap();
    let metadata = reader.metadata().file_metadata().key_value_metadata().unwrap();
    let value =
        |name: &str| metadata.iter().find(|entry| entry.key == format!("doppel.book-replay.{name}")).unwrap().value.as_deref().unwrap();
    assert_eq!(value("schema"), "1");
    assert_eq!(value("source-stream"), STREAM);
    assert_eq!(value("source-records"), "9");
    assert_eq!(value("source-sha256"), hex::encode(Sha256::digest(fs::read(&input).unwrap())));

    let lossless = read(&sidecar);
    assert_eq!(lossless.num_rows(), 10);
    assert_eq!(col::<Int64Array>(&lossless, "record_no").values().as_ref(), [0, 1, 2, 3, 3, 4, 5, 6, 7, 8]);
    assert_eq!(col::<Int32Array>(&lossless, "message_no").value(4), 1);
    assert_eq!(col::<Int64Array>(&lossless, "venue_ms").value(2), 1_700_000_000_001);
    assert_eq!(col::<ListArray>(&lossless, "bids").value(0).len(), 6);
    assert_eq!(col::<StringArray>(&lossless, "ctl").value(6), "disconnect");
    assert_eq!(col::<StringArray>(&lossless, "tx").value(4), "marker");
    assert_eq!(col::<StringArray>(&lossless, "fallback_raw").value(5), fallback);
    assert_eq!(col::<StringArray>(&lossless, "event_type").value(9), "skip");

    let old = fs::read(&output).unwrap();
    assert!(materialize(&input, &output).is_err());
    assert_eq!(fs::read(&output).unwrap(), old);
}

#[test]
fn invalid_tail_scope_and_crc_refuse_without_publishing() {
    let temp = tempfile::tempdir().unwrap();
    let input = temp.path().join("input.dtape.zst");
    let output = temp.path().join("output.parquet");
    let first = encode_record(1, STREAM, T, 0, "PONG", None);
    let mut bad_crc: Value = serde_json::from_slice(&first).unwrap();
    bad_crc["crc"] = json!(0);
    let wrong_hour = encode_record(2, STREAM, T + 3_600_000_000_000, 0, "PONG", None);
    let wrong_stream = encode_record(2, "clob_eth_5m", T, 0, "PONG", None);
    let raw_cases = [
        [first.as_slice(), b"{\n"].concat(),
        [first.as_slice(), b"{"].concat(),
        [first.as_slice(), &serde_json::to_vec(&bad_crc).unwrap(), b"\n"].concat(),
        [first.as_slice(), wrong_hour.as_slice()].concat(),
        [first.as_slice(), wrong_stream.as_slice()].concat(),
    ];
    for raw in raw_cases {
        fs::write(&input, zstd::stream::encode_all(raw.as_slice(), 1).unwrap()).unwrap();
        assert!(materialize(&input, &output).is_err());
        assert!(!output.exists() && !replay_path(&output).exists());
    }
    let mut truncated = zstd::stream::encode_all(first.as_slice(), 1).unwrap();
    truncated.pop();
    fs::write(&input, truncated).unwrap();
    assert!(materialize(&input, &output).is_err());
    assert!(!output.exists() && !replay_path(&output).exists());
}

#[test]
fn patches_link_strict_snapshots_and_keep_original_levels() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    let mut writer = SegmentWriter::new(root, STREAM, 1).unwrap();
    let first = snapshot("a", "a-yes");
    let second = snapshot("b", "b-yes");
    let mut changed = first.clone();
    changed["bids"][0]["size"] = json!("9");
    changed["bids"][5]["size"] = json!("-0");
    changed["asks"][0]["size"] = json!("10");
    let mut off_grid = first.clone();
    off_grid["bids"][0]["price"] = json!("0.490009");
    let mut duplicate = first.clone();
    duplicate["bids"][1]["price"] = json!("0.49");
    let mut reversed = first.clone();
    reversed["timestamp"] = json!(1_699_999_999_999_i64);
    let frames = [
        first.to_string(),
        second.to_string(),
        json!([trade("1", "marker"), changed, changed]).to_string(),
        String::new(),
        first.to_string(),
        change("0.49", "17").to_string(),
        first.to_string(),
        off_grid.to_string(),
        first.to_string(),
        duplicate.to_string(),
        first.to_string(),
        reversed.to_string(),
        first.to_string(),
        json!({"event_type":"tick_size_change"}).to_string(),
        first.to_string(),
    ];
    let meta = json!({"ctl":"subscribe_add"});
    for (i, raw) in frames.iter().enumerate() {
        writer.append(raw, T + i as i64 * 1_000_000, 0, (i == 3).then_some(&meta)).unwrap();
    }
    writer.close().unwrap();
    let output = root.join("cache.parquet");
    materialize(&segment_path(root, STREAM, T), &output).unwrap();
    let lossless = read(&replay_path(&output));
    assert_eq!(lossless.num_rows(), 17);
    let recorded = col::<Int64Array>(&lossless, "snapshot_prev_record_no");
    let children = col::<Int32Array>(&lossless, "snapshot_prev_message_no");
    let deltas = col::<ListArray>(&lossless, "snapshot_delta");
    for row in [0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16] {
        assert!(recorded.is_null(row) && children.is_null(row) && deltas.is_null(row), "row {row}");
    }
    assert_eq!((recorded.value(3), children.value(3)), (0, 0));
    assert_eq!((recorded.value(4), children.value(4)), (2, 1));
    assert_eq!((recorded.value(14), children.value(14)), (11, 0));
    let delta = deltas.value(3);
    let delta = delta.as_any().downcast_ref::<StructArray>().unwrap();
    let numbers = |i| delta.column(i).as_any().downcast_ref::<Float64Array>().unwrap().values().as_ref();
    assert_eq!(numbers(0), [0.44, 0.49, 0.55]);
    assert_eq!(numbers(1), [0.0, 9.0, 10.0]);
    assert!(numbers(1)[0].is_sign_positive());
    let sides = delta.column(2).as_any().downcast_ref::<StringArray>().unwrap();
    assert_eq!(sides.iter().map(Option::unwrap).collect::<Vec<_>>(), ["BUY", "BUY", "SELL"]);
    assert!(!deltas.is_null(4) && deltas.value(4).is_empty());
    assert_eq!(col::<ListArray>(&lossless, "bids").value(3).len(), 6);
    assert_eq!(col::<ListArray>(&lossless, "asks").value(3).len(), 1);
    assert!(!deltas.is_null(14) && deltas.value(14).is_empty());
}
