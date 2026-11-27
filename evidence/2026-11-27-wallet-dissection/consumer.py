"""Exercise the ordinary rulefit CLI on both registered wallets using hash-bound inputs."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

import orjson

from doppel.lab.goldsky_proof import digest
from doppel.lab.inspect_wallet import sealed_files
from doppel.lab.ordertape import committed_parts

PRIOR = Path(__file__).resolve().parent.parent / "2026-11-27-wallet-inspection"


def run(archive, destination):
    parent = archive / "research/wallet-inspection.jOPOHm/consumer-8c44ca1/match"
    signed = archive / "research/wallet-absorption.crsLww/first-orders"
    tokens = archive / "lab/tokens.jsonl"
    registered = orjson.loads((PRIOR / "consumer-first15.json").read_bytes())
    standard = orjson.loads((PRIOR / "standard-first15.json").read_bytes())
    files, seal = sealed_files(signed, [*committed_parts(signed), *signed.glob("_state/flush-*.json")])
    if files != standard["source_order_sha256"] or seal != standard["source_order_manifest_sha256"]:
        raise ValueError("signed source differs from Layer 2")
    match = {p.name: digest(p) for p in parent.iterdir()}
    if match != registered["match_output_sha256"] or digest(tokens) != registered["token_map_sha256"]:
        raise ValueError("order matches or token map differ from Layer 2")
    destination.mkdir()
    view = destination / "tape"
    source_sha = {}
    for stream in ("clob_btc_5m", "rtds_a", "rtds_b"):
        src = archive / "tape" / stream
        manifest = {r["file"]: r for line in (src/"manifest.jsonl").read_bytes().splitlines()
                    if (r := orjson.loads(line))}
        relative = "20261125/18.dtape.zst"
        path = src / relative
        sha = digest(path)
        if manifest[relative]["sha256"] != sha:
            raise ValueError(f"source differs from recorder seal: {stream}")
        target = view / stream / relative
        target.parent.mkdir(parents=True)
        target.symlink_to(path)
        source_sha[str(target.relative_to(view))] = sha
    docs = {}
    start, end = registered["window"]
    for wallet in registered["wallets"]:
        out = destination / wallet
        out.mkdir()
        args = [sys.executable, "-m", "doppel.lab.rulefit", "--data", str(view), "--orders", str(signed),
                "--wallet", wallet, "--from", str(start), "--to", str(end), "--ordermatch-dir", str(parent),
                "--tokens", str(tokens), "--max-candidates", "0", "--out", str(out)]
        with (out/"cli.log").open("wb") as log:
            proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT)
        report = orjson.loads((out/"fit_report.json").read_bytes())
        if (proc.returncode != 1 or (out/"genome.json").exists() or report["genome"]["registered"]
                or report["genome"]["status"] != "not_emitted" or not report["genome"]["reasons"]
                or report["wallet"] != wallet or report["inputs"]["streams"] != ["clob_btc_5m"]):
            raise ValueError(f"unexpected rulefit outcome for {wallet}")
        docs[wallet] = {"exit_code": proc.returncode, "genome": report["genome"], "label": report["label"],
                        "inputs": report["inputs"], "report_sha256": digest(out/"fit_report.json"),
                        "decision_rows_sha256": digest(out/"decision_table.jsonl")}
    return {"window": [start, end], "fit_scope": "BTC five-minute, one archived hour, first 15 minutes scored",
            "matched_source": match, "signed_source_sha256": seal, "token_map_sha256": digest(tokens),
            "sealed_tape": source_sha, "wallets": docs}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--archive", required=True, type=Path)
    cli.add_argument("--out", required=True, type=Path)
    a = cli.parse_args()
    print(orjson.dumps(run(a.archive, a.out), option=orjson.OPT_INDENT_2).decode())
