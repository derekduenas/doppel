"""Fit and replay receipt-time taker entry hypotheses on a registered public clock.

The public JSONL grid is constructed independently of taker labels. Each row
supplies a cutoff, market id, and independently timestamped book, fair and lead
features. A separate training JSONL supplies owner intent labels on that grid.
Only the fit operation reads labels. The forecast operation accepts a fixed
model and later public rows, and cannot consult observed executions.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from random import Random
import tempfile
from typing import Iterator

import orjson

SCHEMA = "doppel.taker-entries/v1"
PUBLIC_FIELDS = {"ts_ns", "market_id", "book", "fair", "lead"}
LABEL_FIELDS = {"ts_ns", "market_id", "side"}
BOOK_FIELDS = {"tw_ns", "up_bid", "up_ask", "down_bid", "down_ask"}
FAIR_FIELDS = {"tw_ns", "p_up"}
LEAD_FIELDS = {"tw_ns", "p_up", "binance_tw_ns", "hyperliquid_tw_ns"}
SOURCES = ("lead", "fair")
SIDES = ("BUY_UP", "BUY_DOWN")
THRESHOLDS = (0.0, 0.005, 0.01, 0.02, 0.03, 0.05)
FEE_COEFFICIENT = 0.02085
FAIR_SPREAD_MAX = 0.06
MAX_AGE_NS = 3_000_000_000
MIN_LABELED_MARKETS = 3
MIN_LABELS = 5
MIN_RECALL = 0.70
MIN_PRECISION = 0.70
FAMILY_GAP = 0.05
NULL_REPLICATES = 99
NULL_SEED = 2026112802

Key = tuple[int, str]
Policy = tuple[str, float]
Signal = tuple[str, float]


def jsonl(path: Path, fields: set[str]) -> Iterator[tuple[Key, dict]]:
    previous: Key | None = None
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = orjson.loads(line)
            except orjson.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{line_number} invalid JSON") from exc
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError(f"{path.name}:{line_number} invalid fields")
            stamp, market = row["ts_ns"], row["market_id"]
            if type(stamp) is not int or stamp <= 0 or not isinstance(market, str) or not market:
                raise ValueError(f"{path.name}:{line_number} invalid cutoff or market")
            key = stamp, market
            if previous is not None and key <= previous:
                raise ValueError(f"{path.name}:{line_number} cutoffs must be strictly ordered by time and market")
            previous = key
            yield key, row


def _witness(witness: object, cutoff: int) -> bool:
    return type(witness) is int and 0 < witness < cutoff and cutoff - witness <= MAX_AGE_NS


def _odds(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _signals(key: Key, row: dict) -> dict[str, Signal]:
    """The best feasible buy for each family, before its firing threshold."""
    cutoff, _ = key
    b = row["book"]
    if not isinstance(b, dict) or set(b) != BOOK_FIELDS or not _witness(b["tw_ns"], cutoff):
        return {}
    quotes = tuple((b[f"{side}_bid"], b[f"{side}_ask"]) for side in ("up", "down"))
    if not all(_odds(bid) and _odds(ask) and 0 < bid <= ask < 1 for bid, ask in quotes):
        return {}

    signals = {}
    for source, fields in (("lead", LEAD_FIELDS), ("fair", FAIR_FIELDS)):
        state = row[source]
        if (not isinstance(state, dict) or set(state) != fields
                or not _witness(state["tw_ns"], cutoff) or not _odds(state["p_up"])):
            continue
        if source == "lead" and not all(
            _witness(state[leg], cutoff) and state[leg] <= state["tw_ns"]
            for leg in ("binance_tw_ns", "hyperliquid_tw_ns")
        ):
            continue
        p_up = state["p_up"]
        opportunities = [
            (side, fair - ask - FEE_COEFFICIENT * ask * (1 - ask))
            for side, fair, (bid, ask) in zip(SIDES, (p_up, 1 - p_up), quotes, strict=True)
            if source != "fair" or ask - bid <= FAIR_SPREAD_MAX + 1e-12
        ]
        if opportunities:
            signals[source] = max(opportunities, key=lambda item: item[1])
    return signals


def _cross(previous: dict[str, str | None], market: str, signal: Signal | None, threshold: float) -> Signal | None:
    side = signal[0] if signal is not None and signal[1] >= threshold else None
    old = previous.get(market)
    previous[market] = side
    return signal if side is not None and side != old else None


@dataclass
class Counts:
    labels: int
    predictions: int = 0
    hits: int = 0

    @property
    def f1(self) -> float:
        return 2 * self.hits / (self.labels + self.predictions) if self.labels + self.predictions else 0.0

    def summary(self) -> dict:
        return {
            "labels": self.labels, "predictions": self.predictions, "hits": self.hits, "f1": self.f1,
            "precision": self.hits / self.predictions if self.predictions else None,
            "recall": self.hits / self.labels if self.labels else None,
        }


def _catalog(path: Path) -> dict[str, tuple[int, int]]:
    windows: dict[str, tuple[int, int]] = {}
    for (stamp, market), _ in jsonl(path, PUBLIC_FIELDS):
        windows[market] = windows.get(market, (stamp, stamp))[0], stamp
    return windows


def _select(public: Path, labels: dict[Key, str], markets: set[str]) -> tuple[Policy, float, float]:
    policies = [(source, threshold) for source in SOURCES for threshold in THRESHOLDS]
    counts = {p: Counts(len(labels)) for p in policies}
    states: dict[Policy, dict[str, str | None]] = {p: {} for p in policies}
    for key, row in jsonl(public, PUBLIC_FIELDS):
        market = key[1]
        if market not in markets:
            continue
        signals = _signals(key, row)
        for policy, stats in counts.items():
            source, threshold = policy
            entry = _cross(states[policy], market, signals.get(source), threshold)
            if entry is not None:
                stats.predictions += 1
                stats.hits += int(labels.get(key) == entry[0])
    # Higher threshold is the preregistered tie-break within a family.
    chosen = max(policies, key=lambda p: (counts[p].f1, p[1]))
    alt = max(c.f1 for p, c in counts.items() if p[0] != chosen[0])
    return chosen, counts[chosen].f1, alt


def _confirm(public: Path, labels: dict[Key, str], markets: set[str], policy: Policy) -> tuple[Counts, float]:
    stats = Counts(len(labels))
    side_state: dict[str, str | None] = {}
    # One byte per eligible cutoff keeps the randomization test small even when
    # the loose policy fires on every cut. Tape rows and prices are never buffered.
    targets: dict[str, list[tuple[int, int]]] = defaultdict(list)
    predictions: dict[str, bytearray] = defaultdict(bytearray)
    source, threshold = policy
    for key, row in jsonl(public, PUBLIC_FIELDS):
        market = key[1]
        if market not in markets:
            continue
        signal = _signals(key, row).get(source)
        entry = _cross(side_state, market, signal, threshold)
        if signal is None:
            continue
        actions = predictions[market]
        index = len(actions)
        truth = labels.get(key)
        if truth is not None:
            targets[market].append((index, SIDES.index(truth) + 1))
        actions.append(SIDES.index(entry[0]) + 1 if entry is not None else 0)
        if entry is not None:
            stats.predictions += 1
            stats.hits += int(truth == entry[0])

    # Independently rotate positive labels within each market, preserving each
    # market's event count and sides. The policy is fixed before this phase.
    rng = Random(NULL_SEED)
    at_least_observed = 0
    for _ in range(NULL_REPLICATES):
        hits = 0
        for market in sorted(targets):
            actions = predictions[market]
            size = len(actions)
            delta = rng.randrange(size)
            hits += sum(actions[(index + delta) % size] == side for index, side in targets[market])
        at_least_observed += int(hits >= stats.hits)
    return stats, (1 + at_least_observed) / (1 + NULL_REPLICATES)


def _stamp(path: Path) -> tuple[int, int, int, int, int]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _sha(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def fit(train_public_path: Path, train_intents_path: Path) -> dict:
    """Select on earlier training markets and confirm on the rest of training."""
    sources = train_public_path, train_intents_path
    stamps = tuple(_stamp(p) for p in sources)
    windows = _catalog(train_public_path)
    intents = {}
    for key, row in jsonl(train_intents_path, LABEL_FIELDS):
        stamp, market = key
        side = row["side"]
        span = windows.get(market)
        if side not in SIDES or span is None or not span[0] <= stamp <= span[1]:
            raise ValueError("training intent outside the public market clock or with an invalid side")
        intents[key] = side
    remaining = set(intents)
    for key, _ in jsonl(train_public_path, PUBLIC_FIELDS):
        remaining.discard(key)
    if remaining:
        raise ValueError("training intent is not a registered public cutoff")

    model = {
        "schema": SCHEMA, "status": "ABSTAIN", "trigger_source": None, "threshold": None,
        "training_range_ns": [
            min((start for start, _ in windows.values()), default=None),
            max((end for _, end in windows.values()), default=None),
        ],
        "train_public_sha256": _sha(train_public_path),
        "train_intents_sha256": _sha(train_intents_path),
        "fee_coefficient": FEE_COEFFICIENT,
    }
    chronological = sorted(windows, key=lambda m: (windows[m][0], m))
    if len(chronological) < MIN_LABELED_MARKETS * 2:
        decision = model | {"reason": "too_few_training_markets"}
    else:
        index = min(len(chronological) * 2 // 3, len(chronological) - MIN_LABELED_MARKETS)
        cutoff = windows[chronological[index]][0]
        earlier = {m for m, (_, end) in windows.items() if end < cutoff}
        later = {m for m, (start, _) in windows.items() if start >= cutoff}
        groups = [{key: side for key, side in intents.items() if key[1] in markets}
                  for markets in (earlier, later)]
        model.update({
            "confirmation_start_ns": cutoff,
            "purged_overlap_markets": len(windows) - len(earlier) - len(later),
            "labeled_markets_by_phase": [len({key[1] for key in g}) for g in groups],
            "labels_by_phase": [len(g) for g in groups],
        })
        if any(len(g) < MIN_LABELS or len({k[1] for k in g}) < MIN_LABELED_MARKETS for g in groups):
            decision = model | {"reason": "too_few_labeled_training_markets"}
        else:
            chosen, selected, alternate = _select(train_public_path, groups[0], earlier)
            stats, null_p = _confirm(train_public_path, groups[1], later, chosen)
            diag = {
                "selection_f1": selected, "competing_family_f1": alternate,
                "training_confirmation": stats.summary(), "rotated_null_p": null_p,
                "rotated_null_replicates": NULL_REPLICATES,
            }
            if selected - alternate < FAMILY_GAP:
                decision = model | diag | {"reason": "ambiguous_trigger_family"}
            elif (stats.hits < MIN_LABELS
                  or stats.hits < MIN_PRECISION * stats.predictions
                  or stats.hits < MIN_RECALL * stats.labels or null_p > 0.05):
                decision = model | diag | {"reason": "training_confirmation_did_not_beat_null"}
            else:
                decision = model | diag | {
                    "status": "FIT", "trigger_source": chosen[0], "threshold": chosen[1], "reason": None,
                }
    if tuple(_stamp(p) for p in sources) != stamps:
        raise ValueError("training inputs changed during fitting")
    return decision


def predict(public_path: Path, model: dict) -> Iterator[dict]:
    """Emit forecasts on cutoffs strictly later than the frozen training range."""
    status = model.get("status")
    history = model.get("training_range_ns")
    if model.get("schema") != SCHEMA or status not in ("FIT", "ABSTAIN"):
        raise ValueError("invalid taker entry model")
    if not isinstance(history, list) or len(history) != 2 or (
        history != [None, None] and
        not (all(type(t) is int and t > 0 for t in history) and history[0] <= history[1])
    ):
        raise ValueError("invalid taker entry training range")
    source, threshold = model.get("trigger_source"), model.get("threshold")
    policy: Policy | None = None
    if status == "FIT":
        if (history[1] is None or not isinstance(source, str) or source not in SOURCES or
                type(threshold) not in (int, float) or threshold not in THRESHOLDS):
            raise ValueError("invalid taker entry policy")
        policy = source, float(threshold)
    previous: dict[str, str | None] = {}
    sealed = _stamp(public_path)
    for key, row in jsonl(public_path, PUBLIC_FIELDS):
        stamp, market = key
        if history[1] is not None and stamp <= history[1]:
            raise ValueError("prediction grid overlaps training cutoffs")
        if policy is not None:
            source, threshold = policy
            entry = _cross(previous, market, _signals(key, row).get(source), threshold)
            if entry is not None:
                yield {"ts_ns": stamp, "market_id": market, "side": entry[0],
                       "predicted_edge": entry[1], "trigger_source": source}
    if _stamp(public_path) != sealed:
        raise ValueError("prediction input changed during replay")


def _write_once(path: Path, payloads: Iterator[bytes]) -> None:
    """Publish no partial forecast if a later input row fails validation."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        tmp = Path(output.name)
        try:
            for payload in payloads:
                output.write(payload)
            output.flush()
            os.fsync(output.fileno())
            os.link(tmp, path)
        finally:
            tmp.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="doppel-rulefit --taker-entries", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    fitting = commands.add_parser("fit", help="fit a taker policy using chronological training data")
    fitting.add_argument("--public", type=Path, required=True)
    fitting.add_argument("--intents", type=Path, required=True)
    forecast = commands.add_parser("predict", help="replay a frozen policy on later public data")
    forecast.add_argument("--public", type=Path, required=True)
    forecast.add_argument("--model", type=Path, required=True)
    for command in (fitting, forecast):
        command.add_argument("--out", type=Path, required=True, help="new output file")
    args = parser.parse_args(argv)
    try:
        if args.action == "fit":
            report = fit(args.public, args.intents)
            _write_once(args.out, iter((orjson.dumps(report, option=orjson.OPT_SORT_KEYS) + b"\n",)))
        else:
            model = orjson.loads(args.model.read_bytes())
            if not isinstance(model, dict):
                raise ValueError("invalid taker entry model")
            forecasts = predict(args.public, model)
            _write_once(args.out, (orjson.dumps(row, option=orjson.OPT_SORT_KEYS) + b"\n" for row in forecasts))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
