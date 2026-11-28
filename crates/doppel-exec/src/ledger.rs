//! Durable hash-chained JSONL. The order reservation is fsynced before a request leaves.
use crate::{oms::State, Result, Venue};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{File, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::Path,
};

#[derive(Serialize, Deserialize)]
struct Payload {
    seq: u64,
    previous_sha256: String,
    event: Value,
    state: State,
}

#[derive(Serialize, Deserialize)]
struct Row {
    sha256: String,
    payload: Payload,
}

pub struct Ledger {
    file: File,
    seq: u64,
    head: String,
}

pub(crate) type Recovery = (Ledger, Option<State>, BTreeSet<(Venue, String)>);

impl Ledger {
    pub(crate) fn open(path: &Path) -> Result<Recovery> {
        let file = OpenOptions::new().create(true).read(true).append(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(path)?;
        let meta = file.metadata()?;
        if !meta.is_file() || meta.mode() & 0o077 != 0 {
            return Err("ledger must be a private regular file".into());
        }
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            File::open(parent)?.sync_all()?;
        }
        // The file descriptor holds the writer lease until the OMS exits.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("ledger already has a writer".into());
        }
        let mut ledger = Self { file, seq: 0, head: "0".repeat(64) };
        let mut state = None;
        let mut seen = BTreeSet::new();
        let mut reader = BufReader::new(&ledger.file);
        let mut line = String::new();
        while reader.read_line(&mut line)? != 0 {
            if !line.ends_with('\n') {
                return Err("torn ledger row requires reconciliation".into());
            }
            let row: Row = serde_json::from_str(&line)?;
            let p = row.payload;
            if p.seq != ledger.seq || p.previous_sha256 != ledger.head || hex::encode(Sha256::digest(serde_json::to_vec(&p)?)) != row.sha256
            {
                return Err("ledger hash chain failed".into());
            }
            if let Some(id) = p.event.get("fill_id").and_then(Value::as_str) {
                let venue = serde_json::from_value(p.event["venue"].clone())?;
                if !seen.insert((venue, id.to_owned())) {
                    return Err("ledger already contains this fill".into());
                }
            }
            ledger.seq += 1;
            ledger.head = row.sha256;
            state = Some(p.state);
            line.clear();
        }
        drop(reader);
        Ok((ledger, state, seen))
    }

    pub(crate) fn append(&mut self, event: Value, state: &State) -> Result<()> {
        let payload = Payload { seq: self.seq, previous_sha256: self.head.clone(), event, state: state.clone() };
        let sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&payload)?));
        let row = Row { sha256, payload };
        let mut raw = serde_json::to_vec(&row)?;
        raw.push(b'\n');
        self.file.write_all(&raw)?;
        self.file.sync_data()?;
        self.seq += 1;
        self.head = row.sha256;
        Ok(())
    }
}
