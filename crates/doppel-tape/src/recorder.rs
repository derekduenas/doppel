//! One writer per stream: a dedicated OS thread owns the `SegmentWriter` and drains a bounded
//! channel. Sources hold a cheap `Recorder` handle and never block on it.
//!
//! Receive time is captured by the caller before anything else (see `ws.rs`) and travels with the
//! record. `try_send` is the only operation on the hot path. If the channel is full the record is
//! dropped and a shared counter is bumped; the next record that does get through carries the count,
//! and the writer writes `ctl: "backpressure_drop"` with that count (at that record's clocks)
//! immediately before it. If no record follows, the writer's 1 s idle tick writes the marker
//! itself. A drop is therefore always visible in the tape, never silent.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use serde_json::{json, Map, Value};

use crate::clock::{mono_ns, wall_ns};
use crate::segment::{SegmentWriter, FLUSH_INTERVAL};

/// Records per stream channel. Bounded by count: at ~4 KB a record this is ~16 MB per stream, so a
/// disk stall turns into explicit `backpressure_drop` markers well before the unit's memory cap.
pub const DEFAULT_QUEUE: usize = 4096;

/// Per-source counters behind status.json (same keys as Python's `SourceStats.as_dict`).
#[derive(Default)]
pub struct Stats {
    pub msgs: AtomicU64,
    pub reconnects: AtomicU64,
    pub stalls: AtomicU64,
    pub errors: AtomicU64,
    pub connected: AtomicBool,
    last_msg_tw: AtomicI64, // 0 = never
    pub backpressure_drops: AtomicU64,
    pub write_errors: AtomicU64,
    last_error: Mutex<Option<String>>,
    extra: Mutex<Map<String, Value>>,
}

impl Stats {
    pub fn error(&self, msg: impl Into<String>) {
        self.errors.fetch_add(1, Ordering::Relaxed);
        let mut m: String = msg.into();
        if m.chars().count() > 200 {
            m = m.chars().take(200).collect();
        }
        *self.last_error.lock().unwrap() = Some(m);
    }

    pub fn set_connected(&self, v: bool) {
        self.connected.store(v, Ordering::Relaxed);
    }

    pub fn set_extra(&self, k: &str, v: impl Into<Value>) {
        self.extra.lock().unwrap().insert(k.to_string(), v.into());
    }

    pub fn incr(&self, k: &str) {
        let mut e = self.extra.lock().unwrap();
        let n = e.get(k).and_then(Value::as_u64).unwrap_or(0);
        e.insert(k.to_string(), (n + 1).into());
    }

    pub fn extra(&self, k: &str) -> Option<Value> {
        self.extra.lock().unwrap().get(k).cloned()
    }

    pub fn last_error(&self) -> Option<String> {
        self.last_error.lock().unwrap().clone()
    }

    pub fn as_value(&self) -> Value {
        let last = self.last_msg_tw.load(Ordering::Relaxed);
        let age = (last != 0).then(|| ((wall_ns() - last) as f64 / 1e9 * 1000.0).round() / 1000.0);
        let mut m = Map::new();
        m.insert("msgs".into(), self.msgs.load(Ordering::Relaxed).into());
        m.insert("reconnects".into(), self.reconnects.load(Ordering::Relaxed).into());
        m.insert("stalls".into(), self.stalls.load(Ordering::Relaxed).into());
        m.insert("errors".into(), self.errors.load(Ordering::Relaxed).into());
        m.insert("connected".into(), self.connected.load(Ordering::Relaxed).into());
        m.insert("last_msg_age_s".into(), age.into());
        m.insert("last_error".into(), self.last_error().into());
        for (k, v) in self.extra.lock().unwrap().iter() {
            m.insert(k.clone(), v.clone());
        }
        m.insert("backpressure_drops".into(), self.backpressure_drops.load(Ordering::Relaxed).into());
        let we = self.write_errors.load(Ordering::Relaxed);
        if we > 0 {
            m.insert("write_errors".into(), we.into());
        }
        Value::Object(m)
    }
}

struct Item {
    raw: String,
    tw: i64,
    tm: i64,
    meta: Option<Value>,
    dropped_before: u64,
}

/// Cheap, cloneable handle to one stream's writer.
#[derive(Clone)]
pub struct Recorder {
    name: Arc<str>,
    tx: SyncSender<Item>,
    dropped: Arc<AtomicU64>,
    pub stats: Arc<Stats>,
}

impl Recorder {
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Record a data payload received at (`tw`, `tm`). Never blocks. Returns false if dropped.
    pub fn record_at(&self, raw: String, tw: i64, tm: i64, meta: Option<Value>) -> bool {
        self.stats.msgs.fetch_add(1, Ordering::Relaxed);
        self.stats.last_msg_tw.store(tw, Ordering::Relaxed);
        self.send(Item { raw, tw, tm, meta, dropped_before: 0 })
    }

    /// Record a control event (`raw = ""`, `meta = {"ctl": event, ..kw}`) stamped now.
    pub fn ctl(&self, event: &str, kw: Value) {
        let mut m = Map::new();
        m.insert("ctl".into(), event.into());
        if let Value::Object(o) = kw {
            m.extend(o);
        }
        self.send(Item { raw: String::new(), tw: wall_ns(), tm: mono_ns(), meta: Some(Value::Object(m)), dropped_before: 0 });
    }

    fn send(&self, mut item: Item) -> bool {
        let pending = self.dropped.swap(0, Ordering::AcqRel);
        item.dropped_before = pending;
        match self.tx.try_send(item) {
            Ok(()) => true,
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                self.dropped.fetch_add(pending + 1, Ordering::AcqRel);
                self.stats.backpressure_drops.fetch_add(1, Ordering::Relaxed);
                false
            }
        }
    }
}

/// Owner of a writer thread; `join` after every `Recorder` clone is dropped.
pub struct WriterThread {
    pub name: String,
    handle: JoinHandle<()>,
}

impl WriterThread {
    pub fn join(self) {
        let _ = self.handle.join();
    }
}

/// Spawn the writer thread for `stream` under `root` with a channel of `queue` items.
pub fn spawn_writer(root: PathBuf, stream: &str, level: i32, queue: usize) -> std::io::Result<(Recorder, WriterThread)> {
    let writer = SegmentWriter::new(root, stream, level)?;
    Ok(spawn_with(writer, queue))
}

pub fn spawn_with(mut writer: SegmentWriter, queue: usize) -> (Recorder, WriterThread) {
    let (tx, rx) = mpsc::sync_channel::<Item>(queue.max(1));
    let dropped = Arc::new(AtomicU64::new(0));
    let stats = Arc::new(Stats::default());
    let name: Arc<str> = Arc::from(writer.stream());
    let rec = Recorder { name: name.clone(), tx, dropped: dropped.clone(), stats: stats.clone() };
    let tname = name.clone();
    let handle = std::thread::Builder::new()
        .name(format!("w-{name}"))
        .spawn(move || {
            let st = stats;
            let report = |r: std::io::Result<()>| {
                if let Err(e) = r {
                    st.write_errors.fetch_add(1, Ordering::Relaxed);
                    eprintln!("doppel-taped: {tname}: write error: {e}");
                }
            };
            let drop_marker = |w: &mut SegmentWriter, n: u64, tw: i64, tm: i64| {
                let meta = json!({"ctl": "backpressure_drop", "count": n});
                w.append("", tw, tm, Some(&meta)).map(|_| ())
            };
            loop {
                match rx.recv_timeout(FLUSH_INTERVAL) {
                    Ok(it) => {
                        if it.dropped_before > 0 {
                            report(drop_marker(&mut writer, it.dropped_before, it.tw, it.tm));
                        }
                        report(writer.append(&it.raw, it.tw, it.tm, it.meta.as_ref()).map(|_| ()));
                    }
                    Err(RecvTimeoutError::Timeout) => {}
                    Err(RecvTimeoutError::Disconnected) => break,
                }
                if writer.since_flush().is_none_or(|d| d >= FLUSH_INTERVAL) {
                    let n = dropped.swap(0, Ordering::AcqRel);
                    if n > 0 {
                        report(drop_marker(&mut writer, n, wall_ns(), mono_ns()));
                    }
                    report(writer.flush());
                    report(writer.close_if_stale(wall_ns()).map(|_| ()));
                }
            }
            let n = dropped.swap(0, Ordering::AcqRel);
            if n > 0 {
                report(drop_marker(&mut writer, n, wall_ns(), mono_ns()));
            }
            report(writer.close());
        })
        .expect("spawn writer thread");
    (rec, WriterThread { name: name.to_string(), handle })
}

/// Snapshot of all stats keyed by stream name (status.json "sources").
pub fn stats_map(list: &[(String, Arc<Stats>)]) -> Value {
    let m: BTreeMap<_, _> = list.iter().map(|(n, s)| (n.clone(), s.as_value())).collect();
    serde_json::to_value(m).expect("stats serialize")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::segment::{read_segment, segment_path};
    use std::time::Duration;

    #[test]
    fn backpressure_drops_are_recorded_not_silent() {
        let d = tempfile::tempdir().unwrap();
        // Build the channel by hand so the "writer" is stalled: nothing drains it yet.
        let (tx, rx) = mpsc::sync_channel::<Item>(2);
        let dropped = Arc::new(AtomicU64::new(0));
        let stats = Arc::new(Stats::default());
        let rec = Recorder { name: Arc::from("s"), tx, dropped: dropped.clone(), stats: stats.clone() };
        let t0 = 1_795_622_400_000_000_000;
        for i in 0..6 {
            rec.record_at(format!("m{i}"), t0 + i, i, None);
        }
        assert_eq!(stats.backpressure_drops.load(Ordering::Relaxed), 4);
        assert_eq!(dropped.load(Ordering::Relaxed), 4);
        // drain the two queued items, then the next record carries the drop count
        let a = rx.recv().unwrap();
        let b = rx.recv().unwrap();
        assert_eq!((a.dropped_before, b.dropped_before), (0, 0));
        rec.record_at("m6".into(), t0 + 6, 6, None);
        let c = rx.recv().unwrap();
        assert_eq!(c.dropped_before, 4);
        assert_eq!(dropped.load(Ordering::Relaxed), 0);

        // and the real writer turns it into a ctl record right before that payload
        let w = SegmentWriter::new(d.path(), "s", 3).unwrap();
        let (rec2, th) = spawn_with(w, 8);
        rec2.dropped.store(3, Ordering::Relaxed);
        rec2.record_at("after".into(), t0 + 9, 9, None);
        drop(rec2);
        th.join();
        let recs = read_segment(&segment_path(d.path(), "s", t0), true).unwrap();
        assert_eq!(recs.len(), 2);
        assert_eq!(recs[0]["meta"], json!({"ctl": "backpressure_drop", "count": 3}));
        assert_eq!(recs[0]["tw"], t0 + 9);
        assert_eq!(recs[1]["raw"], "after");
    }

    #[test]
    fn idle_tick_writes_pending_drop_marker() {
        let d = tempfile::tempdir().unwrap();
        let w = SegmentWriter::new(d.path(), "s", 3).unwrap();
        let (rec, th) = spawn_with(w, 8);
        rec.ctl("connect", json!({"url": "wss://x"}));
        rec.dropped.store(2, Ordering::Relaxed);
        std::thread::sleep(Duration::from_millis(1300));
        drop(rec);
        th.join();
        let mut recs = Vec::new();
        for day in std::fs::read_dir(d.path().join("s")).unwrap() {
            let p = day.unwrap().path();
            if p.is_dir() {
                for f in std::fs::read_dir(p).unwrap() {
                    recs.extend(read_segment(&f.unwrap().path(), true).unwrap());
                }
            }
        }
        let ctls: Vec<_> = recs.iter().map(|r| r["meta"]["ctl"].as_str().unwrap().to_string()).collect();
        assert_eq!(ctls, ["connect", "backpressure_drop"]);
        assert_eq!(recs[1]["meta"]["count"], 2);
    }

    #[test]
    fn status_schema() {
        let s = Stats::default();
        s.set_extra("targets", 3);
        let v = s.as_value();
        let keys: Vec<_> = v.as_object().unwrap().keys().cloned().collect();
        assert_eq!(&keys[..7], ["msgs", "reconnects", "stalls", "errors", "connected", "last_msg_age_s", "last_error"]);
        assert_eq!(v["targets"], 3);
        assert_eq!(v["last_msg_age_s"], Value::Null);
    }
}
