"""Verify the registered historical cohort freeze and publish its two immutable slices."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

import orjson

from doppel.lab.scan import scan_digest
from doppel.tape.segment import read_segment

NS = 1_000_000_000
CUTOFF = int(datetime(2026, 11, 28, tzinfo=timezone.utc).timestamp()) * NS
V1_SHA256 = "1775c0998506abf721e27ed3cb0043b5816b2dde587462034e55a4622a396b54"
TOKENS_SHA256 = "72f6055444f589d317fb24716b0dc9bb222a6d63b00fbc540ec82534abac0875"
BUILDER_SHA256 = "f8f437c8d66efd84be34b344927ad3820c3d8d2eeffa41322fa8303d2fd77d40"  # d1eb412
# 9146a49: perf-only BestQuotes change (exact; 5.43M real-tape touches checked equal). Same rules, same output.
BUILDER_SHA256S = frozenset({BUILDER_SHA256, "3c84d5fddc2b9729de31f4c3e205f1215bd1eea414987ac477bad16feb9d89b2"})
HEX = re.compile(r"[0-9a-f]{64}")
WALLET = re.compile(r"0x[0-9a-f]{40}")
SEGMENT = re.compile(r"([a-z0-9_]+)/([0-9]{8})/([0-9]{2})\.dtape\.zst")


def file_sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def check_timestamp(ns, hour, cutoff):
    return type(ns) is int and hour <= ns < min(hour + 3600 * NS, cutoff)


def verify_inputs(tape, sources):
    last = None
    receipts = {}
    count = 0
    for family in ("rtds", "clob"):
        entries = sources[family]
        if not isinstance(entries, dict) or not entries:
            raise ValueError(f"empty {family} input manifest")
        manifests = {}
        for relative, digest in sorted(entries.items()):
            match = SEGMENT.fullmatch(relative)
            if not match or not isinstance(digest, str) or not HEX.fullmatch(digest):
                raise ValueError("invalid cohort segment manifest entry")
            stream, day, hh = match.groups()
            if (family == "rtds" and stream not in ("rtds_a", "rtds_b")) or (
                    family == "clob" and not stream.startswith("clob_")):
                raise ValueError("cohort stream is outside its declared family")
            hour = int(datetime.strptime(day + hh, "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp()) * NS
            if hour + 3600 * NS > CUTOFF:
                raise ValueError(f"segment crosses or follows the registered freeze: {relative}")

            path = tape / relative
            stat = path.stat()
            if stream not in manifests:
                lines = (tape / stream / "manifest.jsonl").read_bytes().splitlines()
                manifests[stream] = {row["file"]: row for row in map(orjson.loads, lines)}
            seal = manifests[stream].get(f"{day}/{hh}.dtape.zst", {})
            if (seal.get("sha256") != digest or seal.get("bytes") != stat.st_size
                    or seal.get("evidence_use") == "diagnostic_only" or file_sha(path) != digest):
                raise ValueError(f"unsealed or hash-mismatched source: {relative}")

            receipt_path = tape / "receipts" / day / (hh + ".json")
            if receipt_path.is_file():
                raw = receipt_path.read_bytes()
                receipt = orjson.loads(raw)
                row = receipt.get("streams", {}).get(stream, {})
                if (receipt.get("day") != day or receipt.get("hour") != int(hh)
                        or row.get("crc_ok") is not True or row.get("bytes") != stat.st_size
                        or row.get("sha256", digest) != digest):
                    raise ValueError(f"source differs from its hourly receipt: {relative}")
                receipts[f"{day}/{hh}.json"] = hashlib.sha256(raw).hexdigest()

            previous = None
            for record in read_segment(path, strict=True):
                tw = record["tw"]
                if record.get("stream") != stream or not check_timestamp(tw, hour, CUTOFF) or (
                        previous is not None and tw < previous):
                    raise ValueError(f"record lies outside the sealed receive-time interval: {relative}")
                previous = tw
                last = tw if last is None else max(tw, last)
                count += 1
            if previous is None or (seal.get("last_tw") is not None and seal["last_tw"] != previous):
                raise ValueError(f"receipt-time seal does not match the tape: {relative}")
            after = path.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError(f"source changed during verification: {relative}")

    return {"last_native_receive_ns": last, "records": count,
            "segment_count": len(sources["rtds"]) + len(sources["clob"]),
            "segment_inventory_sha256": scan_digest({k: sources[k] for k in ("rtds", "clob")}),
            "hourly_receipt_count": len(receipts), "hourly_receipt_inventory_sha256": scan_digest(receipts)}


def seal(tape, manifest, stats_file, tokens, cohort_v1, destination):
    manifest_bytes, stats_bytes = manifest.read_bytes(), stats_file.read_bytes()
    doc, stats = orjson.loads(manifest_bytes), orjson.loads(stats_bytes)
    body = {k: v for k, v in doc.items() if k != "sha256"}
    scope = doc["period_a"]
    if (doc["schema"] != "doppel-cohort-v2/1" or stats["schema"] != "doppel-cohort-v2/1/stats"
            or doc["sha256"] != scan_digest(body) or doc["stats_sha256"] != scan_digest(stats)
            or doc["frozen_tw"] != CUTOFF or stats["frozen_tw"] != CUTOFF or scope["until_tw"] != CUTOFF
            or not scope["from_tw"] < scope["split_tw"] < CUTOFF or doc["code_sha256"] not in BUILDER_SHA256S):
        raise ValueError("cohort artifacts do not match the pinned builder, bounds and digests")
    sources = doc["inputs"]
    if (sources.get("tokens") != TOKENS_SHA256 or file_sha(tokens) != TOKENS_SHA256
            or sources.get("cohort_v1") != V1_SHA256 or file_sha(cohort_v1) != V1_SHA256):
        raise ValueError("cohort-v1 or the pre-cutoff token map differs from its pinned bytes")
    wallets = []
    for group in ("sniper", "donor", "controls"):
        members = doc[group]
        if not isinstance(members, list) or not all(isinstance(w, str) and WALLET.fullmatch(w) for w in members):
            raise ValueError(f"invalid {group} membership")
        if group != "controls" and any(stats["wallets"].get(w, {}).get("slice") != group for w in members):
            raise ValueError(f"membership differs from stats for {group}")
        wallets.extend(members)
    if len(wallets) != len(set(wallets)):
        raise ValueError("duplicate or overlapping cohort members")

    evidence = verify_inputs(tape, sources)
    receipt = {
        "schema": "doppel-cohort-v2-freeze/1", "cutoff_ns_exclusive": CUTOFF,
        "period_a": scope, "builder_sha256": doc["code_sha256"],
        "cohort_v1_sha256": V1_SHA256, "token_map_sha256": TOKENS_SHA256,
        "frozen_manifest_file_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "frozen_stats_file_sha256": hashlib.sha256(stats_bytes).hexdigest(),
        "inputs": evidence, "slices": {},
    }
    destination.mkdir(parents=True, exist_ok=False)
    for role, label in (("sniper", "snipers"), ("donor", "donors")):
        name = f"cohort-v2-{label}.json"
        card = {"schema": "doppel-cohort-v2-slice/1", "role": role, "cutoff_ns_exclusive": CUTOFF,
                "cohort_v2_sha256": doc["sha256"], "eligible": doc["eligible"], "shortfall": doc["shortfall"],
                "null": doc["null"], "wallets": doc[role], "controls": doc["controls"]}
        raw = orjson.dumps(card, option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE)
        with (destination / name).open("xb") as handle:
            handle.write(raw)
        receipt["slices"][name] = {"members": len(doc[role]), "sha256": hashlib.sha256(raw).hexdigest()}
    with (destination / "freeze-receipt.json").open("xb") as handle:
        handle.write(orjson.dumps(receipt, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("tape", "manifest", "stats", "tokens", "cohort-v1", "out"):
        parser.add_argument("--" + arg, type=Path, required=True)
    a = parser.parse_args(argv)
    try:
        seal(a.tape, a.manifest, a.stats, a.tokens, a.cohort_v1, a.out)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, f"cohort freeze rejected: {exc}\n")
    print(file_sha(a.out / "freeze-receipt.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
