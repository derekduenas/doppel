import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "ops/stage_perelman_offshore.py"


def test_stage_exact_offshore_rows_and_skip_open_hour(tmp_path):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    selected = [b'{"kind": "bn", "recv": 2}\n', b'{"kind":"hl","recv":3}\n',
                b'{"kind":"feed_error","venue":"hyperliquid","at":4}\n']
    archived = source / "2026-11-26T12.jsonl.gz"
    archived.write_bytes(gzip.compress(b'{"kind":"brti","recv":1}\n' + b"".join(selected)
                                       + b'{"kind":"feed_error","venue":"kraken","at":5}\n'))
    open_hour = source / "2026-11-26T13.jsonl.gz"
    open_hour.write_bytes(b"unsealed")
    for path in (archived, open_hour):
        os.utime(path, (1, 1))

    args = [sys.executable, str(SCRIPT), "--source", str(source), "--out", str(output),
            "--through", "2026-11-26T13"]
    dry = subprocess.run([*args, "--dry-run"], check=True, capture_output=True)
    assert json.loads(dry.stdout)["confirmed_hours"] == 1
    assert not output.exists()
    subprocess.run(args, check=True, capture_output=True)

    staged = output / archived.name
    assert gzip.decompress(staged.read_bytes()) == b"".join(selected)
    assert not (output / open_hour.name).exists()
    assert (output / "upload-paths.txt").read_text().splitlines() == [archived.name, "offshore-manifest.json"]
    doc = json.loads((output / "offshore-manifest.json").read_bytes())["hours"][0]
    assert doc["source"]["sha256"] == hashlib.sha256(archived.read_bytes()).hexdigest()
    assert doc["offshore"]["sha256"] == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert doc["offshore"]["rows"] == {"binance/bn": 1, "hyperliquid/hl": 1, "hyperliquid/feed_error": 1}
