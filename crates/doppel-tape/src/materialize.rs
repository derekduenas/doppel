//! A closed Intl CLOB capture hour, exported as two Parquet views.
//!
//! The compact projection gives top-five changes and trade prints sorted by condition ID and
//! receive time. Asset IDs distinguish the outcomes of a condition. The replay sidecar stays in
//! capture order and keeps full snapshots, deep updates, control rows and empty observations.
//! Times, source ordinals, and embedded-message order allow independent slices to see the same
//! state transitions as the raw reader. A rare unsupported wire frame keeps its exact raw text.
//! Repeated, strictly valid snapshots also carry a patch against their named predecessor, so
//! continuous playback can skip rebuilding the full snapshot. A slice can always use the original.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, File};
use std::io::{self, Seek};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::builder::{Float64Builder, ListBuilder, StringBuilder, StructBuilder};
use arrow_array::types::Float64Type;
use arrow_array::{ArrayRef, FixedSizeListArray, Float64Array, Int32Array, Int64Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Fields, Schema};
use doppel_fair::book::{L2Book, Side};
use doppel_fair::brti::Level;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tempfile::NamedTempFile;

use crate::clock::wall_ns;
use crate::segment::sealed_segment_records;

const HOUR_NS: i64 = 3_600_000_000_000;
const TOP: usize = 5;
const BATCH: usize = 8_192;
const ROW_GROUP: usize = 100_000;
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

#[derive(Clone, Copy)]
struct Capture {
    ts_ns: i64,
    seq: i64,
    record_no: i64,
}

struct Change {
    asset: Option<String>,
    price: f64,
    size: f64,
    side: String,
}

enum Event {
    Book { asset: String, bids: Vec<Level>, asks: Vec<Level> },
    Change(Vec<Change>),
    Trade { asset: String, price: f64, size: f64, side: String, tx: Option<String> },
    Control(String),
    Skip,
    Fallback(String),
}

struct Frame {
    capture: Capture,
    message_no: i32,
    venue_ms: Option<i64>,
    market: Option<String>,
    event: Event,
    snapshot_patch: Option<SnapshotPatch>,
}

impl Frame {
    fn singleton(capture: Capture, event: Event) -> Self {
        Self { capture, message_no: 0, venue_ms: None, market: None, event, snapshot_patch: None }
    }

    fn kind(&self) -> &'static str {
        match self.event {
            Event::Book { .. } => "book",
            Event::Change(_) => "price_change",
            Event::Trade { .. } => "last_trade_price",
            Event::Control(_) => "control",
            Event::Skip => "skip",
            Event::Fallback(_) => "fallback",
        }
    }

    fn asset(&self) -> Option<&str> {
        match &self.event {
            Event::Book { asset, .. } | Event::Trade { asset, .. } => Some(asset),
            _ => None,
        }
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn optional_text(value: Option<&Value>) -> Option<Option<String>> {
    match value {
        None | Some(Value::Null) => Some(None),
        Some(Value::String(s)) => Some(Some(s.clone())),
        _ => None,
    }
}

fn finite(value: &Value) -> Option<f64> {
    // Live prices and sizes are strings. JSON integers are exact in the venue's supported
    // ranges. Float-valued JSON fields go through the raw fallback to preserve Python rounding.
    let value = match value {
        Value::String(s) => s.parse::<f64>().ok()?,
        Value::Number(n) if n.is_i64() || n.is_u64() => n.as_f64()?,
        _ => return None,
    };
    value.is_finite().then_some(value)
}

fn venue_time(value: Option<&Value>) -> Option<Option<i64>> {
    match value {
        None | Some(Value::Null) => Some(None),
        Some(Value::Number(n)) => n.as_i64().map(Some),
        Some(Value::String(s)) if !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit()) => s.parse().ok().map(Some),
        _ => None,
    }
}

fn levels(rows: Option<&Value>) -> Option<Vec<Level>> {
    rows?.as_array()?.iter().map(|row| Some((finite(row.get("price")?)?, finite(row.get("size")?)?))).collect()
}

fn normalize(value: &Value, capture: Capture, message_no: i32) -> Option<Frame> {
    let market = optional_text(value.get("market"))?;
    let venue_ms = venue_time(value.get("timestamp"))?;
    let event = match value.get("event_type")?.as_str()? {
        "book" => Event::Book {
            asset: value.get("asset_id")?.as_str()?.to_string(),
            bids: levels(value.get("bids"))?,
            asks: levels(value.get("asks"))?,
        },
        "price_change" => Event::Change(
            value
                .get("price_changes")?
                .as_array()?
                .iter()
                .map(|row| {
                    Some(Change {
                        asset: optional_text(row.get("asset_id"))?,
                        price: finite(row.get("price")?)?,
                        size: finite(row.get("size")?)?,
                        side: row.get("side")?.as_str()?.to_string(),
                    })
                })
                .collect::<Option<Vec<_>>>()?,
        ),
        "last_trade_price" => Event::Trade {
            asset: value.get("asset_id")?.as_str()?.to_string(),
            price: finite(value.get("price")?)?,
            size: finite(value.get("size")?)?,
            side: value.get("side")?.as_str()?.to_string(),
            tx: optional_text(value.get("transaction_hash"))?,
        },
        _ => return None,
    };
    Some(Frame { capture, message_no, venue_ms, market, event, snapshot_patch: None })
}

#[derive(Clone, Default, PartialEq)]
struct Top {
    bids: [Option<Level>; TOP],
    asks: [Option<Level>; TOP],
}

impl Top {
    fn snapshot(book: &L2Book) -> Self {
        let native = book.snapshot_depth(TOP);
        let mut top = Self::default();
        for (slot, level) in top.bids.iter_mut().zip(native.bids) {
            *slot = Some(level);
        }
        for (slot, level) in top.asks.iter_mut().zip(native.asks) {
            *slot = Some(level);
        }
        top
    }
}

#[derive(Default)]
struct AssetBook {
    market: String,
    book: L2Book,
    top: Option<Top>,
}

struct Projected {
    capture: Capture,
    message_no: i32,
    venue_ms: Option<i64>,
    market: String,
    asset: String,
    top: Option<Top>,
    trade: Option<(f64, f64, String)>,
}

#[derive(Default)]
struct Projection {
    books: HashMap<String, AssetBook>,
    rows: Vec<Projected>,
}

fn tick(price: f64) -> f64 {
    // Formatting rounds the original binary float directly, avoiding x*10000 double rounding.
    // Ordinary CLOB prices are exact tenths of a cent, so bypass formatting on that path.
    let scale = price * 10_000.0;
    if scale.is_finite() && (scale - scale.round()).abs() < 1e-8 {
        return scale.round() / 10_000.0;
    }
    format!("{price:.4}").parse().expect("a formatted finite price")
}

/// The key order matches `IntlBook`: price ascending, BUY before SELL at a shared price.
type LevelKey = (u16, bool);

struct SnapshotPatch {
    previous: (i64, i32),
    levels: Vec<(LevelKey, f64)>,
}

struct SnapshotAnchor {
    ordinal: (i64, i32),
    venue_ms: i64,
    levels: BTreeMap<LevelKey, f64>,
}

#[derive(Default)]
struct SnapshotPatches(HashMap<String, SnapshotAnchor>);

fn patchable_levels(bids: &[Level], asks: &[Level]) -> Option<BTreeMap<LevelKey, f64>> {
    let mut levels = BTreeMap::new();
    for (ask, side) in [(false, bids), (true, asks)] {
        for &(price, size) in side {
            // The typed decoder already rejected nonfinite fields. Signed zero cannot be
            // represented by the integer key without changing the serialized price.
            if price.is_sign_negative() || price > 1.0 || tick(price) != price || size < 0.0 {
                return None;
            }
            let key = ((price * 10_000.0).round() as u16, ask);
            // Python drops all zero-size snapshot entries and emits positive zero on removal.
            let size = if size == 0.0 { 0.0 } else { size };
            if levels.insert(key, size).is_some() {
                return None;
            }
        }
    }
    Some(levels)
}

impl SnapshotPatches {
    fn observe(&mut self, frame: &mut Frame) {
        match &frame.event {
            Event::Book { asset, bids, asks } => {
                let previous = self.0.remove(asset);
                let (Some(venue_ms), Some(levels)) =
                    (frame.venue_ms.filter(|&ts| ts > 0 && !asset.is_empty()), patchable_levels(bids, asks))
                else {
                    return;
                };
                let ordinal = (frame.capture.record_no, frame.message_no);
                if let Some(from) = previous.filter(|from| from.venue_ms <= venue_ms) {
                    let keys: BTreeSet<_> = from.levels.keys().chain(levels.keys()).copied().collect();
                    let delta = keys
                        .into_iter()
                        .filter_map(|key| {
                            let old = from.levels.get(&key).copied().unwrap_or_default();
                            let new = levels.get(&key).copied().unwrap_or_default();
                            (old != new).then_some((key, new))
                        })
                        .collect();
                    frame.snapshot_patch = Some(SnapshotPatch { previous: from.ordinal, levels: delta });
                }
                self.0.insert(asset.clone(), SnapshotAnchor { ordinal, venue_ms, levels });
            }
            Event::Change(changes) => {
                for change in changes {
                    if let Some(asset) = &change.asset {
                        self.0.remove(asset);
                    }
                }
            }
            Event::Control(_) | Event::Fallback(_) => self.0.clear(),
            Event::Trade { .. } | Event::Skip => {}
        }
    }
}

impl Projection {
    fn observe(&mut self, frame: &Frame) {
        match &frame.event {
            Event::Control(ctl) if !matches!(ctl.as_str(), "subscribe" | "subscribe_add" | "unsubscribe") => {
                self.books.clear();
            }
            Event::Book { asset, bids, asks } => {
                let book = self.books.entry(asset.clone()).or_default();
                book.market = frame.market.clone().unwrap_or_default();
                book.book.reset();
                for (side, levels) in [(Side::Bid, bids), (Side::Ask, asks)] {
                    for &(price, size) in levels {
                        book.book.put(side, tick(price), size);
                    }
                }
                self.book_row(frame, asset);
            }
            Event::Change(changes) => {
                let mut touched = BTreeSet::new();
                for change in changes {
                    let Some(asset) = change.asset.as_ref() else { continue };
                    let Some(book) = self.books.get_mut(asset) else { continue };
                    let side = match change.side.as_str() {
                        "BUY" => Side::Bid,
                        "SELL" => Side::Ask,
                        _ => continue,
                    };
                    book.book.put(side, tick(change.price), change.size);
                    touched.insert(asset);
                }
                for asset in touched {
                    self.book_row(frame, asset);
                }
            }
            Event::Trade { asset, price, size, side, .. } if !asset.is_empty() => {
                let known = self.books.get(asset);
                self.rows.push(Projected {
                    capture: frame.capture,
                    message_no: frame.message_no,
                    venue_ms: frame.venue_ms,
                    market: frame.market.clone().or_else(|| known.map(|book| book.market.clone())).unwrap_or_default(),
                    asset: asset.clone(),
                    top: known.and_then(|book| book.top.clone()),
                    trade: Some((tick(*price), *size, side.clone())),
                });
            }
            _ => {}
        }
    }

    fn book_row(&mut self, frame: &Frame, asset: &str) {
        let book = self.books.get_mut(asset).expect("projected asset is present");
        let top = Top::snapshot(&book.book);
        if book.top.as_ref() != Some(&top) {
            self.rows.push(Projected {
                capture: frame.capture,
                message_no: frame.message_no,
                venue_ms: frame.venue_ms,
                market: book.market.clone(),
                asset: asset.to_string(),
                top: Some(top.clone()),
                trade: None,
            });
            book.top = Some(top);
        }
    }
}

fn parse_frames(record: &Value, capture: Capture, projection: &mut Projection) -> io::Result<Vec<Frame>> {
    let raw = record.get("raw").and_then(Value::as_str).ok_or_else(|| invalid("raw record text is missing"))?;
    if let Some(ctl) = record.get("meta").and_then(|m| m.get("ctl")).filter(|x| !x.is_null()) {
        let ctl = ctl.as_str().ok_or_else(|| invalid("non-string control record"))?;
        let frame = Frame::singleton(capture, Event::Control(ctl.to_string()));
        projection.observe(&frame);
        return Ok(vec![frame]);
    }
    if matches!(raw, "" | "PING" | "PONG") {
        return Ok(vec![Frame::singleton(capture, Event::Skip)]);
    }
    if !raw.starts_with(['{', '[']) {
        return Ok(vec![Frame::singleton(capture, Event::Fallback(raw.to_string()))]);
    }
    let Ok(decoded) = serde_json::from_str::<Value>(raw) else {
        return Ok(vec![Frame::singleton(capture, Event::Fallback(raw.to_string()))]);
    };
    let decoded = match &decoded {
        Value::Array(values) => values.as_slice(),
        one => std::slice::from_ref(one),
    };
    if decoded.is_empty() {
        return Ok(vec![Frame::singleton(capture, Event::Skip)]);
    }
    let mut frames = Vec::with_capacity(decoded.len());
    for (message_no, message) in decoded.iter().enumerate() {
        let message_no = i32::try_from(message_no).map_err(|_| invalid("record contains too many messages"))?;
        if let Some(frame) = normalize(message, capture, message_no) {
            frames.push(frame);
        }
    }
    // Construct the original wire fallback before applying any known children to the display
    // projection. Strict/lenient replay semantics for the other children stay in Python.
    let fallback = (frames.len() != decoded.len()).then(|| Frame::singleton(capture, Event::Fallback(raw.to_string())));
    for frame in &frames {
        projection.observe(frame);
    }
    if let Some(frame) = fallback {
        return Ok(vec![frame]);
    }
    Ok(frames)
}

fn field(name: &str, ty: DataType) -> Field {
    Field::new(name, ty, true)
}

fn level_fields(changes: bool) -> Fields {
    let mut fields = vec![field("price", DataType::Float64), field("size", DataType::Float64)];
    if changes {
        fields.push(field("asset_id", DataType::Utf8));
        fields.push(field("side", DataType::Utf8));
    }
    fields.into()
}

fn list_field(changes: bool) -> Arc<Field> {
    Arc::new(field("item", DataType::Struct(level_fields(changes))))
}

fn patch_field() -> Arc<Field> {
    let fields = vec![field("price", DataType::Float64), field("size", DataType::Float64), field("side", DataType::Utf8)].into();
    Arc::new(field("item", DataType::Struct(fields)))
}

fn replay_schema(metadata: HashMap<String, String>) -> Arc<Schema> {
    Arc::new(Schema::new_with_metadata(
        vec![
            field("ts_ns", DataType::Int64),
            field("record_no", DataType::Int64),
            field("message_no", DataType::Int32),
            field("seq", DataType::Int64),
            field("event_type", DataType::Utf8),
            field("venue_ms", DataType::Int64),
            field("market_id", DataType::Utf8),
            field("asset_id", DataType::Utf8),
            field("bids", DataType::List(list_field(false))),
            field("asks", DataType::List(list_field(false))),
            field("changes", DataType::List(list_field(true))),
            field("trade_px", DataType::Float64),
            field("trade_sz", DataType::Float64),
            field("trade_side", DataType::Utf8),
            field("tx", DataType::Utf8),
            field("ctl", DataType::Utf8),
            field("fallback_raw", DataType::Utf8),
            field("snapshot_prev_record_no", DataType::Int64),
            field("snapshot_prev_message_no", DataType::Int32),
            field("snapshot_delta", DataType::List(patch_field())),
        ],
        metadata,
    ))
}

fn struct_list(changes: bool) -> ListBuilder<StructBuilder> {
    ListBuilder::new(StructBuilder::from_fields(level_fields(changes), BATCH)).with_field(list_field(changes))
}

fn level_value(values: &mut StructBuilder, price: f64, size: f64) {
    values.field_builder::<Float64Builder>(0).expect("price").append_value(price);
    values.field_builder::<Float64Builder>(1).expect("size").append_value(size);
}

fn snapshot_list<'a>(rows: impl Iterator<Item = Option<&'a [Level]>>) -> ArrayRef {
    let mut output = struct_list(false);
    for row in rows {
        if let Some(levels) = row {
            for &(price, size) in levels {
                let values = output.values();
                level_value(values, price, size);
                values.append(true);
            }
        }
        output.append(row.is_some());
    }
    Arc::new(output.finish())
}

fn changes_list(frames: &[Frame]) -> ArrayRef {
    let mut output = struct_list(true);
    for frame in frames {
        if let Event::Change(changes) = &frame.event {
            for change in changes {
                let values = output.values();
                level_value(values, change.price, change.size);
                values.field_builder::<StringBuilder>(2).expect("asset").append_option(change.asset.as_deref());
                values.field_builder::<StringBuilder>(3).expect("side").append_value(&change.side);
                values.append(true);
            }
            output.append(true);
        } else {
            output.append(false);
        }
    }
    Arc::new(output.finish())
}

fn snapshot_deltas(frames: &[Frame]) -> ArrayRef {
    let item = patch_field();
    let DataType::Struct(fields) = item.data_type() else { unreachable!("snapshot patch item is a struct") };
    let mut output = ListBuilder::new(StructBuilder::from_fields(fields.clone(), BATCH)).with_field(item.clone());
    for frame in frames {
        if let Some(patch) = &frame.snapshot_patch {
            for &((price, ask), size) in &patch.levels {
                let values = output.values();
                level_value(values, f64::from(price) / 10_000.0, size);
                values.field_builder::<StringBuilder>(2).expect("side").append_value(if ask { "SELL" } else { "BUY" });
                values.append(true);
            }
            output.append(true);
        } else {
            output.append(false);
        }
    }
    Arc::new(output.finish())
}

fn replay_batch(schema: Arc<Schema>, frames: &[Frame]) -> Result<RecordBatch> {
    let bids = snapshot_list(frames.iter().map(|frame| match &frame.event {
        Event::Book { bids, .. } => Some(bids.as_slice()),
        _ => None,
    }));
    let asks = snapshot_list(frames.iter().map(|frame| match &frame.event {
        Event::Book { asks, .. } => Some(asks.as_slice()),
        _ => None,
    }));
    let trade_float = |part: fn(f64, f64) -> f64| {
        Float64Array::from_iter(frames.iter().map(|f| match f.event {
            Event::Trade { price, size, .. } => Some(part(price, size)),
            _ => None,
        }))
    };
    let trade_side = StringArray::from_iter(frames.iter().map(|f| match &f.event {
        Event::Trade { side, .. } => Some(side.as_str()),
        _ => None,
    }));
    let tx = StringArray::from_iter(frames.iter().map(|f| match &f.event {
        Event::Trade { tx, .. } => tx.as_deref(),
        _ => None,
    }));
    let ctl = StringArray::from_iter(frames.iter().map(|f| match &f.event {
        Event::Control(ctl) => Some(ctl.as_str()),
        _ => None,
    }));
    let fallback = StringArray::from_iter(frames.iter().map(|f| match &f.event {
        Event::Fallback(raw) => Some(raw.as_str()),
        _ => None,
    }));
    Ok(RecordBatch::try_new(
        schema,
        vec![
            Arc::new(Int64Array::from_iter_values(frames.iter().map(|f| f.capture.ts_ns))),
            Arc::new(Int64Array::from_iter_values(frames.iter().map(|f| f.capture.record_no))),
            Arc::new(Int32Array::from_iter_values(frames.iter().map(|f| f.message_no))),
            Arc::new(Int64Array::from_iter_values(frames.iter().map(|f| f.capture.seq))),
            Arc::new(StringArray::from_iter_values(frames.iter().map(Frame::kind))),
            Arc::new(Int64Array::from_iter(frames.iter().map(|f| f.venue_ms))),
            Arc::new(StringArray::from_iter(frames.iter().map(|f| f.market.as_deref()))),
            Arc::new(StringArray::from_iter(frames.iter().map(Frame::asset))),
            bids,
            asks,
            changes_list(frames),
            Arc::new(trade_float(|price, _| price)),
            Arc::new(trade_float(|_, size| size)),
            Arc::new(trade_side),
            Arc::new(tx),
            Arc::new(ctl),
            Arc::new(fallback),
            Arc::new(Int64Array::from_iter(frames.iter().map(|f| f.snapshot_patch.as_ref().map(|p| p.previous.0)))),
            Arc::new(Int32Array::from_iter(frames.iter().map(|f| f.snapshot_patch.as_ref().map(|p| p.previous.1)))),
            snapshot_deltas(frames),
        ],
    )?)
}

fn compact_schema(metadata: HashMap<String, String>) -> Arc<Schema> {
    let top = || DataType::FixedSizeList(Arc::new(field("item", DataType::Float64)), TOP as i32);
    Arc::new(Schema::new_with_metadata(
        vec![
            field("ts_ns", DataType::Int64),
            field("record_no", DataType::Int64),
            field("message_no", DataType::Int32),
            field("venue_ms", DataType::Int64),
            field("market_id", DataType::Utf8),
            field("asset_id", DataType::Utf8),
            field("event_type", DataType::Utf8),
            field("bid_px", top()),
            field("bid_sz", top()),
            field("ask_px", top()),
            field("ask_sz", top()),
            field("trade_px", DataType::Float64),
            field("trade_sz", DataType::Float64),
            field("trade_side", DataType::Utf8),
        ],
        metadata,
    ))
}

fn compact_batch(schema: Arc<Schema>, rows: &[Projected]) -> Result<RecordBatch> {
    let level_column = |side: fn(&Top) -> &[Option<Level>; TOP], part: fn(Level) -> f64| {
        let levels = rows
            .iter()
            .map(|row| Some(if let Some(top) = row.top.as_ref() { side(top).map(|level| level.map(part)) } else { [None; TOP] }));
        Arc::new(FixedSizeListArray::from_iter_primitive::<Float64Type, _, _>(levels, TOP as i32))
    };
    let trade_column = |part: fn(&(f64, f64, String)) -> f64| Float64Array::from_iter(rows.iter().map(|row| row.trade.as_ref().map(part)));
    Ok(RecordBatch::try_new(
        schema,
        vec![
            Arc::new(Int64Array::from_iter_values(rows.iter().map(|r| r.capture.ts_ns))),
            Arc::new(Int64Array::from_iter_values(rows.iter().map(|r| r.capture.record_no))),
            Arc::new(Int32Array::from_iter_values(rows.iter().map(|r| r.message_no))),
            Arc::new(Int64Array::from_iter(rows.iter().map(|r| r.venue_ms))),
            Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.market.as_str()))),
            Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.asset.as_str()))),
            Arc::new(StringArray::from_iter_values(rows.iter().map(|r| if r.trade.is_some() { "trade" } else { "book" }))),
            level_column(|top| &top.bids, |level| level.0),
            level_column(|top| &top.bids, |level| level.1),
            level_column(|top| &top.asks, |level| level.0),
            level_column(|top| &top.asks, |level| level.1),
            Arc::new(trade_column(|trade| trade.0)),
            Arc::new(trade_column(|trade| trade.1)),
            Arc::new(StringArray::from_iter(rows.iter().map(|r| r.trade.as_ref().map(|trade| trade.2.as_str())))),
        ],
    )?)
}

struct Source {
    stream: String,
    hour_ns: i64,
    sha256: String,
    bytes: u64,
}

impl Source {
    fn from_first(first: &Value, sha256: String, bytes: u64) -> Result<Self> {
        let stream = first.get("stream").and_then(Value::as_str).ok_or_else(|| invalid("source stream is missing"))?;
        if !stream.starts_with("clob_") {
            return Err(invalid("only Intl clob_ streams support lossless book materialization").into());
        }
        let tw = first.get("tw").and_then(Value::as_i64).filter(|&n| n >= 0).ok_or_else(|| invalid("invalid receive clock"))?;
        let hour_ns = tw.div_euclid(HOUR_NS) * HOUR_NS;
        if hour_ns.checked_add(HOUR_NS).is_none_or(|end| end > wall_ns()) {
            return Err(invalid("cannot materialize an open capture hour").into());
        }
        Ok(Self { stream: stream.to_string(), hour_ns, sha256, bytes })
    }

    fn metadata(&self, view: &str) -> HashMap<String, String> {
        let prefix = format!("doppel.book-{view}");
        HashMap::from([
            (format!("{prefix}.schema"), "1".to_string()),
            (format!("{prefix}.source-sha256"), self.sha256.clone()),
            (format!("{prefix}.source-bytes"), self.bytes.to_string()),
            (format!("{prefix}.source-stream"), self.stream.clone()),
            (format!("{prefix}.source-hour-ns"), self.hour_ns.to_string()),
        ])
    }

    fn capture(&self, row: &Value, record_no: usize) -> Result<Capture> {
        let seq = row.get("seq").and_then(Value::as_i64).filter(|&n| n > 0).ok_or_else(|| invalid("invalid capture sequence"))?;
        let ts_ns = row.get("tw").and_then(Value::as_i64).ok_or_else(|| invalid("invalid receive clock"))?;
        if row.get("stream").and_then(Value::as_str) != Some(self.stream.as_str())
            || !(self.hour_ns..self.hour_ns + HOUR_NS).contains(&ts_ns)
        {
            return Err(invalid("capture record falls outside the materialized stream or hour").into());
        }
        Ok(Capture { ts_ns, seq, record_no: i64::try_from(record_no)? })
    }
}

fn identity(mut source: impl io::Read) -> io::Result<(String, u64)> {
    let mut sha = Sha256::new();
    let bytes = io::copy(&mut source, &mut sha)?;
    Ok((hex::encode(sha.finalize()), bytes))
}

fn parq(out: &mut NamedTempFile, schema: Arc<Schema>) -> Result<ArrowWriter<&mut File>> {
    let mut metadata = schema.metadata().iter().map(|(k, v)| KeyValue::new(k.clone(), v.clone())).collect::<Vec<_>>();
    metadata.sort_by(|a, b| a.key.cmp(&b.key));
    let properties = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::default()))
        .set_max_row_group_row_count(Some(ROW_GROUP))
        .set_key_value_metadata(Some(metadata))
        .build();
    Ok(ArrowWriter::try_new(out.as_file_mut(), schema, Some(properties))?)
}

/// Every compact projection ending in `.parquet` has a sibling `<stem>.replay.parquet`.
pub fn replay_path(output: &Path) -> PathBuf {
    output.with_extension("replay.parquet")
}

/// Stream one closed hour into both views. The compact file publishes last as the commit marker.
pub fn materialize(input: &Path, output: &Path) -> Result<()> {
    if output.extension().and_then(|name| name.to_str()) != Some("parquet") {
        return Err(invalid("output must end in .parquet").into());
    }
    let side = replay_path(output);
    if output.exists() || side.exists() {
        return Err(io::Error::new(io::ErrorKind::AlreadyExists, "materialization output already exists").into());
    }
    let mut file = File::open(input)?;
    let (sha256, bytes) = identity(&mut file)?;
    file.rewind()?;
    let mut input_rows = sealed_segment_records(file, input)?;
    let first = input_rows.next().ok_or_else(|| invalid("capture hour is empty"))??;
    let source = Source::from_first(&first, sha256, bytes)?;
    let parent = output.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let mut side_tmp = NamedTempFile::new_in(parent)?;
    let mut compact_tmp = NamedTempFile::new_in(parent)?;
    let schema = replay_schema(source.metadata("replay"));
    let mut writer = parq(&mut side_tmp, schema.clone())?;
    let mut projection = Projection::default();
    let mut patches = SnapshotPatches::default();
    let mut frames = Vec::with_capacity(BATCH);
    let mut source_records = 0_u64;
    for (record_no, record) in std::iter::once(Ok(first)).chain(input_rows).enumerate() {
        let record = record?;
        let capture = source.capture(&record, record_no)?;
        for mut frame in parse_frames(&record, capture, &mut projection)? {
            patches.observe(&mut frame);
            frames.push(frame);
            if frames.len() == BATCH {
                writer.write(&replay_batch(schema.clone(), &frames)?)?;
                frames.clear();
            }
        }
        source_records += 1;
    }
    if !frames.is_empty() {
        writer.write(&replay_batch(schema, &frames)?)?;
    }
    writer.append_key_value_metadata(KeyValue::new("doppel.book-replay.source-records".to_string(), source_records.to_string()));
    writer.close()?;
    if identity(File::open(input)?)? != (source.sha256.clone(), source.bytes) {
        return Err(invalid("capture bytes changed while materializing").into());
    }
    projection.rows.sort_by(|a, b| {
        (&a.market, a.capture.ts_ns, a.capture.record_no, a.message_no, &a.asset).cmp(&(
            &b.market,
            b.capture.ts_ns,
            b.capture.record_no,
            b.message_no,
            &b.asset,
        ))
    });
    let schema = compact_schema(source.metadata("projection"));
    let mut writer = parq(&mut compact_tmp, schema.clone())?;
    for rows in projection.rows.chunks(BATCH) {
        writer.write(&compact_batch(schema.clone(), rows)?)?;
    }
    writer.close()?;
    side_tmp.as_file().sync_all()?;
    compact_tmp.as_file().sync_all()?;
    side_tmp.persist_noclobber(&side)?;
    if let Err(error) = compact_tmp.persist_noclobber(output) {
        fs::remove_file(&side)?;
        return Err(error.into());
    }
    File::open(parent)?.sync_all()?;
    Ok(())
}
