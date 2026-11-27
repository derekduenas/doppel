"""Cross-check two controlled CLI runs directly against the private maker journals."""
import argparse
from collections import Counter
import hashlib
import json
from math import isclose
from pathlib import Path
from random import Random
from statistics import mean


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root: Path, repeat: Path):
    folder = Path(__file__).resolve().parent
    registration = (folder / "registration.json").read_bytes()
    reg = json.loads(registration)
    seal = json.loads((folder / "controlled-forecast-seal.json").read_bytes())
    require(hashlib.sha256(registration).hexdigest() == seal["registration_sha256"], "registration digest differs")
    output = {"forecast_seal_revision": "3d109e6", "splits": {}}
    for split, label in (("dev", "development"), ("eval", "controlled_holdout")):
        plan = reg["splits"][label]
        docs, hashes = {}, {}
        for kind in ("public", "predictions", "journal", "labels", "scores"):
            name = f"{split}-{kind}.json" + ("l" if kind == "journal" else "")
            raw = (root / name).read_bytes()
            require(raw == (repeat / name).read_bytes(), f"replay changed {name}")
            hashes[kind] = hashlib.sha256(raw).hexdigest()
            docs[kind] = [json.loads(line) for line in raw.splitlines()] if kind == "journal" else json.loads(raw)
        for kind in ("public", "predictions"):
            require(hashes[kind] == seal["splits"][split][kind + "_sha256"], f"unsealed {kind}")
        for kind in ("public", "predictions", "labels"):
            require(docs["scores"][kind + "_sha256"] == hashes[kind], "score contract differs")
        journal = {(r["wallet"], r["cutoff_s"]): r["order"] for r in docs["journal"]}
        subjects = plan["subjects"]
        times = range(plan["start"], plan["end"], 10)
        require(len(journal) == len(docs["journal"]) == len(times) * len(subjects), "journal denominator changed")
        cells = docs["public"]["cells"]
        require([c["cutoff_s"] for c in cells] == list(times), "public grid mismatch")
        coin = Random(plan["hidden_seed"])
        for c in cells:
            t = c["cutoff_s"]
            require((journal["sim-c", t] is not None) == bool(coin.getrandbits(1)), "private coin differs")
        results = {}
        for subject in subjects:
            labels = docs["labels"]["subjects"][subject]
            derived = [{"cutoff_s": t, "action": journal[subject,t] is not None} for t in times]
            require(labels == derived, "derived labels disagree with private journal")
            score = docs["scores"]["subjects"][subject]["action"]
            require([m["start"] for m in score["markets"]] == list(range(plan["start"], plan["end"], 300)),
                    "scored market population differs")
            votes = Counter()
            accuracy = {r: [] for r in ("touch", "queue_at_most", "never_bid")}
            for i, market in enumerate(score["markets"]):
                block = cells[30*i:30*(i+1)]
                hits, deep_hits = Counter(), Counter()
                n = deep = positive = 0
                for c in block:
                    t, book = c["cutoff_s"], c["book"]
                    if book is None or c["capture"] != "covered":
                        continue
                    posted = journal[subject,t]
                    if posted is not None:
                        require(posted == {"token": f"{t-t%300}-down", "side": "BUY",
                                           "limit_cents": round(book["bid"] * 100), "shares": 60}, "wrong quote terms")
                    shallow = book["bid_shares"] <= reg["queue_at_most_shares"]
                    require(subject != "sim-a" or posted is not None, "touch maker missed opportunity")
                    require(subject != "sim-b" or (posted is not None) == shallow, "queue maker decision differs")
                    active = posted is not None
                    n += 1
                    positive += active
                    deep += not shallow
                    hits.update({"touch": int(active), "queue_at_most": int(shallow == active), "never_bid": int(not active)})
                    if not shallow:
                        deep_hits.update({"touch": int(active), "queue_at_most": int(not active)})
                require(n == market["scored"] == 28 and deep == market["disagreements"] == 14, "incorrect market denominator")
                require(dict(hits) == market["correct"] and dict(deep_hits) == market["correct_on_disagreements"]
                        and positive == market["positive"], "incorrect market counts")
                for rule in accuracy:
                    accuracy[rule].append(hits[rule]/n)
                vote = deep_hits["touch"] - deep_hits["queue_at_most"]
                votes["touch" if vote > 0 else "queue_at_most" if vote < 0 else "ties"] += 1
            require(Counter(score["votes"]) == votes, "incorrect market votes")
            non_ties = votes["touch"] + votes["queue_at_most"]
            margin = abs(votes["touch"] - votes["queue_at_most"])
            extreme = sum(abs(2*j.bit_count()-non_ties) >= margin for j in range(2**non_ties))
            require(isclose(score["sign_test_two_sided_p"], extreme / 2**non_ties), "incorrect exact sign probability")
            require(all(isclose(mean(values), score["mean_market_accuracy"][r]) for r,values in accuracy.items()),
                    "incorrect mean-market accuracy")
            results[subject] = {key: score[key] for key in ("verdict", "winner", "counts", "votes",
                                "mean_market_accuracy", "mean_disagreement_difference_touch_minus_queue",
                                "sign_test_two_sided_p")}
        output["splits"][split] = {"byte_identical_artifacts_sha256": hashes,
                                   "verified_owner_decisions": len(journal), "subjects": results}
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", required=True, type=Path)
    parser.add_argument("--repeat", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.first, args.repeat), indent=2, sort_keys=True))
