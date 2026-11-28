"""Two-phase answer-key exam for causal taker entry timing."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import random
import resource
import statistics
import sys
from tempfile import TemporaryDirectory
from typing import TypeGuard

from ..tape.segment import read_segment
from . import taker_entries


NS = 1_000_000_000
TOLERANCE_NS = NS
MAX_AGE_NS = 3 * NS
MAX_RSS_BYTES = 1_500_000_000
FEE = 0.02085
MIN_EVENTS = 20
MIN_MARKETS = 10
MIN_PRECISION = MIN_RECALL = 0.90
SUBJECTS = {"scripted_lead_taker": "lead", "scripted_fair_taker": "fair", "random_taker_control": None}
RECORDING_STREAMS = {
    "books": {"pmus_ws_5m", "pmus_ws_15m", "clob_btc_5m", "clob_btc_15m"},
    "brti": {"ref_brti_raw", "ref_brti_live"},
    "binance": {f"lead_binance_{kind}_{channel}" for kind in ("spot", "perp") for channel in ("l2", "trades")},
    "hyperliquid": {"lead_hyperliquid_l2", "lead_hyperliquid_trades"},
}
PUBLIC = ("training-public.jsonl", "holdout-public.jsonl")


def peak_rss_bytes() -> int:
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def digest_text(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"invalid SHA-256 for {name}")
    return value


def verified(path: Path, expected: object) -> str:
    checksum = digest_text(expected, path.name)
    if sha256(path) != checksum:
        raise ValueError(f"SHA-256 mismatch: {path.name}")
    return checksum


def rows(path: Path) -> Iterator[dict]:
    with path.open("rb") as source:
        for line in source:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected a JSON object in {path.name}")
            yield row


def write_rows(path: Path, items: Iterable[dict]) -> None:
    with path.open("x", encoding="utf-8") as output:
        for item in items:
            print(json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False), file=output)


def load_json(path: Path) -> dict:
    out = json.loads(path.read_bytes())
    if not isinstance(out, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return out


def under(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("receipt paths must be nonempty")
    relative = Path(value)
    dest = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not dest.is_relative_to(root.resolve()):
        raise ValueError("receipt paths must stay beneath the input directory")
    return dest


def bound(value: object) -> tuple[int, int]:
    if (not isinstance(value, list) or len(value) != 2
            or any(type(clock) is not int or clock <= 0 for clock in value) or not value[0] < value[1]):
        raise ValueError("range must be two positive integer Unix-nanosecond bounds")
    return value[0], value[1]


def native_coverage(role: str, source: Path, description: dict) -> tuple[int, int]:
    expected = description.get("stream")
    if expected not in RECORDING_STREAMS[role] or not source.name.endswith(".dtape.zst"):
        raise ValueError(f"invalid native stream for {role}")
    lo, hi = bound(description.get("covered_range_ns"))
    first: int | None = None
    last = 0
    prev_seq = 0
    for record in read_segment(source, verify=True, strict=True):
        tw, seq = record.get("tw"), record.get("seq")
        if (record.get("stream") != expected or type(tw) is not int
                or tw <= 0 or type(seq) is not int or seq <= prev_seq):
            raise ValueError(f"native tape record stream, clock or sequence mismatch: {source.name}")
        prev_seq = seq
        if record["raw"]:
            first = tw if first is None else min(first, tw)
            last = max(last, tw)
    if first is None or first > lo + MAX_AGE_NS or last < hi - MAX_AGE_NS:
        raise ValueError(f"native tape does not span its declared coverage: {source.name}")
    return lo, hi


def source_digests(plan: dict, inputs: Path, ranges: tuple[tuple[int, int], ...]) -> dict[str, str]:
    references = plan.get("source_receipts")
    if plan["source_kind"] == "synthetic":
        if references != {}:
            raise ValueError("synthetic plans cannot advertise recorded receipts")
        return {}
    producer = plan.get("feature_producer")
    if not isinstance(producer, dict) or not isinstance(producer.get("revision"), str) or not producer["revision"]:
        raise ValueError("recorded plans require a bound feature producer")
    verified(under(inputs, producer.get("path")), producer.get("sha256"))
    if not isinstance(references, dict) or set(references) != set(RECORDING_STREAMS):
        raise ValueError("recorded plans require books, BRTI, Binance and Hyperliquid receipts")
    used_segments = set()
    result = {}
    for role, reference in references.items():
        path = under(inputs, reference["path"])
        result[role] = verified(path, reference["sha256"])
        receipt = load_json(path)
        segments = receipt.get("segments")
        if (receipt.get("schema") != "doppel.layer5b-source/v1" or receipt.get("role") != role
                or receipt.get("clock") != "local_receive_ns" or not isinstance(segments, list) or not segments):
            raise ValueError(f"invalid recorded source receipt: {role}")
        intervals = []
        for segment in segments:
            segment_path = under(inputs, segment["path"])
            checksum = verified(segment_path, segment["sha256"])
            if checksum in used_segments:
                raise ValueError("native source bytes reused across receipt entries")
            used_segments.add(checksum)
            intervals.append(native_coverage(role, segment_path, segment))
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1] = merged[-1][0], max(merged[-1][1], hi)
            else:
                merged.append((lo, hi))
        if not all(any(lo <= start and end <= hi for lo, hi in merged) for start, end in ranges):
            raise ValueError(f"recorded source receipt does not cover the complete exam: {role}")
    return result


def inspect_panel(path: Path, scope: tuple[int, int]) -> set[str]:
    markets = set()
    last = (0, "")
    for item in rows(path):
        stamp, market = event_key(item)
        if (stamp, market) <= last or not scope[0] <= stamp < scope[1]:
            raise ValueError(f"public rows out of order or outside their registered interval: {path.name}")
        markets.add(market)
        last = stamp, market
    if not markets:
        raise ValueError(f"empty public panel: {path.name}")
    return markets


def load_plan(path: Path, expected_sha: str, inputs: Path) -> tuple[dict, dict[str, str]]:
    verified(path, expected_sha)
    plan = load_json(path)
    if plan.get("schema") != "doppel.layer5b-plan/v1" or plan.get("source_kind") not in {"synthetic", "recorded"}:
        raise ValueError("unknown Layer 5b plan schema or source kind")
    train, holdout = bound(plan.get("training_range_ns")), bound(plan.get("holdout_range_ns"))
    if train[1] > holdout[0]:
        raise ValueError("training must close before holdout opens")
    probes = plan.get("probe_cutoffs_ns")
    if (not isinstance(probes, list) or len(probes) != 3
            or any(type(p) is not int or not holdout[0] < p < holdout[1] for p in probes)
            or probes != sorted(set(probes))):
        raise ValueError("plan requires three distinct ordered interior holdout probes")
    public_hashes = plan.get("public_sha256")
    if not isinstance(public_hashes, dict) or set(public_hashes) != set(PUBLIC):
        raise ValueError("public panels must be bound by the plan")
    markets = []
    for name, scope in zip(PUBLIC, (train, holdout), strict=True):
        path = inputs / name
        verified(path, public_hashes[name])
        markets.append(inspect_panel(path, scope))
    if markets[0] & markets[1]:
        raise ValueError("training and holdout markets overlap")
    return plan, source_digests(plan, inputs, (train, holdout))


def event_key(row: dict) -> tuple[int, str]:
    ts_ns, market = row.get("ts_ns"), row.get("market_id")
    if type(ts_ns) is not int or ts_ns <= 0 or not isinstance(market, str) or not market:
        raise ValueError("invalid event timestamp or market")
    return ts_ns, market


def prior(clock: object, decision: int) -> bool:
    return type(clock) is int and 0 < clock < decision and decision - clock <= MAX_AGE_NS


def probability(v: object) -> TypeGuard[int | float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1


def observed(row: dict, name: str, decision: int) -> dict | None:
    value = row.get(name)
    if not isinstance(value, dict) or not prior(value.get("tw_ns"), decision):
        return None
    return value


def oracle_probability(row: dict, name: str, decision: int) -> float | None:
    value = observed(row, name, decision)
    if value is None or not probability(value.get("p_up")):
        return None
    if name == "lead" and any(
        not prior(value.get(f"{leg}_tw_ns"), decision) or value[f"{leg}_tw_ns"] > value["tw_ns"]
        for leg in ("binance", "hyperliquid")
    ):
        return None
    return float(value["p_up"])


def oracle_price(row: dict, decision: int) -> dict | None:
    book = observed(row, "book", decision)
    if book is None:
        return None
    for side in ("up", "down"):
        bid, ask = book.get(f"{side}_bid"), book.get(f"{side}_ask")
        if not probability(bid) or not probability(ask) or not 0 < bid <= ask < 1:
            return None
    return book


def script_side(book: dict, chance_up: float, *, fair: bool) -> str | None:
    threshold = 0.03 if fair else 0.02
    best: str | None = None
    best_edge = -1.0
    for outcome, chance in (("up", chance_up), ("down", 1 - chance_up)):
        ask = book[f"{outcome}_ask"]
        if fair and ask - book[f"{outcome}_bid"] > 0.06 + 1e-12:
            continue
        edge = chance - (ask + FEE * ask * (1 - ask))
        if edge >= threshold and edge > best_edge:
            best, best_edge = f"BUY_{outcome.upper()}", edge
    return best


def oracle(public: Path, subject: str, *, training: bool) -> Iterator[dict]:
    """Private intent generator, with no dependency on the fitted policy."""
    source = SUBJECTS[subject]
    previous: dict[str, str | None] = {}
    rng = random.Random(50128 if training else 50129)
    for row in rows(public):
        ts, market = event_key(row)
        book = oracle_price(row, ts)
        if source is None:
            fully_observed = book is not None and all(
                oracle_probability(row, role, ts) is not None for role in ("lead", "fair")
            )
            side = rng.choice(("BUY_UP", "BUY_DOWN")) if fully_observed and rng.random() < 0.05 else None
        else:
            p_up = oracle_probability(row, source, ts)
            side = script_side(book, p_up, fair=source == "fair") if book is not None and p_up is not None else None
            previous_side = previous.get(market)
            previous[market] = side
            if side == previous_side:
                continue
        if side is not None:
            yield {"ts_ns": ts, "market_id": market, "side": side}


def intent_series(items: Iterable[dict]) -> dict[tuple[str, str], list[int]]:
    by_market: dict[tuple[str, str], list[int]] = defaultdict(list)
    last = (0, "")
    for row in items:
        ts, market = event_key(row)
        side = row.get("side")
        if side not in {"BUY_UP", "BUY_DOWN"} or (ts, market) <= last:
            raise ValueError("intent side is invalid or duplicated decision/market exists")
        by_market[market, side].append(ts)
        last = ts, market
    return dict(by_market)


def timing(predicted: Iterable[dict], actual: Iterable[dict]) -> dict:
    pred, truth = intent_series(predicted), intent_series(actual)
    predicted_count = sum(map(len, pred.values()))
    truth_count = sum(map(len, truth.values()))
    deltas = []
    pairs: Counter[str] = Counter()
    for (market, side), actual_times in truth.items():
        forecast_times = pred.get((market, side), [])
        i = j = 0
        while i < len(actual_times) and j < len(forecast_times):
            dt = forecast_times[j] - actual_times[i]
            if dt < -TOLERANCE_NS:
                j += 1
            elif dt > TOLERANCE_NS:
                i += 1
            else:
                deltas.append(dt)
                pairs[market] += 1
                i += 1
                j += 1
    matches = len(deltas)
    return {
        "truth": truth_count,
        "truth_markets": len({market for market, _ in truth}),
        "predicted": predicted_count,
        "predicted_markets": len({market for market, _ in pred}),
        "matched": matches,
        "matched_markets": len(pairs),
        "precision": matches / predicted_count if predicted_count else None,
        "recall": matches / truth_count if truth_count else None,
        "f1": 2 * matches / (predicted_count + truth_count) if predicted_count + truth_count else None,
        "median_timing_delta_ns": statistics.median(deltas) if deltas else None,
        "median_absolute_delta_ns": statistics.median(map(abs, deltas)) if deltas else None,
    }


def shift_future(row: dict, cutoff: int) -> dict:
    """Perturb only future decisions and feature receipts that are themselves future."""
    moved = row.copy()
    offset = 2 * NS
    moved["ts_ns"] += offset
    for role in ("book", "fair", "lead"):
        if not isinstance(row.get(role), dict):
            continue
        value = row[role].copy()
        moved[role] = value
        fields = ("tw_ns", "binance_tw_ns", "hyperliquid_tw_ns") if role == "lead" else ("tw_ns",)
        for field in fields:
            mark = value.get(field)
            if type(mark) is int and mark > cutoff:
                value[field] += offset
        witness = row[role].get("tw_ns")
        if role in {"lead", "fair"} and type(witness) is int and witness > cutoff and probability(value.get("p_up")):
            value["p_up"] = 1 - value["p_up"]
    return moved


def until(items: Iterable[dict], cutoff: int) -> Iterator[dict]:
    for item in items:
        if item["ts_ns"] > cutoff:
            return
        yield item


def equal_streams(actual: Iterable[dict], expected: Iterable[dict]) -> bool:
    sentinel = object()
    return all(a == b for a, b in zip_longest(actual, expected, fillvalue=sentinel))


def causality(holdout: Path, model: dict, forecast: Path, probes: list[int], scratch: Path) -> list[dict]:
    checks = []
    for index, cutoff in enumerate(probes):
        prefix_path, shifted_path = scratch / f"prefix-{index}", scratch / f"shifted-{index}"
        later = 0
        earlier = 0
        with prefix_path.open("x", encoding="utf-8") as prefix, shifted_path.open("x", encoding="utf-8") as shifted:
            for row in rows(holdout):
                if row["ts_ns"] <= cutoff:
                    print(json.dumps(row, sort_keys=True), file=prefix)
                    earlier += 1
                else:
                    row = shift_future(row, cutoff)
                    later += 1
                print(json.dumps(row, sort_keys=True), file=shifted)
        same_prefix = equal_streams(taker_entries.predict(prefix_path, model), until(rows(forecast), cutoff))
        same_tail = equal_streams(until(taker_entries.predict(shifted_path, model), cutoff),
                                      until(rows(forecast), cutoff))
        checks.append({"cutoff_ns": cutoff, "earlier_rows": earlier, "altered_future_rows": later,
                       "prefix_equal": same_prefix, "perturbed_future_equal": same_tail})
        prefix_path.unlink()
        shifted_path.unlink()
    return checks


def freeze(plan_file: Path, plan_sha: str, inputs: Path, out: Path) -> dict:
    plan, receipts = load_plan(plan_file, plan_sha, inputs)
    out.mkdir(parents=True, exist_ok=False)
    training, holdout = (inputs / name for name in PUBLIC)
    subjects, artifacts = {}, {}
    with TemporaryDirectory(prefix="layer5b-") as work:
        scratch = Path(work)
        for i, subject in enumerate(SUBJECTS):
            training_labels = scratch / f"train-{i}.jsonl"
            write_rows(training_labels, oracle(training, subject, training=True))
            model = taker_entries.fit(training, training_labels)
            model_path, forecast_path = out / f"model-{i}.jsonl", out / f"predictions-{i}.jsonl"
            write_rows(model_path, (model,))
            write_rows(forecast_path, taker_entries.predict(holdout, model))
            subjects[subject] = {
                "model": model_path.name, "predictions": forecast_path.name,
                "causality": causality(holdout, model, forecast_path, plan["probe_cutoffs_ns"], scratch),
            }
            for path in (model_path, forecast_path):
                artifacts[path.name] = sha256(path)
    for name in PUBLIC:
        verified(inputs / name, plan["public_sha256"][name])
    verified(plan_file, plan_sha)
    commitment = out / "forecast.json"
    write_rows(commitment, ({
        "schema": "doppel.layer5b-forecast/v1",
        "plan_sha256": plan_sha, "source_receipts_sha256": receipts, "artifacts": artifacts, "subjects": subjects,
        "fitter_sha256": sha256(Path(taker_entries.__file__)),
        "exam_sha256": sha256(Path(__file__)), "peak_rss_bytes": peak_rss_bytes(),
    },))
    return {"forecast_sha256": sha256(commitment), "forecast": str(commitment)}


def meets_precision_recall(metrics: dict) -> bool:
    return (
        metrics["precision"] is not None and metrics["precision"] >= MIN_PRECISION
        and metrics["recall"] is not None and metrics["recall"] >= MIN_RECALL
    )


def subject_score(subject: str, model: dict, predicted: Path, holdout: Path, checks: list[dict],
                  probes: list[int]) -> dict:
    expected = SUBJECTS[subject]
    metrics = timing(rows(predicted), oracle(holdout, subject, training=False))
    leakage = (
        [c.get("cutoff_ns") for c in checks] == probes
        and all(c.get("prefix_equal") is True and c.get("perturbed_future_equal") is True
                and type(c.get("earlier_rows")) is int and c["earlier_rows"] > 0
                and type(c.get("altered_future_rows")) is int and c["altered_future_rows"] > 0 for c in checks)
    )
    status, trigger = model.get("status"), model.get("trigger_source")
    if expected is None:
        passed = leakage and status == "ABSTAIN" and metrics["predicted"] == 0 and metrics["truth"] > 0
        verdict = "ABSTAIN" if passed else "FAIL"
    else:
        passed = (
            leakage and status == "FIT" and trigger == expected
            and metrics["truth"] >= MIN_EVENTS and metrics["truth_markets"] >= MIN_MARKETS
            and meets_precision_recall(metrics)
            and metrics["median_absolute_delta_ns"] is not None
            and metrics["median_absolute_delta_ns"] <= TOLERANCE_NS
        )
        verdict = "PASS" if passed else "FAIL"
    center = metrics["median_timing_delta_ns"]
    return {
        "status": verdict, "model_status": status, "trigger_source": trigger, **metrics,
        "simultaneous_target_met": center <= 0 if center is not None else None,
        "zero_leakage": leakage, "causality": checks,
    }


def score(plan_file: Path, plan_sha: str, inputs: Path, forecasts: Path, forecast_sha: str) -> dict:
    plan, receipts = load_plan(plan_file, plan_sha, inputs)
    commitment = forecasts / "forecast.json"
    verified(commitment, forecast_sha)
    sealed = load_json(commitment)
    roster = sealed.get("subjects")
    if (
        sealed.get("schema") != "doppel.layer5b-forecast/v1" or sealed.get("plan_sha256") != plan_sha
        or sealed.get("source_receipts_sha256") != receipts or not isinstance(roster, dict)
        or set(roster) != set(SUBJECTS) or sealed.get("exam_sha256") != sha256(Path(__file__))
        or sealed.get("fitter_sha256") != sha256(Path(taker_entries.__file__))
    ):
        raise ValueError("forecast manifest does not match the plan or exam source")
    expected = {f"{prefix}-{i}.jsonl" for i in range(len(SUBJECTS)) for prefix in ("model", "predictions")}
    artifacts = sealed.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != expected:
        raise ValueError("forecast artifact set does not match the exam")
    for name, checksum in artifacts.items():
        verified(forecasts / name, checksum)
    holdout = inputs / PUBLIC[1]
    result = {}
    for i, subject in enumerate(SUBJECTS):
        record = roster[subject]
        model, predictions = f"model-{i}.jsonl", f"predictions-{i}.jsonl"
        if record.get("model") != model or record.get("predictions") != predictions:
            raise ValueError("forecast subject order was changed")
        fitted = load_json(forecasts / model)
        if not equal_streams(taker_entries.predict(holdout, fitted), rows(forecasts / predictions)):
            raise ValueError("committed predictions do not replay on registered public inputs")
        result[subject] = subject_score(subject, fitted, forecasts / predictions, holdout,
                                        record["causality"], plan["probe_cutoffs_ns"])
    verified(holdout, plan["public_sha256"][PUBLIC[1]])
    verified(commitment, forecast_sha)
    for name, checksum in artifacts.items():
        verified(forecasts / name, checksum)
    forecast_rss = sealed.get("peak_rss_bytes")
    if type(forecast_rss) is not int or forecast_rss <= 0:
        raise ValueError("forecast manifest lacks a process high-water RSS measure")
    score_rss = peak_rss_bytes()
    memory_ok = max(forecast_rss, score_rss) < MAX_RSS_BYTES
    passed = memory_ok and all(row["status"] == ("ABSTAIN" if SUBJECTS[name] is None else "PASS")
                               for name, row in result.items())
    return {
        "schema": "doppel.layer5b-score/v1", "source_kind": plan["source_kind"],
        "status": ("MEASURED_PASS" if passed else "MEASURED_FAIL") if plan["source_kind"] == "recorded"
        else "UNMEASURED",
        "software_status": "PASS" if passed else "FAIL", "plan_sha256": plan_sha, "forecast_sha256": forecast_sha,
        "source_receipts_sha256": receipts, "subjects": result,
        "peak_rss_bytes": {"forecast": forecast_rss, "score": score_rss, "limit": MAX_RSS_BYTES, "under_limit": memory_ok},
    }


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--plan", required=True, type=Path)
    cli.add_argument("--plan-sha256", required=True)
    cli.add_argument("--inputs", required=True, type=Path)
    phases = cli.add_subparsers(dest="phase", required=True)
    forecast = phases.add_parser("forecast", help="seal public-only holdout forecasts")
    forecast.add_argument("--out", required=True, type=Path)
    grade = phases.add_parser("score", help="score only after the forecast SHA-256 was sealed")
    grade.add_argument("--forecasts", required=True, type=Path)
    grade.add_argument("--forecast-sha256", required=True)
    grade.add_argument("--out", required=True, type=Path)
    args = cli.parse_args(argv)
    if args.phase == "forecast":
        print(json.dumps(freeze(args.plan, args.plan_sha256, args.inputs, args.out), sort_keys=True))
    else:
        result = score(args.plan, args.plan_sha256, args.inputs, args.forecasts, args.forecast_sha256)
        write_rows(args.out, (result,))
        print(json.dumps({"status": result["status"], "software_status": result["software_status"],
                          "score_sha256": sha256(args.out)}, sort_keys=True))
        return int(result["software_status"] != "PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
