//! Append-only tape segments, byte-compatible with `src/doppel/tape/segment.py`.
//!
//! Layout: `<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst`, a sequence of independent zstd frames, each
//! holding newline-delimited JSON records
//! `{"seq","stream","tw","tm","crc","raw","meta"?}` serialized compactly in exactly that key order
//! (what `orjson.dumps` emits). `crc` is CRC32 (IEEE, as `zlib.crc32`) of the UTF-8 `raw`. Every
//! flush closes a frame and fsyncs, so a crash loses at most the unflushed tail. Closing a segment
//! appends one line to `<root>/<stream>/manifest.jsonl`.

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::clock::{utc_parts, wall_ns};

pub const FLUSH_INTERVAL: Duration = Duration::from_secs(1);

/// `<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst` for a wall time in ns (UTC hour).
pub fn segment_path(root: &Path, stream: &str, wall_ns: i64) -> PathBuf {
    let (y, m, d, h, _) = utc_parts(wall_ns.div_euclid(1_000_000_000));
    root.join(stream).join(format!("{y:04}{m:02}{d:02}")).join(format!("{h:02}.dtape.zst"))
}

/// Serialize one record line (with trailing newline) exactly as Python's writer does.
pub fn encode_record(seq: u64, stream: &str, tw: i64, tm: i64, raw: &str, meta: Option<&Value>) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len() + stream.len() + 96);
    out.extend_from_slice(b"{\"seq\":");
    out.extend_from_slice(seq.to_string().as_bytes());
    out.extend_from_slice(b",\"stream\":");
    serde_json::to_writer(&mut out, stream).expect("string serializes");
    out.extend_from_slice(b",\"tw\":");
    out.extend_from_slice(tw.to_string().as_bytes());
    out.extend_from_slice(b",\"tm\":");
    out.extend_from_slice(tm.to_string().as_bytes());
    out.extend_from_slice(b",\"crc\":");
    out.extend_from_slice(crc32fast::hash(raw.as_bytes()).to_string().as_bytes());
    out.extend_from_slice(b",\"raw\":");
    serde_json::to_writer(&mut out, raw).expect("string serializes");
    if let Some(m) = meta.filter(|m| !is_empty_meta(m)) {
        out.extend_from_slice(b",\"meta\":");
        serde_json::to_writer(&mut out, m).expect("value serializes");
    }
    out.extend_from_slice(b"}\n");
    out
}

/// Python writes `meta` only when it is truthy (`if meta:`).
fn is_empty_meta(m: &Value) -> bool {
    match m {
        Value::Null => true,
        Value::Object(o) => o.is_empty(),
        _ => false,
    }
}

#[derive(Debug, Default, Clone)]
struct SegmentStats {
    records: u64,
    raw_bytes: u64,
    first_tw: Option<i64>,
    last_tw: Option<i64>,
}

/// One writer per stream; owned by a single thread.
pub struct SegmentWriter {
    root: PathBuf,
    stream: String,
    level: i32,
    path: Option<PathBuf>,
    fh: Option<File>,
    buf: Vec<u8>,
    last_flush: Option<Instant>,
    seq: u64,
    stats: SegmentStats,
    hash: Sha256,
}

impl SegmentWriter {
    pub fn new(root: impl Into<PathBuf>, stream: &str, level: i32) -> io::Result<Self> {
        let root = root.into();
        let seq = resume_seq(&root, stream)?;
        Ok(Self {
            root,
            stream: stream.to_string(),
            level,
            path: None,
            fh: None,
            buf: Vec::new(),
            last_flush: None,
            seq,
            stats: SegmentStats::default(),
            hash: Sha256::new(),
        })
    }

    pub fn seq(&self) -> u64 {
        self.seq
    }

    pub fn stream(&self) -> &str {
        &self.stream
    }

    /// Append one record; rotation follows the record's own `tw`. Returns its seq.
    pub fn append(&mut self, raw: &str, tw: i64, tm: i64, meta: Option<&Value>) -> io::Result<u64> {
        let path = segment_path(&self.root, &self.stream, tw);
        if self.path.as_ref() != Some(&path) {
            self.rotate(path)?;
        }
        self.seq += 1;
        self.buf.extend_from_slice(&encode_record(self.seq, &self.stream, tw, tm, raw, meta));
        let st = &mut self.stats;
        st.records += 1;
        st.raw_bytes += raw.chars().count() as u64; // Python: len(str), i.e. code points
        st.first_tw.get_or_insert(tw);
        st.last_tw = Some(tw);
        if self.last_flush.is_none_or(|t| t.elapsed() >= FLUSH_INTERVAL) {
            self.flush()?;
        }
        Ok(self.seq)
    }

    /// Flush buffered records as one zstd frame, then fsync.
    pub fn flush(&mut self) -> io::Result<()> {
        self.last_flush = Some(Instant::now());
        let Some(fh) = self.fh.as_mut() else { return Ok(()) };
        if self.buf.is_empty() {
            return Ok(());
        }
        let frame = zstd::bulk::compress(&self.buf, self.level)?;
        self.buf.clear();
        fh.write_all(&frame)?;
        self.hash.update(&frame);
        fh.sync_data()?;
        Ok(())
    }

    /// Time since the last flush attempt (None before the first).
    pub fn since_flush(&self) -> Option<Duration> {
        self.last_flush.map(|t| t.elapsed())
    }

    pub fn close(&mut self) -> io::Result<()> {
        self.flush()?;
        if self.fh.take().is_some() {
            self.write_manifest()?;
        }
        self.path = None;
        Ok(())
    }

    /// Close the open segment if it belongs to an hour other than `wall_ns`'s.
    pub fn close_if_stale(&mut self, wall_ns: i64) -> io::Result<bool> {
        match &self.path {
            Some(p) if *p != segment_path(&self.root, &self.stream, wall_ns) => {
                self.close()?;
                Ok(true)
            }
            _ => Ok(false),
        }
    }

    fn rotate(&mut self, path: PathBuf) -> io::Result<()> {
        self.close()?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        self.hash = Sha256::new();
        if path.exists() {
            // restart inside the same hour: hash what is already there (rare)
            self.hash.update(fs::read(&path)?);
        }
        self.fh = Some(OpenOptions::new().create(true).append(true).open(&path)?);
        self.path = Some(path);
        self.stats = SegmentStats::default();
        Ok(())
    }

    fn write_manifest(&mut self) -> io::Result<()> {
        let Some(path) = self.path.as_ref() else { return Ok(()) };
        if self.stats.records == 0 {
            return Ok(());
        }
        let sdir = self.root.join(&self.stream);
        let rel = path.strip_prefix(&sdir).unwrap_or(path).to_string_lossy().into_owned();
        let sha = hex::encode(std::mem::take(&mut self.hash).finalize());
        let line = json!({
            "file": rel,
            "sha256": sha,
            "bytes": fs::metadata(path)?.len(),
            "records": self.stats.records,
            "raw_bytes": self.stats.raw_bytes,
            "first_tw": self.stats.first_tw,
            "last_tw": self.stats.last_tw,
            "last_seq": self.seq,
            "closed_tw": wall_ns(),
        });
        let mut bytes = serde_json::to_vec(&line).expect("manifest serializes");
        bytes.push(b'\n');
        let mut fh = OpenOptions::new().create(true).append(true).open(sdir.join("manifest.jsonl"))?;
        fh.write_all(&bytes)?;
        fh.sync_data()?;
        Ok(())
    }
}

impl Drop for SegmentWriter {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

/// Continue sequence numbers across restarts: the manifest's max `last_seq`, and any higher seq in
/// the newest segment (which a crash may have left un-manifested).
pub fn resume_seq(root: &Path, stream: &str) -> io::Result<u64> {
    let sdir = root.join(stream);
    let mut last = 0u64;
    if let Ok(fh) = File::open(sdir.join("manifest.jsonl")) {
        for line in BufReader::new(fh).lines() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<Value>(&line) {
                last = last.max(v.get("last_seq").and_then(Value::as_u64).unwrap_or(0));
            }
        }
    }
    if let Some(newest) = newest_segment(&sdir)? {
        for rec in read_segment(&newest, false)? {
            last = last.max(rec.get("seq").and_then(Value::as_u64).unwrap_or(0));
        }
    }
    Ok(last)
}

/// Lexicographically last `*/*.dtape.zst` under a stream directory (Python's `sorted(glob)[-1]`).
fn newest_segment(sdir: &Path) -> io::Result<Option<PathBuf>> {
    let mut segs = Vec::new();
    let Ok(days) = fs::read_dir(sdir) else { return Ok(None) };
    for day in days {
        let day = day?.path();
        if !day.is_dir() {
            continue;
        }
        for f in fs::read_dir(&day)? {
            let f = f?.path();
            if f.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with(".dtape.zst")) {
                segs.push(f);
            }
        }
    }
    segs.sort();
    Ok(segs.pop())
}

/// Read all records of a segment. A truncated or corrupt tail ends the read (records already
/// decoded are kept). With `verify`, a CRC mismatch is an error.
pub fn read_segment(path: &Path, verify: bool) -> io::Result<Vec<Value>> {
    let mut dec = zstd::stream::read::Decoder::new(File::open(path)?)?;
    let mut data = Vec::new();
    let mut chunk = vec![0u8; 1 << 16];
    loop {
        match dec.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => data.extend_from_slice(&chunk[..n]),
            Err(_) => break, // truncated tail frame from a crash
        }
    }
    let mut out = Vec::new();
    for line in data.split(|b| *b == b'\n') {
        if line.is_empty() {
            continue;
        }
        let Ok(rec) = serde_json::from_slice::<Value>(line) else { continue };
        if verify {
            let raw = rec.get("raw").and_then(Value::as_str).unwrap_or("");
            if Some(u64::from(crc32fast::hash(raw.as_bytes()))) != rec.get("crc").and_then(Value::as_u64) {
                return Err(io::Error::new(io::ErrorKind::InvalidData, format!("CRC mismatch in {}", path.display())));
            }
        }
        out.push(rec);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const H: i64 = 3_600_000_000_000;
    const T0: i64 = 1_795_622_400_000_000_000; // 2026-11-25 16:00:00Z

    fn manifest(root: &Path, s: &str) -> Vec<Value> {
        fs::read_to_string(root.join(s).join("manifest.jsonl")).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
    }

    #[test]
    fn record_line_matches_python_shape() {
        let meta = json!({"ctl": "connect", "url": "wss://x"});
        let line = encode_record(7, "s", 10, 20, "", Some(&meta));
        assert_eq!(
            String::from_utf8(line).unwrap(),
            "{\"seq\":7,\"stream\":\"s\",\"tw\":10,\"tm\":20,\"crc\":0,\"raw\":\"\",\"meta\":{\"ctl\":\"connect\",\"url\":\"wss://x\"}}\n"
        );
        let line = encode_record(1, "s", 1, 2, "a\"b\né", Some(&json!({})));
        let crc = crc32fast::hash("a\"b\né".as_bytes());
        assert_eq!(
            String::from_utf8(line).unwrap(),
            format!("{{\"seq\":1,\"stream\":\"s\",\"tw\":1,\"tm\":2,\"crc\":{crc},\"raw\":\"a\\\"b\\né\"}}\n")
        );
    }

    #[test]
    fn paths() {
        let p = segment_path(Path::new("/r"), "s", T0 + 5 * H + 1);
        assert_eq!(p, PathBuf::from("/r/s/20261125/21.dtape.zst"));
    }

    #[test]
    fn roundtrip_crc_and_manifest() {
        let d = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(d.path(), "s", 6).unwrap();
        for i in 0..5 {
            w.append(&format!("msg{i}"), T0 + i, i, None).unwrap();
        }
        w.close().unwrap();
        let recs = read_segment(&segment_path(d.path(), "s", T0), true).unwrap();
        let raws: Vec<_> = recs.iter().map(|r| r["raw"].as_str().unwrap().to_string()).collect();
        assert_eq!(raws, (0..5).map(|i| format!("msg{i}")).collect::<Vec<_>>());
        assert_eq!(recs.iter().map(|r| r["seq"].as_u64().unwrap()).collect::<Vec<_>>(), vec![1, 2, 3, 4, 5]);
        let m = manifest(d.path(), "s");
        assert_eq!(m.len(), 1);
        assert_eq!(m[0]["file"], "20261125/16.dtape.zst");
        assert_eq!(m[0]["records"], 5);
        assert_eq!(m[0]["last_seq"], 5);
        assert_eq!(m[0]["raw_bytes"], 20);
        assert_eq!(m[0]["first_tw"], T0);
        assert_eq!(m[0]["last_tw"], T0 + 4);
        let bytes = fs::read(segment_path(d.path(), "s", T0)).unwrap();
        assert_eq!(m[0]["bytes"], bytes.len() as u64);
        assert_eq!(m[0]["sha256"], hex::encode(Sha256::digest(&bytes)));
        let keys: Vec<_> = m[0].as_object().unwrap().keys().cloned().collect();
        assert_eq!(keys, ["file", "sha256", "bytes", "records", "raw_bytes", "first_tw", "last_tw", "last_seq", "closed_tw"]);
    }

    #[test]
    fn hour_rotation_and_seq_resume() {
        let d = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(d.path(), "s", 6).unwrap();
        w.append("a", T0, 0, None).unwrap();
        w.append("b", T0 + H, 0, None).unwrap(); // next hour: new file, first one manifested
        assert_eq!(manifest(d.path(), "s").len(), 1);
        w.close().unwrap();
        let mut w2 = SegmentWriter::new(d.path(), "s", 6).unwrap();
        assert_eq!(w2.append("c", T0 + H + 1, 0, None).unwrap(), 3);
        w2.close().unwrap();
        let m = manifest(d.path(), "s");
        assert_eq!(m.len(), 3);
        // same-hour restart: the hash covers the whole file, records count this session only
        let p = segment_path(d.path(), "s", T0 + H);
        assert_eq!(m[2]["sha256"], hex::encode(Sha256::digest(fs::read(&p).unwrap())));
        assert_eq!(m[2]["records"], 1);
        assert_eq!(read_segment(&p, true).unwrap().len(), 2);
    }

    #[test]
    fn resume_from_unmanifested_crash_segment() {
        let d = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(d.path(), "s", 6).unwrap();
        for i in 0..4 {
            w.append("x", T0 + i, 0, None).unwrap();
            w.flush().unwrap();
        }
        std::mem::forget(w); // crash: no close, no manifest
        assert!(!d.path().join("s/manifest.jsonl").exists());
        assert_eq!(resume_seq(d.path(), "s").unwrap(), 4);
    }

    #[test]
    fn truncated_tail_is_tolerated() {
        let d = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(d.path(), "s", 6).unwrap();
        w.append("one", T0, 0, None).unwrap();
        w.flush().unwrap();
        w.append("two", T0 + 1, 0, None).unwrap();
        w.flush().unwrap();
        w.close().unwrap();
        let p = segment_path(d.path(), "s", T0);
        let data = fs::read(&p).unwrap();
        fs::write(&p, &data[..data.len() - 5]).unwrap();
        let recs = read_segment(&p, true).unwrap();
        assert_eq!(recs[0]["raw"], "one");
        assert!(recs.len() <= 2);
    }

    #[test]
    fn close_if_stale_and_empty_close() {
        let d = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(d.path(), "s", 6).unwrap();
        assert!(!w.close_if_stale(T0).unwrap());
        w.append("a", T0, 0, None).unwrap();
        assert!(!w.close_if_stale(T0 + 10).unwrap());
        assert!(w.close_if_stale(T0 + H).unwrap());
        assert_eq!(manifest(d.path(), "s").len(), 1);
        w.close().unwrap(); // nothing open: no second manifest line
        assert_eq!(manifest(d.path(), "s").len(), 1);
    }

    #[test]
    fn crc_mismatch_is_an_error() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("bad.dtape.zst");
        let line = b"{\"seq\":1,\"stream\":\"s\",\"tw\":1,\"tm\":1,\"crc\":1,\"raw\":\"x\"}\n";
        fs::write(&p, zstd::bulk::compress(line, 3).unwrap()).unwrap();
        assert!(read_segment(&p, true).is_err());
        assert_eq!(read_segment(&p, false).unwrap().len(), 1);
    }
}
