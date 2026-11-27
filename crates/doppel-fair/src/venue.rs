//! Per-venue L2 frame decoders: port of the WebSocket handlers in PERELMAN `books.py` (`coinbase_at`,
//! `kraken`, `bitstamp`, `gemini`, `cryptocom`, `bullish`) and of the LMAX REST parser in
//! `recorder.py` `REST_FEEDS`. They rebuild an [`L2Book`] from the raw frames that `doppel-taped`
//! records in the `spot_l2_<venue>` streams and hand the replica a [`BookSnapshot`].
//!
//! Integrity checks beyond `books.py`:
//! - Coinbase `sequence_num` and Gemini `socket_sequence` (per connection, every frame) and
//!   Crypto.com `pu` == previous `u` must be contiguous.
//! - Kraken's CRC32 over the top 10 levels per side must match `checksum`.
//!
//! Any failure resets the book; it stays invalid until the venue's next snapshot. So does every
//! control record in the tape (connect, disconnect, reconnect, seq_gap, backpressure_drop, ...):
//! continuity across one is not guaranteed.
//!
//! Prices and sizes are accepted as JSON strings or numbers. `retrieved_ns` is the record's
//! receive time `tw`, never a venue timestamp (no look-ahead).

use serde_json::Value;

use crate::book::{L2Book, Side};
use crate::brti::{BookSnapshot, Level};

/// Kraken book subscription depth (`books.py` KRAKEN_DEPTH); the local book is truncated to it.
pub const KRAKEN_DEPTH: usize = 1000;
/// Crypto.com `book.BTC_USD.150`.
pub const CRYPTOCOM_DEPTH: usize = 150;
/// Kraken BTC/USD price and quantity decimals, used to format the checksum input.
pub const KRAKEN_PRICE_DECIMALS: usize = 1;
pub const KRAKEN_QTY_DECIMALS: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Venue {
    Coinbase,
    Kraken,
    Bitstamp,
    Gemini,
    Cryptocom,
    Lmax,
    Bullish,
}

impl Venue {
    /// PERELMAN `recorder.py` FEEDS order.
    pub const ALL: [Venue; 7] =
        [Venue::Coinbase, Venue::Kraken, Venue::Bitstamp, Venue::Gemini, Venue::Cryptocom, Venue::Lmax, Venue::Bullish];

    /// The name in `spot_l2_<name>` and in the replica's diagnostics.
    pub fn name(self) -> &'static str {
        match self {
            Venue::Coinbase => "coinbase",
            Venue::Kraken => "kraken",
            Venue::Bitstamp => "bitstamp",
            Venue::Gemini => "gemini",
            Venue::Cryptocom => "cryptocom",
            Venue::Lmax => "lmax",
            Venue::Bullish => "bullish",
        }
    }

    pub fn from_name(s: &str) -> Option<Venue> {
        Venue::ALL.into_iter().find(|v| v.name() == s)
    }
}

/// Why a frame broke the book's continuity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Integrity {
    SeqGap { expected: u64, got: u64 },
    Checksum { expected: u32, got: u32 },
}

/// One venue's book rebuilt from its tape.
#[derive(Clone, Debug)]
pub struct VenueBook {
    pub venue: Venue,
    pub book: L2Book,
    /// Coinbase `sequence_num`, Gemini `socket_sequence`, or Crypto.com `u` of the last frame.
    seq: Option<u64>,
    pub integrity_failures: u64,
}

impl VenueBook {
    pub fn new(venue: Venue) -> Self {
        Self { venue, book: L2Book::new(), seq: None, integrity_failures: 0 }
    }

    /// Forget everything: the next snapshot starts a new book and a new sequence.
    pub fn reset(&mut self) {
        self.book.reset();
        self.seq = None;
    }

    /// Apply one tape record (`raw`, `tw`, `meta`). A control record other than `subscribe` resets the book, and so does
    /// a REST response whose `meta.status` is not 200.
    pub fn apply_record(&mut self, raw: &str, tw_ns: i64, meta: Option<&Value>) -> Result<(), Integrity> {
        let failed_poll = self.venue == Venue::Lmax && meta.and_then(|m| m.get("status")).is_some_and(|s| s.as_u64() != Some(200));
        // `subscribe` only logs what was sent on an open connection. Gemini's initial book can land
        // before that record is written, and resetting on it would drop Gemini for the whole connection.
        let ctl = meta.and_then(|m| m.get("ctl")).and_then(Value::as_str);
        if ctl.is_some_and(|c| c != "subscribe") || failed_poll {
            self.reset();
            return Ok(());
        }
        self.apply(raw, tw_ns)
    }

    /// Apply one raw frame received at `tw_ns`. Frames that are not JSON or not book data are
    /// ignored; an integrity failure resets the book and is returned. LMAX is a REST snapshot
    /// venue: a body that is not a whole book resets it (PERELMAN `rest_poll`), so a failed poll
    /// never leaves an old book looking fresh.
    pub fn apply(&mut self, raw: &str, tw_ns: i64) -> Result<(), Integrity> {
        let Ok(m) = serde_json::from_str::<Value>(raw) else {
            if self.venue == Venue::Lmax {
                self.reset();
            }
            return Ok(());
        };
        let r = match self.venue {
            Venue::Coinbase => self.coinbase(&m),
            Venue::Kraken => self.kraken(&m),
            Venue::Bitstamp => {
                bitstamp(&mut self.book, &m);
                Ok(())
            }
            Venue::Gemini => self.gemini(&m),
            Venue::Cryptocom => self.cryptocom(&m),
            Venue::Lmax => {
                lmax(&mut self.book, &m);
                Ok(())
            }
            Venue::Bullish => {
                bullish(&mut self.book, &m);
                Ok(())
            }
        };
        match r {
            Ok(()) => {
                // books.py stamps every received frame (heartbeats included): an unchanged
                // incremental book is still current.
                self.book.last_update_ns = tw_ns;
                Ok(())
            }
            Err(e) => {
                self.integrity_failures += 1;
                self.reset();
                Err(e)
            }
        }
    }

    /// The book for the replica, when a snapshot with both sides has been applied.
    pub fn snapshot(&self) -> Option<BookSnapshot> {
        self.book.valid.then(|| self.book.snapshot())
    }

    fn next_seq(&mut self, got: u64) -> Result<(), Integrity> {
        if let Some(prev) = self.seq {
            if got != prev + 1 {
                return Err(Integrity::SeqGap { expected: prev + 1, got });
            }
        }
        self.seq = Some(got);
        Ok(())
    }

    /// Coinbase Advanced Trade `level2` (channel `l2_data`) + `heartbeats`. `sequence_num` counts
    /// every frame on the connection, heartbeats and subscription acks included.
    fn coinbase(&mut self, m: &Value) -> Result<(), Integrity> {
        if let Some(s) = m.get("sequence_num").and_then(Value::as_u64) {
            self.next_seq(s)?;
        }
        if m.get("channel").and_then(Value::as_str) != Some("l2_data") {
            return Ok(());
        }
        let b = &mut self.book;
        for ev in arr(m.get("events")) {
            let snap = ev.get("type").and_then(Value::as_str) == Some("snapshot");
            if snap {
                b.reset();
            } else if !b.valid {
                continue;
            }
            for u in arr(ev.get("updates")) {
                let side = if u.get("side").and_then(Value::as_str) == Some("bid") { Side::Bid } else { Side::Ask };
                if let (Some(p), Some(q)) = (num(u.get("price_level")), num(u.get("new_quantity"))) {
                    b.put(side, p, q);
                }
            }
            if snap {
                let (nb, na) = b.depth();
                b.valid = nb > 0 && na > 0;
                b.snapshots += 1;
            } else {
                b.updates += 1;
            }
        }
        if let Some(ns) = m.get("timestamp").and_then(Value::as_str).and_then(iso_ns) {
            b.venue_ts_ns = ns;
        }
        Ok(())
    }

    /// Kraken v2 `book`, depth 1000.
    fn kraken(&mut self, m: &Value) -> Result<(), Integrity> {
        if m.get("channel").and_then(Value::as_str) != Some("book") {
            return Ok(());
        }
        let Some(d) = m.get("data").and_then(|d| d.get(0)) else { return Ok(()) };
        let b = &mut self.book;
        let lv =
            |k: &str| -> Vec<Level> { arr(d.get(k)).iter().filter_map(|x| Some((num(x.get("price"))?, num(x.get("qty"))?))).collect() };
        let typ = m.get("type").and_then(Value::as_str);
        if typ == Some("snapshot") {
            b.replace(&lv("bids"), &lv("asks"));
        } else if typ == Some("update") && b.valid {
            for (p, q) in lv("bids") {
                b.put(Side::Bid, p, q);
            }
            for (p, q) in lv("asks") {
                b.put(Side::Ask, p, q);
            }
            truncate(b, KRAKEN_DEPTH);
            b.updates += 1;
        } else {
            return Ok(());
        }
        if let Some(ns) = d.get("timestamp").and_then(Value::as_str).and_then(iso_ns) {
            b.venue_ts_ns = ns;
        }
        if let (true, Some(want)) = (b.valid, d.get("checksum").and_then(Value::as_u64)) {
            let got = kraken_checksum(&b.snapshot());
            if u64::from(got) != want {
                return Err(Integrity::Checksum { expected: want as u32, got });
            }
        }
        Ok(())
    }

    /// Gemini v1 market data: `change` events carry the absolute remaining size; the first
    /// message's events have `reason: initial`.
    fn gemini(&mut self, m: &Value) -> Result<(), Integrity> {
        if let Some(s) = m.get("socket_sequence").and_then(Value::as_u64) {
            self.next_seq(s)?;
        }
        if m.get("type").and_then(Value::as_str) != Some("update") {
            return Ok(());
        }
        let events: Vec<&Value> = arr(m.get("events")).iter().filter(|e| e.get("type").and_then(Value::as_str) == Some("change")).collect();
        let initial = events.iter().any(|e| e.get("reason").and_then(Value::as_str) == Some("initial"));
        let b = &mut self.book;
        if !initial && !b.valid {
            return Ok(());
        }
        for e in events {
            let side = if e.get("side").and_then(Value::as_str) == Some("bid") { Side::Bid } else { Side::Ask };
            if let (Some(p), Some(q)) = (num(e.get("price")), num(e.get("remaining"))) {
                b.put(side, p, q);
            }
        }
        if initial {
            let (nb, na) = b.depth();
            b.valid = nb > 0 && na > 0;
            b.snapshots += 1;
        } else {
            b.updates += 1;
        }
        if let Some(ms) = m.get("timestampms").and_then(Value::as_i64) {
            b.venue_ts_ns = ms * 1_000_000;
        }
        Ok(())
    }

    /// Crypto.com `book.BTC_USD.150`: `book` is a snapshot, `book.update` a delta whose `pu` must
    /// equal the previous frame's `u`.
    fn cryptocom(&mut self, m: &Value) -> Result<(), Integrity> {
        let Some(r) = m.get("result") else { return Ok(()) };
        let Some(d) = r.get("data").and_then(|d| d.get(0)) else { return Ok(()) };
        let lv = |x: Option<&Value>| -> Vec<Level> { arr(x).iter().filter_map(|l| Some((num(l.get(0))?, num(l.get(1))?))).collect() };
        let u = d.get("u").or_else(|| d.get("s")).and_then(Value::as_u64);
        match r.get("channel").and_then(Value::as_str) {
            Some("book") => {
                self.book.replace(&lv(d.get("bids")), &lv(d.get("asks")));
                self.seq = u;
            }
            Some("book.update") if self.book.valid => {
                if let (Some(prev), Some(pu)) = (self.seq, d.get("pu").and_then(Value::as_u64)) {
                    if pu != prev {
                        return Err(Integrity::SeqGap { expected: prev, got: pu });
                    }
                }
                let up = d.get("update").unwrap_or(d);
                let b = &mut self.book;
                for (p, q) in lv(up.get("bids")) {
                    b.put(Side::Bid, p, q);
                }
                for (p, q) in lv(up.get("asks")) {
                    b.put(Side::Ask, p, q);
                }
                truncate(b, CRYPTOCOM_DEPTH);
                b.updates += 1;
                self.seq = u;
            }
            _ => return Ok(()),
        }
        if let Some(ms) = d.get("t").and_then(Value::as_i64) {
            self.book.venue_ts_ns = ms * 1_000_000;
        }
        Ok(())
    }
}

/// Bitstamp `order_book_btcusd`: a full top-100 snapshot on every change.
fn bitstamp(b: &mut L2Book, m: &Value) {
    let Some(d) = m.get("data").filter(|d| d.get("bids").is_some()) else { return };
    if m.get("event").and_then(Value::as_str) != Some("data") {
        return;
    }
    let lv = |k: &str| -> Vec<Level> { arr(d.get(k)).iter().filter_map(|l| Some((num(l.get(0))?, num(l.get(1))?))).collect() };
    b.replace(&lv("bids"), &lv("asks"));
    if let Some(us) = num(d.get("microtimestamp")) {
        b.venue_ts_ns = us as i64 * 1000;
    }
}

/// LMAX Digital REST `v1/orderbook/btc-usd`: a whole book, `{price, quantity}` strings. Anything
/// PERELMAN's parser would raise on (missing side, unparsable level) resets the book.
fn lmax(b: &mut L2Book, m: &Value) {
    let side = |k: &str| -> Option<Vec<Level>> {
        m.get(k)?.as_array()?.iter().map(|l| Some((num(l.get("price"))?, num(l.get("quantity"))?))).collect()
    };
    match (side("bids"), side("asks")) {
        (Some(bids), Some(asks)) => b.replace(&bids, &asks),
        _ => return b.reset(),
    }
    if let Some(ns) = m.get("timestamp").and_then(Value::as_str).and_then(iso_ns) {
        b.venue_ts_ns = ns;
    }
}

/// Bullish `l2Orderbook` (`dataType: V1TALevel2`): flat `[price, size, price, size, ...]` arrays.
fn bullish(b: &mut L2Book, m: &Value) {
    if m.get("dataType").and_then(Value::as_str) != Some("V1TALevel2") {
        return;
    }
    let Some(d) = m.get("data") else { return };
    if let Some(ms) = num(d.get("timestamp")) {
        b.venue_ts_ns = ms as i64 * 1_000_000;
    }
    let flat = |k: &str| -> Vec<Level> {
        let xs = arr(d.get(k));
        xs.as_chunks::<2>().0.iter().filter_map(|[p, q]| Some((num(Some(p))?, num(Some(q))?))).collect()
    };
    if m.get("type").and_then(Value::as_str) == Some("snapshot") {
        b.replace(&flat("bids"), &flat("asks"));
    } else if b.valid {
        for (p, q) in flat("bids") {
            b.put(Side::Bid, p, q);
        }
        for (p, q) in flat("asks") {
            b.put(Side::Ask, p, q);
        }
        b.updates += 1;
    }
}

pub(crate) fn arr(v: Option<&Value>) -> &[Value] {
    v.and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[])
}

/// A price or size given as a JSON string or number.
pub(crate) fn num(v: Option<&Value>) -> Option<f64> {
    match v? {
        Value::String(s) => s.parse().ok(),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// Keep the best `depth` levels per side.
fn truncate(b: &mut L2Book, depth: usize) {
    let (nb, na) = b.depth();
    if nb <= depth && na <= depth {
        return;
    }
    let s = b.snapshot();
    for &(p, _) in s.bids.iter().skip(depth) {
        b.put(Side::Bid, p, 0.0);
    }
    for &(p, _) in s.asks.iter().skip(depth) {
        b.put(Side::Ask, p, 0.0);
    }
}

/// Kraken v2 book checksum: CRC32 of the top 10 asks (ascending) then the top 10 bids
/// (descending), each level as price then qty with the decimal point and leading zeros removed.
pub fn kraken_checksum(s: &BookSnapshot) -> u32 {
    let mut text = String::new();
    let mut add = |x: f64, decimals: usize| {
        let f = format!("{x:.decimals$}").replace('.', "");
        text.push_str(f.trim_start_matches('0'));
    };
    for &(p, q) in s.asks.iter().take(10).chain(s.bids.iter().take(10)) {
        add(p, KRAKEN_PRICE_DECIMALS);
        add(q, KRAKEN_QTY_DECIMALS);
    }
    crc32(text.as_bytes())
}

/// CRC-32 (IEEE 802.3, as zlib).
fn crc32(data: &[u8]) -> u32 {
    let mut c = !0u32;
    for &byte in data {
        c ^= u32::from(byte);
        for _ in 0..8 {
            c = if c & 1 != 0 { (c >> 1) ^ 0xEDB8_8320 } else { c >> 1 };
        }
    }
    !c
}

/// RFC 3339 UTC time (`2026-11-24T15:30:00.5Z`, `+00:00` also accepted) to unix ns.
pub fn iso_ns(s: &str) -> Option<i64> {
    let s = s.strip_suffix('Z').or_else(|| s.strip_suffix("+00:00"))?;
    let (date, time) = s.split_once('T')?;
    let mut d = date.splitn(3, '-').map(|x| x.parse::<i64>().ok());
    let (y, mo, da) = (d.next()??, d.next()??, d.next()??);
    let (hms, frac) = time.split_once('.').unwrap_or((time, ""));
    let mut t = hms.splitn(3, ':').map(|x| x.parse::<i64>().ok());
    let (h, mi, se) = (t.next()??, t.next()??, t.next()??);
    if !frac.bytes().all(|c| c.is_ascii_digit()) || frac.len() > 9 {
        return None;
    }
    let frac_ns = if frac.is_empty() { 0 } else { format!("{frac:0<9}").parse::<i64>().ok()? };
    // days from civil (Howard Hinnant)
    let yy = if mo <= 2 { y - 1 } else { y };
    let era = yy.div_euclid(400);
    let yoe = yy - era * 400;
    let mp = (mo + 9) % 12;
    let doy = (153 * mp + 2) / 5 + da - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(((days * 86_400 + h * 3600 + mi * 60 + se) * 1_000_000_000) + frac_ns)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn feed(v: &mut VenueBook, frames: &[Value]) -> Vec<Result<(), Integrity>> {
        frames.iter().enumerate().map(|(i, f)| v.apply(&f.to_string(), 1000 + i as i64)).collect()
    }

    fn top(v: &VenueBook) -> Option<(f64, f64)> {
        v.snapshot().map(|s| (s.bids[0].0, s.asks[0].0))
    }

    #[test]
    fn iso_times() {
        assert_eq!(iso_ns("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(iso_ns("2026-11-25T16:00:00Z"), Some(1_795_622_400_000_000_000));
        assert_eq!(iso_ns("2026-11-25T16:00:00.5Z"), Some(1_795_622_400_500_000_000));
        assert_eq!(iso_ns("2026-11-25T16:00:00.123456789+00:00"), Some(1_795_622_400_123_456_789));
        assert_eq!(iso_ns("2024-02-29T00:00:01Z"), Some(1_709_164_801_000_000_000));
        assert_eq!(iso_ns("2026-11-25 16:00:00"), None);
        assert_eq!(iso_ns("2026-11-25T16:00:00.1234567891Z"), None);
    }

    #[test]
    fn coinbase_snapshot_updates_and_sequence_gap() {
        let upd =
            |side: &str, p: &str, q: &str| json!({"side": side, "event_time": "2026-11-25T16:00:00Z", "price_level": p, "new_quantity": q});
        let mut v = VenueBook::new(Venue::Coinbase);
        let frames = [
            json!({"channel": "subscriptions", "client_id": "", "timestamp": "2026-11-25T16:00:00Z", "sequence_num": 0, "events": []}),
            // an update before the snapshot is ignored
            json!({"channel": "l2_data", "timestamp": "2026-11-25T16:00:00Z", "sequence_num": 1,
                   "events": [{"type": "update", "product_id": "BTC-USD", "updates": [upd("bid", "99", "1")]}]}),
            json!({"channel": "l2_data", "timestamp": "2026-11-25T16:00:00.25Z", "sequence_num": 2,
                   "events": [{"type": "snapshot", "product_id": "BTC-USD", "updates": [upd("bid", "100", "1"), upd("bid", "98.5", "2"), upd("offer", "101", "1")]}]}),
            json!({"channel": "heartbeats", "timestamp": "2026-11-25T16:00:01Z", "sequence_num": 3, "events": [{"current_time": "x", "heartbeat_counter": 7}]}),
            json!({"channel": "l2_data", "timestamp": "2026-11-25T16:00:01.5Z", "sequence_num": 4,
                   "events": [{"type": "update", "product_id": "BTC-USD", "updates": [upd("offer", "101", "0"), upd("offer", "102", "3"), upd("bid", "100.5", "0.7")]}]}),
        ];
        assert!(feed(&mut v, &frames).iter().all(Result::is_ok));
        let s = v.snapshot().unwrap();
        assert_eq!(s.bids, vec![(100.5, 0.7), (100.0, 1.0), (98.5, 2.0)]);
        assert_eq!(s.asks, vec![(102.0, 3.0)]);
        assert_eq!(s.retrieved_ns, 1004);
        assert_eq!(v.book.venue_ts_ns, iso_ns("2026-11-25T16:00:01.5Z").unwrap());
        assert_eq!((v.book.snapshots, v.book.updates), (1, 1));
        // sequence 6 after 4: gap, the book is dropped until a new snapshot
        let gap = json!({"channel": "l2_data", "sequence_num": 6, "events": [{"type": "update", "updates": [upd("bid", "100.6", "1")]}]});
        assert_eq!(v.apply(&gap.to_string(), 2000), Err(Integrity::SeqGap { expected: 5, got: 6 }));
        assert!(v.snapshot().is_none());
        // a new connection (ctl record) starts the sequence over
        v.apply_record("", 2001, Some(&json!({"ctl": "connect", "url": "wss://x"}))).unwrap();
        let again = json!({"channel": "l2_data", "sequence_num": 0, "events": [{"type": "snapshot", "updates": [upd("bid", "1", "1"), upd("offer", "2", "1")]}]});
        v.apply(&again.to_string(), 2002).unwrap();
        assert_eq!(top(&v), Some((1.0, 2.0)));
        assert_eq!(v.integrity_failures, 1);
    }

    #[test]
    fn kraken_checksum_matches_docs_vector() {
        // docs.kraken.com guides/spot-ws-book-v2: expected 3310070434
        let bids = [
            ("45283.5", "0.10000000"),
            ("45283.4", "1.54582015"),
            ("45282.1", "0.10000000"),
            ("45281.0", "0.10000000"),
            ("45280.3", "1.54592586"),
            ("45279.0", "0.07990000"),
            ("45277.6", "0.03310103"),
            ("45277.5", "0.30000000"),
            ("45277.3", "1.54602737"),
            ("45276.6", "0.15445238"),
        ];
        let asks = [
            ("45285.2", "0.00100000"),
            ("45286.4", "1.54571953"),
            ("45286.6", "1.54571109"),
            ("45289.6", "1.54560911"),
            ("45290.2", "0.15890660"),
            ("45291.8", "1.54553491"),
            ("45294.7", "0.04454749"),
            ("45296.1", "0.35380000"),
            ("45297.5", "0.09945542"),
            ("45299.5", "0.18772827"),
        ];
        let lv = |xs: &[(&str, &str)]| xs.iter().map(|(p, q)| json!({"price": p, "qty": q})).collect::<Vec<_>>();
        let snap = json!({"channel": "book", "type": "snapshot",
                          "data": [{"symbol": "BTC/USD", "bids": lv(&bids), "asks": lv(&asks), "checksum": 3310070434u64,
                                    "timestamp": "2026-11-25T16:00:00.123456Z"}]});
        let mut v = VenueBook::new(Venue::Kraken);
        v.apply(&snap.to_string(), 5).unwrap();
        assert_eq!(top(&v), Some((45283.5, 45285.2)));
        assert_eq!(v.book.venue_ts_ns, 1_795_622_400_123_456_000);
        // the same book with numeric fields (what the live v2 feed sends) gives the same checksum
        let num = |xs: &[(&str, &str)]| {
            xs.iter().map(|(p, q)| json!({"price": p.parse::<f64>().unwrap(), "qty": q.parse::<f64>().unwrap()})).collect::<Vec<_>>()
        };
        let snap2 =
            json!({"channel": "book", "type": "snapshot", "data": [{"bids": num(&bids), "asks": num(&asks), "checksum": 3310070434u64}]});
        let mut w = VenueBook::new(Venue::Kraken);
        w.apply(&snap2.to_string(), 5).unwrap();
        assert_eq!(kraken_checksum(&w.snapshot().unwrap()), 3_310_070_434);
        // an update that leaves the top 10 unchanged keeps the checksum; a wrong one resets
        let upd = |cs: u64| json!({"channel": "book", "type": "update", "data": [{"bids": [{"price": 45000.0, "qty": 1.0}], "asks": [], "checksum": cs}]});
        w.apply(&upd(3310070434).to_string(), 6).unwrap();
        assert_eq!(w.book.depth(), (11, 10));
        let bad = w.apply(&upd(1).to_string(), 7);
        assert!(matches!(bad, Err(Integrity::Checksum { expected: 1, got: 3_310_070_434 })), "{bad:?}");
        assert!(w.snapshot().is_none());
        // updates before a snapshot are ignored
        w.apply(&upd(1).to_string(), 8).unwrap();
        assert!(w.snapshot().is_none());
    }

    #[test]
    fn kraken_truncates_to_subscribed_depth() {
        let side =
            |base: f64, step: f64| (0..KRAKEN_DEPTH).map(|i| json!({"price": base + step * i as f64, "qty": 1.0})).collect::<Vec<_>>();
        let snap = json!({"channel": "book", "type": "snapshot", "data": [{"bids": side(100.0, -0.1), "asks": side(101.0, 0.1)}]});
        let mut v = VenueBook::new(Venue::Kraken);
        v.apply(&snap.to_string(), 1).unwrap();
        let upd = json!({"channel": "book", "type": "update", "data": [{"bids": [{"price": 100.05, "qty": 2.0}], "asks": []}]});
        v.apply(&upd.to_string(), 2).unwrap();
        let s = v.snapshot().unwrap();
        assert_eq!((s.bids.len(), s.asks.len()), (KRAKEN_DEPTH, KRAKEN_DEPTH));
        assert_eq!(s.bids[0], (100.05, 2.0));
        assert!(s.bids.last().unwrap().0 > 100.0 - 0.1 * 999.0 + 0.05);
    }

    #[test]
    fn bitstamp_full_snapshots_replace() {
        let mut v = VenueBook::new(Venue::Bitstamp);
        let frames = [
            json!({"event": "bts:subscription_succeeded", "channel": "order_book_btcusd", "data": {}}),
            json!({"data": {"timestamp": "1795622400", "microtimestamp": "1795622400500000", "bids": [["100", "1"]], "asks": [["101", "2"]]},
                   "channel": "order_book_btcusd", "event": "data"}),
            json!({"data": {"timestamp": "1795622401", "microtimestamp": "1795622401000000", "bids": [["99", "1"], ["98", "0"]], "asks": [["100.5", "2"]]},
                   "channel": "order_book_btcusd", "event": "data"}),
        ];
        assert!(feed(&mut v, &frames).iter().all(Result::is_ok));
        let s = v.snapshot().unwrap();
        assert_eq!((s.bids.clone(), s.asks.clone()), (vec![(99.0, 1.0)], vec![(100.5, 2.0)]));
        assert_eq!(v.book.venue_ts_ns, 1_795_622_401_000_000_000);
        assert_eq!(v.book.snapshots, 2);
    }

    #[test]
    fn gemini_initial_changes_heartbeats_and_gap() {
        let ch = |side: &str, p: &str, r: &str, why: &str| json!({"type": "change", "side": side, "price": p, "remaining": r, "delta": r, "reason": why});
        let mut v = VenueBook::new(Venue::Gemini);
        let frames = [
            json!({"type": "update", "eventId": 1, "socket_sequence": 0, "events": [ch("bid", "100", "1", "initial"), ch("ask", "101", "1", "initial")]}),
            json!({"type": "heartbeat", "socket_sequence": 1}),
            json!({"type": "update", "eventId": 2, "timestamp": 1795622400, "timestampms": 1795622400250u64, "socket_sequence": 2,
                   "events": [ch("ask", "101", "0", "cancel"), ch("ask", "103", "2", "place"), {"type": "trade", "price": "101", "amount": "1"}]}),
        ];
        assert!(feed(&mut v, &frames).iter().all(Result::is_ok));
        assert_eq!(top(&v), Some((100.0, 103.0)));
        assert_eq!(v.book.venue_ts_ns, 1_795_622_400_250_000_000);
        assert_eq!(v.book.last_update_ns, 1002);
        let gap = json!({"type": "heartbeat", "socket_sequence": 4});
        assert_eq!(v.apply(&gap.to_string(), 9), Err(Integrity::SeqGap { expected: 3, got: 4 }));
        assert!(v.snapshot().is_none());
        // a change before the next initial book is ignored
        let late = json!({"type": "update", "socket_sequence": 5, "events": [ch("bid", "100", "1", "place")]});
        v.apply(&late.to_string(), 10).unwrap();
        assert!(v.snapshot().is_none());
    }

    #[test]
    fn cryptocom_snapshot_deltas_and_pu_gap() {
        let msg = |ch: &str, data: Value| {
            json!({"id": -1, "method": "subscribe", "code": 0,
                   "result": {"instrument_name": "BTC_USD", "subscription": "book.BTC_USD.150", "channel": ch, "depth": 150, "data": [data]}})
        };
        let mut v = VenueBook::new(Venue::Cryptocom);
        let frames = [
            json!({"id": 1, "method": "subscribe", "code": 0}),
            msg(
                "book",
                json!({"bids": [["100", "1", "1"], ["99", "2", "1"]], "asks": [["101", "1", "1"]], "t": 1795622400000u64, "tt": 1795622399990u64, "u": 10}),
            ),
            json!({"id": 1795622400100u64, "method": "public/heartbeat", "code": 0}),
            msg(
                "book.update",
                json!({"update": {"bids": [["100", "0", "0"]], "asks": [["102", "4", "1"]]}, "t": 1795622400200u64, "u": 12, "pu": 10, "cs": 0}),
            ),
        ];
        assert!(feed(&mut v, &frames).iter().all(Result::is_ok));
        let s = v.snapshot().unwrap();
        assert_eq!(s.bids, vec![(99.0, 2.0)]);
        assert_eq!(s.asks, vec![(101.0, 1.0), (102.0, 4.0)]);
        assert_eq!(v.book.venue_ts_ns, 1_795_622_400_200_000_000);
        let gap = msg("book.update", json!({"update": {"bids": [], "asks": []}, "u": 15, "pu": 13}));
        assert_eq!(v.apply(&gap.to_string(), 9), Err(Integrity::SeqGap { expected: 12, got: 13 }));
        assert!(v.snapshot().is_none());
        // the next full book restores it
        v.apply(&frames[1].to_string(), 10).unwrap();
        assert_eq!(top(&v), Some((100.0, 101.0)));
    }

    #[test]
    fn bullish_flat_arrays() {
        let mut v = VenueBook::new(Venue::Bullish);
        let snap = json!({"type": "snapshot", "dataType": "V1TALevel2",
                          "data": {"symbol": "BTCUSD", "bids": ["100.0", "1", "99.0", "2"], "asks": ["101.0", "3"],
                                   "timestamp": "1795622400028", "sequenceNumberRange": [5, 6]}});
        v.apply(&snap.to_string(), 3).unwrap();
        let s = v.snapshot().unwrap();
        assert_eq!(s.bids, vec![(100.0, 1.0), (99.0, 2.0)]);
        assert_eq!(s.asks, vec![(101.0, 3.0)]);
        assert_eq!(v.book.venue_ts_ns, 1_795_622_400_028_000_000);
        // non-book frames (keepalive pong) only refresh the receive stamp
        let pong =
            json!({"id": "0", "jsonrpc": "2.0", "result": {"responseCodeName": "OK", "responseCode": "200", "message": "Keep alive pong"}});
        v.apply(&pong.to_string(), 4).unwrap();
        assert_eq!(v.snapshot().unwrap().retrieved_ns, 4);
    }

    #[test]
    fn lmax_rest_books_and_failed_polls() {
        // the real body shape (2026-11-26), cut to two levels a side
        let body = r#"{"instrument_id":"btc-usd","timestamp":"2026-11-26T15:28:49.000Z","status":"OPEN","bids":[{"price":"60208.200000","quantity":"1.0000"},{"price":"60203.306000","quantity":"0.3300"}],"asks":[{"price":"60216.000000","quantity":"0.0800"},{"price":"60222.390000","quantity":"0.0400"}]}"#;
        let ok = json!({"status": 200, "sent_tw": 1, "sent_tm": 1});
        let mut v = VenueBook::new(Venue::Lmax);
        v.apply_record(body, 10, Some(&ok)).unwrap();
        let s = v.snapshot().unwrap();
        assert_eq!(s.bids, vec![(60208.2, 1.0), (60203.306, 0.33)]);
        assert_eq!(s.asks, vec![(60216.0, 0.08), (60222.39, 0.04)]);
        assert_eq!((s.retrieved_ns, v.book.venue_ts_ns), (10, iso_ns("2026-11-26T15:28:49Z").unwrap()));
        // a 429, an HTML error page with status 200, and a body missing a side all reset
        let fail: [(&str, Value); 3] = [
            (r#"{"error":"too many requests"}"#, json!({"status": 429, "sent_tw": 2, "sent_tm": 2})),
            ("<html><body>502 Bad Gateway</body></html>", ok.clone()),
            (r#"{"bids":[{"price":"1","quantity":"1"}]}"#, ok.clone()),
        ];
        for (raw, meta) in fail {
            v.apply_record(body, 20, Some(&ok)).unwrap();
            assert!(v.snapshot().is_some());
            v.apply_record(raw, 21, Some(&meta)).unwrap();
            assert!(v.snapshot().is_none(), "{raw}");
        }
        // an unparsable level is what PERELMAN's float() raises on
        v.apply_record(&body.replace("0.3300", "x"), 22, Some(&ok)).unwrap();
        assert!(v.snapshot().is_none());
    }

    #[test]
    fn ctl_records_and_garbage() {
        let mut v = VenueBook::new(Venue::Bitstamp);
        let f = json!({"event": "data", "data": {"bids": [["1", "1"]], "asks": [["2", "1"]], "microtimestamp": "1"}});
        v.apply_record(&f.to_string(), 1, None).unwrap();
        assert!(v.snapshot().is_some());
        v.apply_record("not json", 2, None).unwrap();
        assert!(v.snapshot().is_some());
        v.apply_record("", 3, Some(&json!({"ctl": "backpressure_drop", "count": 2}))).unwrap();
        assert!(v.snapshot().is_none());
        assert_eq!(Venue::from_name("cryptocom"), Some(Venue::Cryptocom));
        assert!(Venue::ALL.iter().all(|&x| Venue::from_name(x.name()) == Some(x)));
    }
}
