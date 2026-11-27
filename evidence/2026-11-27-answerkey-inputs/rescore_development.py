"""Regrade saved development output without running a subject, fitting rules or changing inputs."""

import argparse
import hashlib
import json
from pathlib import Path

from doppel.lab import answerkey


def load(path):
    return json.loads(path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rescore(base):
    source = base / "scorecard.json"
    card = load(source)
    rows = {}
    for item in card["strategies"].values():
        name = item["policy"]
        directory = base / name
        tools = directory / "tools"
        original = load(directory / "truth/truth.json")
        truth = original["summary"].copy()
        truth["skew_c_per_contract"] = original["params"].get("k_c_per_contract", None if name == "random_taker" else 0.0)
        evidence = {"exit": item["tools"]["exit"], "profile": load(tools / "profile" / f"{item['wallet']}.ledger.json"),
                    "rulefit": load(tools / "rulefit/fit_report.json")}
        for key, filename in [("fidelity", "fidelity/fidelity.json"), ("genome", "rulefit/genome.json")]:
            if (tools / filename).exists():
                evidence[key] = load(tools / filename)
        rows[name] = answerkey.score(name, truth, evidence)
    errors = [{"subject": subject, "cell": obligation, "truth": cell["truth"], "recovered": cell["recovered"]}
              for subject, row in rows.items() for obligation, cell in row.items() if cell["category"] == "wrongly_confident"]
    return {"development_run": base.name, "source_scorecard": f"{base.name}/scorecard.json", "source_scorecard_sha256": digest(source),
            "range": card["range"], "seed": card["seed"], "old_totals": card["totals"],
            "regraded_totals": answerkey.totals(rows), "confidence_errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    args = parser.parse_args()
    report = {"phase": "Historical synthetic development receipts regraded without refitting",
              "scorer_sha256": digest(Path(answerkey.__file__)), "runs": [rescore(p) for p in args.run_dirs]}
    print(json.dumps(report, indent=2))
