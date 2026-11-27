"""Compare control coverage on discovery features without freezing a second cohort or reading returns."""

import argparse
import hashlib
from pathlib import Path

import orjson

from doppel.lab.scan import rank_members
from doppel.lab.selection_plan import _features, _match, validate_plan


def compare(scan_bytes, plan):
    validate_plan(plan)
    if hashlib.sha256(scan_bytes).hexdigest() != plan["scan_sha256"]:
        raise ValueError("discovery scan does not match registration")
    scan = orjson.loads(scan_bytes)
    comparisons = {}
    for design in ("lower-band", "interleaved"):
        _, candidates, controls = rank_members(scan, design=design)
        features = {a: _features(scan["wallets"][a]) for a in candidates + controls}
        matches = _match(candidates, controls, features)
        if design == "lower-band" and (
                candidates != plan["candidates"] or controls != plan["controls"] or matches != plan["matches"]):
            raise ValueError("could not reproduce v1 registration from its scan")
        comparable = [c for c in candidates if matches[c]["status"] == "matched"]
        comparisons[design] = {"candidates": len(candidates), "controls": len(controls),
                               "matched": len(comparable), "unmatched": len(candidates) - len(comparable),
                               "also_candidates_in_v1": len(set(candidates) & set(plan["candidates"]))}
    return {"scope": "Retrospective discovery-feature support only. No returns or new study.",
            "registered_plan_sha256": plan["sha256"], "scan_sha256": plan["scan_sha256"],
            "designs": comparisons}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    print(orjson.dumps(compare(args.scan.read_bytes(), orjson.loads(args.plan.read_bytes())),
                       option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
