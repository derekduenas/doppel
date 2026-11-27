"""Verify the complete recovered H1 input set and its enforced research-only status."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from doppel.twin import evaluate


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(recovery):
    report_path = recovery / "report.json"
    report = json.loads(report_path.read_bytes())
    tape = recovery / "tape"
    paths = evaluate.evaluation_inputs(tape, None)
    input_digest, stamps = evaluate.fingerprint(tape, paths)
    start, end = (int(datetime.fromisoformat(report[name]).timestamp()) for name in ("from_utc", "end_exclusive_utc"))
    evaluate.verify_hours(tape, ("5m",), start, end, paths)
    if not report["diagnostic_only"]:
        raise ValueError("this verification expects the registered diagnostic recovery case")
    first = next(x for x in report["prefixes"] if x["classification"] == "upstream_reconstructed_diagnostic")
    key = "ref_brti_raw/" + first["file"]
    # The manifest and complete selected set are identical. Visit the first restricted file
    # before the large unaffected book files when exercising the binding-only rejection.
    priority = {key: paths[key], **paths}
    try:
        evaluate.fingerprint(tape, priority, require_binding=True)
    except ValueError as exc:
        if "diagnostic_only" not in str(exc) or key not in str(exc):
            raise
        refusal = str(exc)
    else:
        raise ValueError("binding-only fingerprint accepted reconstructed input")
    if (evaluate.evaluation_inputs(tape, None) != paths
            or any(evaluate.stamp(path) != stamps[k] for k, path in paths.items())):
        raise ValueError("snapshot changed during validation")
    return {"verified_at_utc": datetime.now(UTC).isoformat(timespec="seconds"), "root": str(recovery),
            "verifier_sha256": digest(Path(evaluate.__file__)), "runner_sha256": digest(Path(__file__)),
            "recovery_report_sha256": digest(report_path), "input_sha256": input_digest,
            "files": len(paths), "hours": (end - start) // 3600,
            "classifications": dict(Counter(row["classification"] for row in report["prefixes"])),
            "upstream_matches": sum(v["matched_publications"] for v in report["upstream_files"].values()),
            "mode": "diagnostic only", "fingerprint": "passed", "hour_checks": "passed",
            "binding_rejection": refusal, "evaluated_strategy": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recovery", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.recovery), indent=2, sort_keys=True))
