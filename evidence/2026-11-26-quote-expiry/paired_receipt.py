"""Compare the two archived paired replay arms and retain the gates and changing orders."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from doppel.lab import reconstruct


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def elapsed(root, name):
    values = {}
    for line in (root / f"{name}.time").read_text().splitlines():
        for label, key in (("Elapsed (wall clock) time (h:mm:ss or m:ss): ", "wall"),
                           ("Maximum resident set size (kbytes): ", "peak_rss_kib"),
                           ("Exit status: ", "exit")):
            if line.strip().startswith(label):
                value = line.strip().removeprefix(label)
                values[key] = value if key == "wall" else int(value)
    if len(values) != 3 or values["exit"]:
        raise ValueError(f"missing successful timing record for {name}")
    return values


def arm(root, name):
    path = root / name / "fresh"
    summary = path / "orderreplay/orderreplay.json"
    replay = path / "orderreplay/orderreplay.jsonl"
    doc = read(summary)
    rows = [json.loads(line) for line in replay.read_bytes().splitlines()]
    by_id = {row["id"]: row for row in rows}
    if len(rows) != len(by_id):
        raise ValueError(f"duplicate replay order in {replay}")
    by_outcome = defaultdict(Counter)
    for r in rows:
        counts = by_outcome[r["outcome"]]
        counts.update(orders=1, fills=r["fills"], delivered=r["fills_delivered"],
                      receipt_grace_deliveries=r.get("fills_delivered_during_grace", 0),
                      known_end=int(r["end_known"]), cut=int(r["cut"] is not None))
    return {
        "folder": name, "summary_sha256": sha(summary), "rows_sha256": sha(replay),
        "process": elapsed(root, name),
        "matched_rows_sha256": sha(path / "ordermatch/orders.jsonl"),
        "params": doc["params"], "counts": doc["counts"], "adapter_gate": doc["adapter_gate"],
        "bracket_violations": doc["bracket_violations"], "by_outcome": by_outcome,
        "clock_evidence": {k: v for k, v in doc.items() if any(t in k for t in ("clock", "grace", "lifetime"))},
    }, by_id


def compare(root, prefix):
    first, before = arm(root, f"{prefix}-control")
    second, after = arm(root, f"{prefix}-candidate")
    if first["matched_rows_sha256"] != second["matched_rows_sha256"]:
        raise ValueError(f"{prefix} arms received different ordermatch rows")
    changed = {}
    additions = {}
    for oid in sorted(before.keys() & after.keys()):
        old, new = before[oid], after[oid]
        diff = {k: {"control": old[k], "candidate": new[k]} for k in old.keys() & new.keys() if old[k] != new[k]}
        if diff:
            changed[oid] = diff
        additions[oid] = {k: new[k] for k in new.keys() - old.keys()}
    return {
        "control": first, "candidate": second,
        "control_only_orders": sorted(before.keys() - after.keys()),
        "candidate_only_orders": sorted(after.keys() - before.keys()),
        "changed_shared_fields": changed, "new_candidate_evidence": additions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("spec", type=Path)
    parser.add_argument("candidate_commit")
    args = parser.parse_args()
    root = args.root
    frozen = read(root / "new-spec/fresh-sample.json")
    if sha(args.spec) != sha(root / "new-spec/fresh-sample.json"):
        raise ValueError("holdout selection parameters differ from the frozen protocol")
    original = compare(root, "old")
    if original["control"]["rows_sha256"] != "b037cfa40ddd29c9503f87a6b11d5d826c4bd494457aa21b721e96b06a95ac04":
        raise ValueError("discovery control differs from the original baseline")
    doc = {
        "apex_root": str(root), "control_commit": "45ccb9244af2afe51407092df977a248fcdbc628",
        "candidate_commit": args.candidate_commit, "trial_spec_sha256": sha(args.spec),
        "protocol_commit": "b787e7af02a28c7493a3d7dcd6061b18631d26d0",
        "runner_sha256": sha(Path(__file__)), "selection_parameters": frozen,
        "selection_helper_sha256": sha(root / "new-spec/fresh_probe.py"),
        "match_process": elapsed(root, "new-match"),
        "replay_source_sha256": {arm: sha(root / arm / "src/doppel/lab/orderreplay.py")
                                 for arm in ("baseline", "candidate")},
        "orders": reconstruct.orders_digest(root / "new-control/orders", {}),
        "tape": reconstruct.tape_hashes(args.archive / "tape", [frozen["stream"]],
                                        frozen["warmup_from"], frozen["sample_to"], {}),
        "tokens_sha256": sha(args.archive / "lab/tokens.jsonl"),
        "discovery": original, "holdout": compare(root, "new"),
        "selection": read(root / "new-control/fresh/selection.json"),
        "identification": {"independently_verified_placement_and_expiry": 0,
                           "independent_chain_completeness": None},
        "reproduction": [
            "Extract the recorded source commits to baseline/ and candidate/. Export PYTHONPATH as the relevant src/.",
            "For old-control and old-candidate link fresh/ordermatch to the prior audit-validation.4OcRq1/final/fresh/ordermatch; link orders/ in every arm to the prior final/orders.",
            "For holdout selection copy baseline/evidence/2026-11-26-orderreplay/fresh_probe.py into new-spec/ alongside the frozen protocol as fresh-sample.json.",
            "Run /opt/doppel/.venv/bin/python new-spec/fresh_probe.py match ARCHIVE ROOT/new-control once on baseline source. Link new-candidate/fresh/ordermatch to the resulting new-control/fresh/ordermatch.",
            "Run the original fresh_probe.py for each discovery arm and new-spec/fresh_probe.py for each holdout arm, with arguments replay ARCHIVE ROOT/ARM. Runs used nice -n 15 ionice -c 3 timeout 300 with per-process /usr/bin/time -v -o ROOT/ARM.time.",
        ],
    }
    if not all(doc["tape"].values()):
        raise ValueError("missing sealed holdout tape segment")
    print(json.dumps(doc, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
