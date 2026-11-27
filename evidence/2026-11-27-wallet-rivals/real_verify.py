"""Verify the frozen real forecast and its scores using only the Python standard library."""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path


REGISTRATION = "ce5ac05f279af0588879e4054ca78d7145dec22de055958cff825e4b04ac6c44"
FORECAST_SEAL = "a2faa7f3093c5100022d5c3715ff7c54fbf5b56430ab750beee31df5ac5382a1"
NS = 10**9
RULES = ("touch", "queue_at_most", "never_bid")
COUNTS = ("scheduled", "public_excluded", "public_eligible", "unknown_target", "scored", "positive", "disagreements")


def check(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def same(expected, actual, path):
    """Compare exact counts and classes, allowing floating-point only for derived ratios."""
    if isinstance(expected, dict):
        check(isinstance(actual, dict) and actual.keys() == expected.keys(), f"fields differ at {path}")
        for name, value in expected.items():
            same(value, actual[name], f"{path}.{name}")
    elif isinstance(expected, list):
        check(isinstance(actual, list) and len(actual) == len(expected), f"row count differs at {path}")
        for i, (left, right) in enumerate(zip(expected, actual)):
            same(left, right, f"{path}[{i}]")
    elif isinstance(expected, float):
        check(type(actual) in (float, int) and math.isclose(actual, expected, rel_tol=0, abs_tol=1e-14),
              f"ratio differs at {path}")
    else:
        check(type(actual) is type(expected) and actual == expected, f"value differs at {path}")


def forecast_cell(cell, t):
    check(set(cell) == {"book", "cutoff_s", "capture"} and type(cell["cutoff_s"]) is int
          and cell["cutoff_s"] == t and cell["capture"] in ("covered", "gap"), "bad public cell")
    book = cell["book"]
    reason = None
    if book is None:
        reason = "missing_book"
    else:
        check(set(book) == {"identity_tw_ns", "depth_tw_ns", "bid", "ask", "bid_shares"}, "bad public fields")
        for witness in ("identity", "depth"):
            stamp = book[witness + "_tw_ns"]
            if type(stamp) is not int or not 0 < stamp < t * NS:
                reason = "missing_prior_" + witness
                break
        if reason is None:
            if t * NS - book["depth_tw_ns"] > 3 * NS:
                reason = "stale_depth"
            elif any(type(book[k]) not in (int, float) or not math.isfinite(book[k]) for k in ("bid", "ask", "bid_shares")
                     ) or not (0 < book["bid"] <= book["ask"] < 1 and book["bid_shares"] > 0):
                reason = "invalid_two_sided_quote"
    return {"cutoff_s": t, "touch": True if reason is None else None, "never_bid": False if reason is None else None,
            "down_bid": book["bid"] if reason is None and book is not None else None,
            "queue_at_most": book["bid_shares"] <= 1600 if reason is None and book is not None else None,
            "score_exclusion": reason or ("capture_gap" if cell["capture"] == "gap" else None)}


def market_table(cells, labels, target):
    # Four disjoint counts determine every accuracy. This calculation never consumes
    # the production scorer's predicted booleans or its market totals.
    quadrants = Counter()
    known = 0
    for cell, label in zip(cells, labels):
        if cell["score_exclusion"] is None:
            known += 1
            answer = label[target]
            if answer is not None:
                quadrants[cell["queue_at_most"], answer] += 1
    deep_yes, deep_no = quadrants[False, True], quadrants[False, False]
    shallow_yes, shallow_no = quadrants[True, True], quadrants[True, False]
    n = quadrants.total()
    d = deep_yes + deep_no
    positive = deep_yes + shallow_yes
    correct = {"touch": positive, "queue_at_most": shallow_yes + deep_no, "never_bid": n - positive}
    return {"start": cells[0]["cutoff_s"], "scheduled": len(cells), "public_excluded": len(cells)-known,
            "public_eligible": known, "unknown_target": known - n, "scored": n, "positive": positive, "disagreements": d,
            "accuracy": {key: value/n if n else None for key, value in correct.items()}, "correct": correct,
            "correct_on_disagreements": {"touch": deep_yes, "queue_at_most": deep_no},
            "disagreement_difference_touch_minus_queue": (deep_yes-deep_no)/d if d else None,
            "informative": n >= 20 and d >= 8}


def summarize(cells, labels, target):
    markets = [market_table(cells[i:i+30], labels[i:i+30], target) for i in range(0, len(cells), 30)]
    selected = [m for m in markets if m["informative"]]
    deltas = [Fraction(2*m["correct_on_disagreements"]["touch"], m["disagreements"])-1 for m in selected]
    up, down = sum(x > 0 for x in deltas), sum(x < 0 for x in deltas)
    signs = up + down
    # Enumerate every equally probable market sign pattern. Tied markets contribute no sign.
    tails = sum(abs(2*bits.bit_count() - signs) >= abs(up-down) for bits in range(1 << signs))
    probability = tails / (1 << signs) if signs else None
    means = {rule: float(sum(Fraction(m["correct"][rule], m["scored"]) for m in selected) / len(selected))
             if selected else None for rule in RULES}
    counts = {k: sum(m[k] for m in markets) for k in COUNTS}
    return {"target": target, "counts": counts, "markets": markets, "informative_markets": len(selected),
            "votes": {"touch": up, "queue_at_most": down, "ties": len(selected)-signs},
            "sign_test_two_sided_p": probability, "mean_market_accuracy": means,
            "mean_disagreement_difference_touch_minus_queue": float(sum(deltas)/len(deltas)) if deltas else None,
            "winner": None, "verdict": "unidentified_no_owner_action_labels" if target == "action" else "execution_diagnostic_only"}


def examples(cells, public, labels):
    out = {}
    for c, p, y in zip(cells, public, labels):
        if c["score_exclusion"] is not None:
            continue
        state = "unknown" if y["execution"] is None else "positive" if y["execution"] else "negative"
        category = ("agree_" if c["queue_at_most"] else "disagree_") + state
        out.setdefault(category, {"cutoff_s": c["cutoff_s"], "down_bid": c["down_bid"], "bid_shares": p["book"]["bid_shares"],
                                   "touch": True, "queue_at_most": c["queue_at_most"], "execution": y["execution"], "action": None})
    return out


def gate_measurements(result):
    """Report numerical checks separately from the real evidence restriction."""
    p = result["sign_test_two_sided_p"]
    d = result["mean_disagreement_difference_touch_minus_queue"]
    votes = result["votes"]
    acc = result["mean_market_accuracy"]
    best = None if d is None or d == 0 else "touch" if d > 0 else "queue_at_most"
    return {"at_least_eight_non_tied_markets": votes["touch"] + votes["queue_at_most"] >= 8,
            "sign_direction_agrees_with_mean": None if best is None else 2*votes[best] > votes["touch"]+votes["queue_at_most"],
            "two_sided_p_at_most_0.01": None if p is None else p <= 0.01,
            "mean_disagreement_advantage_at_least_0.20": None if d is None else abs(d) >= 0.2,
            "mean_accuracy_at_least_0.80": None if best is None else acc[best] >= 0.8,
            "mean_advantage_over_silence_at_least_0.10": None if best is None else acc[best] - acc["never_bid"] >= 0.1}


def source_check(wallet_sources, score, cells, labels):
    markets = score["markets"]
    check(len(wallet_sources) == len(markets), "missing source markets")
    for m in markets:
        source = wallet_sources["btc-updown-5m-" + str(m["start"])]
        check((source["positive_cells"], source["observable_negative_cells"], source["negative_censored_by_unlocated_order"],
               source["unobservable_book_cells"], source["missing_execution_target_cells"])
              == (m["positive"], m["scored"]-m["positive"], m["unknown_target"], m["public_excluded"],
                  m["unknown_target"]+m["public_excluded"]), "source denominator differs")
        first = source["first_positive_example"]
        if m["positive"] == 0:
            check(first is None, "unexpected positive source")
            continue
        # Check the published first positive against the cutoff book and half-open receipt interval.
        start_i = (m["start"] - cells[0]["cutoff_s"]) // 10
        idx = next(i for i in range(start_i, start_i+30) if labels[i]["execution"] is True)
        t = cells[idx]["cutoff_s"]
        check(first["cutoff_s"] == t and t*NS <= first["first_receipt_tw_ns"] < (t+10)*NS
              and first["signed_limit"] == cells[idx]["down_bid"] and first["signed_shares"] > 0
              and first["first_tx"] and first["order_id"], "positive receipt differs from label and bid")


def verify(args):
    reg, sha = read(Path(__file__).with_name("registration.json"))
    check(sha == REGISTRATION, "registration digest differs")
    seal, seal_sha = read(Path(__file__).with_name("real-forecast-seal.json"))
    check(seal_sha == FORECAST_SEAL and seal["registration_sha256"] == sha, "independent forecast seal differs")
    public, pub_sha = read(args.public)
    pred, pred_sha = read(args.predictions)
    check((pub_sha, pred_sha) == (seal["public_sha256"], seal["predictions_sha256"]), "forecast seal differs")
    check(args.public_sha256 in (None, pub_sha) and args.predictions_sha256 in (None, pred_sha), "caller seal differs")
    plan = reg["splits"]["real_holdout"]
    bounds = {"scope": "real", "start": plan["start"], "end": plan["end"]}
    times = range(plan["start"], plan["end"], 10)
    check(len(times) == 360 and len(public["cells"]) == len(times), "public denominator differs")
    same({"schema": "rival-public/1", **bounds, "cells": public["cells"]}, public, "public")
    cells = [forecast_cell(row, t) for row, t in zip(public["cells"], times)]
    same({"schema": "rival-predictions/1", **bounds, "split": "real_holdout", "plan_sha256": sha, "public_sha256": pub_sha,
          "order_when_true": {"side": "BUY", "outcome": "Down", "shares": 60, "limit": "down_bid"}, "cells": cells}, pred, "forecast")
    labels, label_sha = read(args.labels)
    hashes = {"plan_sha256": sha, "public_sha256": pub_sha, "predictions_sha256": pred_sha, "labels_sha256": label_sha}
    same({"schema": "rival-labels/1", **bounds, "public_sha256": pub_sha, "predictions_sha256": pred_sha,
          "subjects": labels["subjects"]}, labels, "labels")
    check(set(labels["subjects"]) == set(plan["subjects"]), "registered wallets differ")
    actual, score_sha = read(args.score)
    sources = source_sha = None
    if args.label_source:
        sources, source_sha = read(args.label_source)
        check(all(sources[k] == hashes[k] for k in ("public_sha256", "predictions_sha256", "labels_sha256"))
              and set(sources["wallets"]) == set(plan["subjects"]), "label source binding differs")
    expected, receipt = {}, {}
    for wallet, rows in labels["subjects"].items():
        check(len(rows) == len(times), "wallet grid length differs")
        for row, cell in zip(rows, cells):
            check(set(row) == {"action", "execution", "cutoff_s"} and row["action"] is None
                  and (row["execution"] is None or type(row["execution"]) is bool)
                  and type(row["cutoff_s"]) is int and row["cutoff_s"] == cell["cutoff_s"], "invalid wallet label")
            check(cell["score_exclusion"] is None or row["execution"] is None, "execution labelled on excluded book")
        expected[wallet] = {k: summarize(cells, rows, k) for k in ("action", "execution")}
        if sources is not None:
            source_check(sources["wallets"][wallet], expected[wallet]["execution"], cells, rows)
        receipt[wallet] = {k: {field: v for field, v in result.items() if field != "markets"}
                           for k, result in expected[wallet].items()}
        for target, result in expected[wallet].items():
            receipt[wallet][target]["registered_numeric_checks"] = gate_measurements(result)
        receipt[wallet]["first_cell_by_observed_category"] = examples(cells, public["cells"], rows)
    excluded = dict(Counter(c["score_exclusion"] for c in cells if c["score_exclusion"]))
    same({"schema": "wallet-rival-comparison/1", "scope": "real", "split": "real_holdout", **hashes,
          "subjects": expected, "public_exclusions": excluded, "genome_emitted": False}, actual, "score")
    return {"schema": "rival-real-verification/1", **hashes, "forecast_seal_sha256": seal_sha, "score_sha256": score_sha,
            "source_sha256": source_sha, "scheduled_markets": 12, "scheduled_cells": len(cells),
            "public_exclusions": excluded, "wallets": receipt}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("public", "predictions", "labels", "score"):
        parser.add_argument("--"+option, required=True, type=Path)
    parser.add_argument("--public-sha256")
    parser.add_argument("--predictions-sha256")
    parser.add_argument("--label-source", type=Path)
    print(json.dumps(verify(parser.parse_args()), indent=2, sort_keys=True))
