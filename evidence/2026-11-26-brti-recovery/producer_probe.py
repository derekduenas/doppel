"""Run the prospective seal protocol on two upstream hours in an empty Apex scratch directory."""

import argparse
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from doppel.lab import brti_import
from doppel.tape.receipts import ReceiptPending, build_receipt
from doppel.tape import receipts, segment
from doppel.twin import evaluate
from doppel.twin.evaluate import evaluation_inputs, verify_hours


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scratch", type=Path)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("archive", type=Path)
    a = parser.parse_args()
    root, incoming, state = a.scratch / "tape", a.scratch / "upstream", a.scratch / "state.json"
    root.mkdir()
    incoming.mkdir()
    hour = int(datetime(2026, 11, 26, 17, tzinfo=timezone.utc).timestamp())
    stream = "pmus_ws_5m"
    rel = "20261126/17.dtape.zst"
    dst = root / stream / rel
    dst.parent.mkdir(parents=True)
    shutil.copyfile(a.archive / stream / rel, dst)
    rows = [json.loads(line) for line in (a.archive / stream / "manifest.jsonl").read_bytes().splitlines()]
    seal = next(r for r in reversed(rows) if r["file"] == rel)
    (root / stream / "manifest.jsonl").write_text(json.dumps(seal) + "\n")
    sources = [a.upstream / f"2026-11-26T{hh}.jsonl.gz" for hh in (17, 18)]
    report = {"scratch": str(a.scratch), "upstream_sha256": {p.name: sha(p) for p in sources},
              "source_sha256": {m.__name__: sha(Path(m.__file__)) for m in (brti_import, receipts, segment, evaluate)},
              "probe_sha256": sha(Path(__file__))}
    (incoming / sources[0].name).symlink_to(sources[0])
    report["first_import"] = brti_import.run(incoming, root, state)
    try:
        build_receipt(root, "20261126", 17, require_brti_import=True)
    except ReceiptPending as exc:
        report["early_receipt"] = str(exc)
    else:
        raise AssertionError("the recorder published an early receipt")
    (incoming / sources[1].name).symlink_to(sources[1])
    report["second_import"] = brti_import.run(incoming, root, state)
    receipt = build_receipt(root, "20261126", 17, require_brti_import=True)
    report["receipt_schema"] = receipt["schema_version"]
    report["streams"] = {s: {k: v[k] for k in ("sha256", "bytes", "first_tw", "last_tw", "records", "crc_ok")}
                         for s, v in receipt["streams"].items()}
    path = root / "receipts/20261126/17.json"
    report["receipt_sha256"] = sha(path)
    report["retry"] = brti_import.run(incoming, root, state)
    assert report["retry"]["added"] == report["retry"]["amendments"] == 0
    assert build_receipt(root, "20261126", 17) == receipt and sha(path) == report["receipt_sha256"]
    verify_hours(root, ("5m",), hour, hour + 3600, evaluation_inputs(root, None))
    report["canonical_input_preflight"] = "passed"
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    report["checked_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
