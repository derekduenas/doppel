"""Freeze competing maker intentions from a public opportunity grid, then score independently acquired outcomes."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import math
from pathlib import Path
from statistics import mean

import orjson

from ..tape.segment import atomic_write

NS = 1_000_000_000
RULES = ("touch", "queue_at_most", "never_bid")


def read(path):
    raw = path.read_bytes()
    return orjson.loads(raw), hashlib.sha256(raw).hexdigest()


def encode(value):
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2) + b"\n"


def registered(plan, panel):
    if (plan["schema"] != "wallet-rival-registration/1" or panel["schema"] != "rival-public/1"
            or set(panel) != {"schema", "scope", "start", "end", "cells"}
            or plan["rivals"] != list(RULES[:2]) or plan["baseline"] != RULES[2]
            or (plan["asset"], plan["outcome"], plan["market_s"], plan["grid_s"]) != ("btc","Down",300,10)):
        raise ValueError("unexpected registration or public grid")
    match = [(key, split) for key, split in plan["splits"].items()
             if all(split[k] == panel[k] for k in ("start", "end", "scope"))]
    if len(match) != 1:
        raise ValueError("no unique registered whole-market split")
    name, split = match[0]
    start, end = split["start"], split["end"]
    if (any(type(t) is not int or t % plan["market_s"] for t in (start, end)) or start >= end
            or split["scope"] not in ("controlled", "real") or not split["subjects"]
            or len(split["subjects"]) != len(set(split["subjects"]))):
        raise ValueError("invalid registered interval or subjects")
    return name, split


def grid(rows, start, end, step):
    expected = range(start, end, step)
    if len(rows) != len(expected) or any(type(r.get("cutoff_s")) is not int or r["cutoff_s"] != t
                                        for r, t in zip(rows, expected)):
        raise ValueError("rows must cover every registered cutoff once in order")


def public_reason(book, cutoff_s, max_age):
    if book is None:
        return "missing_book"
    if not isinstance(book, dict) or set(book) != {"bid", "ask", "bid_shares", "depth_tw_ns","identity_tw_ns"}:
        raise ValueError("unexpected public book fields")
    cut = cutoff_s * NS
    for key in ("identity_tw_ns", "depth_tw_ns"):
        receipt = book[key]
        if type(receipt) is not int or not 0 < receipt < cut:
            return "missing_prior_" + key.removesuffix("_tw_ns")
    if cut - book["depth_tw_ns"] > max_age * NS:
        return "stale_depth"
    nums = [book[k] for k in ("bid", "ask", "bid_shares")]
    if (not all(type(x) in (float, int) and math.isfinite(x) for x in nums)
            or not 0 < nums[0] <= nums[1] < 1 or nums[2] <= 0):
        return "invalid_two_sided_quote"
    return None


def forecast(plan, plan_sha, public, public_sha):
    name, split = registered(plan, public)
    grid(public["cells"], split["start"], split["end"], plan["grid_s"])
    rows = []
    for cell in public["cells"]:
        if set(cell) != {"cutoff_s","book","capture"} or cell["capture"] not in ("covered","gap"):
            raise ValueError("unrecognized public cell")
        t, book = cell["cutoff_s"], cell["book"]
        reason = public_reason(book, t, plan["depth_max_age_s"])
        quote = None if reason else book["bid"]
        shallow = None if reason else book["bid_shares"] <= plan["queue_at_most_shares"]
        # The enclosing capture mask can exclude a score after the fact. It never changes a forecast.
        rows.append({"cutoff_s": t, "down_bid": quote, "touch": True if reason is None else None,
                     "queue_at_most": shallow, "never_bid": False if reason is None else None,
                     "score_exclusion": reason or ("capture_gap" if cell["capture"] == "gap" else None)})
    return {"schema":"rival-predictions/1","scope": split["scope"], "split":name, "start":split["start"],
            "end":split["end"],"plan_sha256":plan_sha, "public_sha256":public_sha,
            "order_when_true":{"side":"BUY", "outcome":"Down", "shares":plan["proposed_shares"], "limit":"down_bid"},
            "cells":rows}


def market_result(pairs, target, policy):
    counts = Counter()
    correct = Counter()
    discord_correct = Counter()
    for row, decision in pairs:
        counts["scheduled"] += 1
        if row["score_exclusion"] is not None:
            counts["public_excluded"] += 1
            continue
        counts["public_eligible"] += 1
        label = decision[target]
        if label is None:
            counts["unknown_target"] += 1
            continue
        counts["scored"] += 1
        counts["positive"] += int(label)
        for key in RULES:
            correct[key] += row[key] == label
        if row["touch"] != row["queue_at_most"]:
            counts["disagreements"] += 1
            for key in RULES[:2]:
                discord_correct[key] += row[key] == label
    fields = ("scheduled","public_excluded","public_eligible","unknown_target","scored","positive","disagreements")
    scored, disagreed = counts["scored"], counts["disagreements"]
    return {**{k: counts[k] for k in fields}, "accuracy":{k: correct[k]/scored if scored else None for k in RULES},
            "correct":{k:correct[k] for k in RULES}, "correct_on_disagreements":{k:discord_correct[k] for k in RULES[:2]},
            "disagreement_difference_touch_minus_queue":
                (discord_correct["touch"] - discord_correct["queue_at_most"])/disagreed if disagreed else None,
            "informative":scored >= policy["min_scored_cells_per_market"] and
                          disagreed >= policy["min_disagreement_cells_per_market"]}


def sign_test(positive, negative):
    n = positive + negative
    if n == 0:
        return None
    return min(1.0, 2 * sum(math.comb(n, k) for k in range(min(positive,negative)+1)) / (1 << n))


def evaluate(pred, labels, target, policy):
    width = policy["market_s"] // policy["grid_s"]
    cells = pred["cells"]
    markets = []
    for i in range(0, len(cells), width):
        m = market_result(zip(cells[i:i+width], labels[i:i+width]), target, policy)
        markets.append({"start":cells[i]["cutoff_s"], **m})
    informed = [m for m in markets if m["informative"]]
    diffs = [m["disagreement_difference_touch_minus_queue"] for m in informed]
    wins, losses = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    p = sign_test(wins, losses)
    delta = mean(diffs) if diffs else None
    acc = {k:mean(m["accuracy"][k] for m in informed) if informed else None for k in RULES}
    best = "touch" if delta is not None and delta > 0 else "queue_at_most" if delta is not None and delta < 0 else None
    supported = (best is not None and wins+losses >= policy["min_informative_markets"]
                 and p is not None and p <= policy["two_sided_sign_p_max"]
                 and delta is not None and abs(delta) >= policy["mean_disagreement_advantage_min"]
                 and acc[best] >= policy["mean_accuracy_min"]
                 and acc[best] - acc["never_bid"] >= policy["advantage_over_never_min"])
    winner = None
    if target == "execution":
        verdict = "execution_diagnostic_only"
    elif pred["scope"] == "real":
        verdict = "unidentified_no_owner_action_labels"
    elif pred["split"] != "controlled_holdout":
        verdict = "development_only"
    elif supported:
        winner, verdict = best, "recovered_among_registered_rivals"
    else:
        verdict = "abstain"
    fields = ("scheduled","public_eligible","public_excluded","scored","positive","unknown_target","disagreements")
    return {"target":target,"verdict":verdict,"winner":winner, "counts":{k:sum(m[k] for m in markets) for k in fields},
            "informative_markets":len(informed), "votes":{"touch":wins,"queue_at_most":losses,"ties":len(diffs)-wins-losses},
            "sign_test_two_sided_p":p, "mean_market_accuracy":acc, "mean_disagreement_difference_touch_minus_queue":delta,
            "markets":markets}


def score(plan, pred, pred_sha, results, label_sha):
    split = plan["splits"][pred["split"]]
    if (results["schema"] != "rival-labels/1" or results["public_sha256"] != pred["public_sha256"]
            or results["predictions_sha256"] != pred_sha or
            any(results[k] != split[k] for k in ("scope","start","end")) or
            set(results["subjects"]) != set(split["subjects"])):
        raise ValueError("labels do not belong to this registered population and prediction artifact")
    targets = ("action","execution") if split["scope"] == "real" else ("action",)
    wallets = {}
    for wallet in split["subjects"]:
        labels = results["subjects"][wallet]
        grid(labels, split["start"], split["end"], plan["grid_s"])
        for row in labels:
            if set(row) != {"cutoff_s", *targets} or any(row[k] is not None and type(row[k]) is not bool for k in targets):
                raise ValueError("target must be an explicit boolean or unknown")
            if split["scope"] == "real" and row["action"] is not None:
                raise ValueError("real public execution sources cannot certify an owner posting decision")
        wallets[wallet] = {k:evaluate(pred, labels, k, plan) for k in targets}
    return {"schema":"wallet-rival-comparison/1", "scope":pred["scope"], "split":pred["split"],
            "plan_sha256":pred["plan_sha256"], "public_sha256":pred["public_sha256"],
            "predictions_sha256":pred_sha, "labels_sha256":label_sha, "subjects":wallets,
            "public_exclusions":dict(Counter(r["score_exclusion"] for r in pred["cells"] if r["score_exclusion"])),
            "genome_emitted":False}


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    subs = cli.add_subparsers(dest="command", required=True)
    for mode in ("predict", "score"):
        sub = subs.add_parser(mode)
        for opt in ("plan", "public", "out"):
            sub.add_argument("--"+opt, type=Path, required=True)
        sub.add_argument("--plan-sha256", required=True)
        if mode == "score":
            sub.add_argument("--predictions", type=Path, required=True)
            sub.add_argument("--labels", type=Path, required=True)
    args = cli.parse_args(argv)
    plan, plan_sha = read(args.plan)
    if plan_sha != args.plan_sha256:
        raise ValueError("registration digest differs")
    public, public_sha = read(args.public)
    pred = forecast(plan, plan_sha, public, public_sha)
    out = pred
    if args.command == "score":
        observed, pred_sha = read(args.predictions)
        if observed != pred or pred_sha != hashlib.sha256(encode(pred)).hexdigest():
            raise ValueError("supplied forecast differs from the registered public replay")
        labels, label_sha = read(args.labels)
        out = score(plan, pred, pred_sha, labels, label_sha)
    data = encode(out)
    atomic_write(args.out, data, replace=False)
    print(hashlib.sha256(data).hexdigest(), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
