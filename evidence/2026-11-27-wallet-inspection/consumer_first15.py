"""Offline Layer 2 acceptance at revision 8c44ca1f471387f1210b511c6110302202e0915f.

On Apex, make a fresh directory under the wallet-inspection research folder. Extract
src and evidence/2026-11-27-wallet-inspection/standard-first15.json from the pinned
revision into it, copy this script there, then run from that directory:

    systemd-run --unit=doppel-consumer-first15-8c44ca1 --property=MemoryMax=1G \
      --property=MemorySwapMax=0 --property=Nice=19 --property=IOSchedulingClass=idle \
      --property=PrivateNetwork=yes --setenv=PYTHONPATH="$PWD/src" \
      /opt/doppel/.venv/bin/python "$PWD/consumer_first15.py"
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import orjson

BASE = Path(__file__).resolve().parent
ARCHIVE = Path("/var/backups/doppel-sealed-20261126")
ORDERS = ARCHIVE / "research/wallet-absorption.crsLww/first-orders"
TOKENS = ARCHIVE / "lab/tokens.jsonl"
STREAM = "clob_btc_5m"
PERIOD = (1795629600, 1795630500)
EXPECTED = BASE / "evidence/2026-11-27-wallet-inspection/standard-first15.json"
MATCH = BASE / "match"
VIEW = BASE / "tape"
RELEASE = "8c44ca1f471387f1210b511c6110302202e0915f"


def sha(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def write(path: Path, obj) -> None:
    path.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2) + b"\n")


def match() -> None:
    from doppel.lab import ordermatch
    from doppel.lab.inspect_wallet import sealed_files
    from doppel.lab.ordertape import committed_parts

    assert Path(ordermatch.__file__).resolve().is_relative_to(BASE / "src")
    expected = orjson.loads(EXPECTED.read_bytes())
    assert sha(EXPECTED) == "f3b4e8b28055c4f532652db2c0b57adc432b28b118697890e7246cdab5b2511d"
    files, seal = sealed_files(ORDERS, [*committed_parts(ORDERS), *ORDERS.glob("_state/flush-*.json")])
    assert files == expected["source_order_sha256"] and seal == expected["source_order_manifest_sha256"]
    for relative, digest in expected["source_segment_sha256"].items():
        origin = ARCHIVE / "tape" / relative
        assert sha(origin) == digest
        # Expose only the registered hour. Rulefit can request pre-window warm-up
        # and additional RTDS discovery in the default archive root.
        visible = VIEW / relative
        visible.parent.mkdir(parents=True, exist_ok=True)
        visible.symlink_to(origin)
    wallets = list(expected["wallet_scope"])
    doc, rows, quotes = ordermatch.run(VIEW, ORDERS, *PERIOD, wallets, streams=[STREAM],
                                      min_fp_orders=10**9, focus=True)
    assert doc == expected["report"] and len(rows) == expected["representative_rows"] and not quotes
    MATCH.mkdir()
    write(MATCH / "ordermatch.json", doc)
    (MATCH / "orders.jsonl").write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
    (MATCH / "quotes.jsonl").write_bytes(b"")


def main() -> None:
    assert sha(TOKENS) == "72f6055444f589d317fb24716b0dc9bb222a6d63b00fbc540ec82534abac0875"
    subprocess.run([sys.executable, __file__, "match"], check=True)
    doc = orjson.loads((MATCH / "ordermatch.json").read_bytes())
    result = {"revision": RELEASE, "expected_match_sha256": sha(EXPECTED), "match_aggregate_equal": True,
              "match_output_sha256": {p.name: sha(p) for p in sorted(MATCH.iterdir())},
              "window": PERIOD, "stream": STREAM, "token_map_sha256": sha(TOKENS),
              "limits": {"MemoryMax": "1G", "MemorySwapMax": 0, "Nice": 19, "IOSchedulingClass": "idle",
                         "PrivateNetwork": True}, "wallets": {}}
    for wallet in doc["wallets"]:
        dest = BASE / f"rulefit-{wallet}"
        dest.mkdir()
        argv = [sys.executable, "-m", "doppel.lab.rulefit", "--data", str(VIEW), "--ordermatch-dir", str(MATCH),
                "--orders", str(ORDERS), "--wallet", wallet, "--from", str(PERIOD[0]), "--to", str(PERIOD[1]),
                "--tokens", str(TOKENS), "--max-candidates", "0", "--out", str(dest)]
        with (dest / "cli.log").open("wb") as log:
            ret = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
        report = orjson.loads((dest / "fit_report.json").read_bytes())
        inputs = report["inputs"]
        decision_rows = sum(1 for line in (dest / "decision_table.jsonl").read_bytes().splitlines() if line)
        result["wallets"][wallet] = {
            "exit_code": ret.returncode, "fit_label": report.get("label"), "genome_file": (dest / "genome.json").exists(),
            "genome_status": report["genome"]["status"], "genome_registered": report["genome"]["registered"],
            "reasons": report["genome"]["reasons"], "fit_counts": inputs["counts"], "fill_counts": {
                k: inputs["fills"].get(k) for k in ("n", "unmapped", "timed_by_block")},
            "rows_orders": inputs["rows_orders"], "rows_candidates": inputs["rows_candidates"],
            "decision_table_rows": decision_rows, "streams": inputs["streams"],
            "fit_report_sha256": sha(dest / "fit_report.json"), "decision_table_sha256": sha(dest / "decision_table.jsonl"),
        }
        assert report["wallet"] == wallet and (inputs["from"], inputs["to"]) == PERIOD
    write(BASE / "consumer-first15.json", result)
    for wallet in result["wallets"].values():
        assert (wallet["exit_code"] == 1 and wallet["rows_candidates"] == 0 and wallet["reasons"]
                and wallet["genome_status"] == "not_emitted" and not wallet["genome_registered"]
                and not wallet["genome_file"])


if __name__ == "__main__":
    match() if sys.argv[1:] == ["match"] else main()
