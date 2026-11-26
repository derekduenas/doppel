"""The Rust recorder (crates/doppel-tape, `doppel-taped`) writes segments the Python reader reads.

The fixture under tests/fixtures_rust_tape/ was produced by the Rust writer
(`DOPPEL_WRITE_FIXTURE=<dir> cargo test --release --test compat`); the same records are listed in
crates/doppel-tape/tests/compat.rs. Here they are checked field by field, and every record line is
checked byte for byte against what Python's own writer (orjson) would emit.
"""

import hashlib
import shutil
import zlib
from pathlib import Path

import orjson
import pytest

from doppel.tape.cli import verify
from doppel.tape.segment import SegmentWriter, _frames, read_segment

FIX = Path(__file__).parent / "fixtures_rust_tape"
SEG = FIX / "rust_s" / "20261125" / "16.dtape.zst"
T0 = 1795622400 * 10**9
TM0 = 123_456_789

pytestmark = pytest.mark.skipif(not SEG.exists(), reason="Rust tape fixture absent")

EXPECTED = [
    ("", {"ctl": "connect", "url": "wss://example.test/ws"}),
    ('{"a":1,"b":"x"}', None),
    ("q\"uote\\back\nnl\ttab\r\x01\x1f\x7f é 漢 😀   </script>", None),
    ('{"data":{"payload":[]}}', {"key": "KXBTC15M-26NOV251615-15", "status": 200, "sent_tw": T0,
                                 "sent_tm": TM0, "new": [[1795622400000, "97000.5"]], "f": 10.5,
                                 "neg": -3, "nested": {"z": [True, False, None]}}),
    ("", {"ctl": "backpressure_drop", "count": 3}),
    ("empty meta is omitted", None),
    ("", {"ctl": "seq_gap", "sid": 1, "channel": "orderbook_delta", "expected": 5, "got": 7}),
]


def test_records_exact():
    recs = list(read_segment(SEG, verify=True))
    assert len(recs) == len(EXPECTED)
    for i, (rec, (raw, meta)) in enumerate(zip(recs, EXPECTED)):
        assert rec["seq"] == i + 1
        assert rec["stream"] == "rust_s"
        assert rec["tw"] == T0 + i and rec["tm"] == TM0 + i
        assert rec["raw"] == raw
        assert rec["crc"] == zlib.crc32(raw.encode("utf-8"))
        assert rec.get("meta") == meta
        assert list(rec) == ["seq", "stream", "tw", "tm", "crc", "raw"] + (["meta"] if meta else [])


def test_lines_byte_identical_to_python_writer():
    frames = list(_frames(SEG))
    assert len(frames) >= 2
    lines = [ln for f in frames for ln in f.splitlines(keepends=True)]
    assert len(lines) == len(EXPECTED)
    for line in lines:
        assert line == orjson.dumps(orjson.loads(line)) + b"\n"


def test_manifest_and_python_tooling(tmp_path):
    man = orjson.loads((FIX / "rust_s" / "manifest.jsonl").read_bytes().splitlines()[0])
    assert list(man) == ["file", "sha256", "bytes", "records", "raw_bytes", "first_tw", "last_tw",
                         "last_seq", "closed_tw"]
    assert man["file"] == "20261125/16.dtape.zst"
    assert man["sha256"] == hashlib.sha256(SEG.read_bytes()).hexdigest()
    assert man["bytes"] == SEG.stat().st_size
    assert man["records"] == 7 and man["last_seq"] == 7
    assert man["raw_bytes"] == sum(len(r) for r, _ in EXPECTED)  # code points, as Python counts
    assert (man["first_tw"], man["last_tw"]) == (T0, T0 + 6)
    shutil.copytree(FIX / "rust_s", tmp_path / "rust_s")
    assert verify(tmp_path) == 0
    # a Python writer resumes the Rust stream's sequence numbers
    w = SegmentWriter(tmp_path, "rust_s")
    assert w.append("next", tw=T0 + 3600 * 10**9) == 8
    w.close()
