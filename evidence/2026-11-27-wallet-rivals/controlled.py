"""Offline maker-decision controls. Publish observations, seal predictions, then run the private wallets."""
from __future__ import annotations

import argparse
import hashlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import orjson

from doppel.lab.decision_book import scan
from doppel.lab.goldsky_proof import digest
from doppel.tape.segment import SegmentWriter, atomic_write

NS = 1_000_000_000
STREAM = "clob_btc_5m"


@dataclass(frozen=True)
class Split:
    start: int
    markets: int
    book_seed: int
    private_seed: int

    @property
    def end(self) -> int:
        return self.start + self.markets * 300


SPLITS = {
    "dev": Split(int(datetime(2025, 1, 1, tzinfo=UTC).timestamp()), 6, 41731, 61031),
    "eval": Split(int(datetime(2025, 1, 2, tzinfo=UTC).timestamp()), 12, 41747, 61043),
}
SUBJECTS = ("sim-a", "sim-b", "sim-c")


@dataclass(frozen=True)
class Tick:
    """Exogenous venue state immediately before a scheduled wallet decision."""
    start: int
    slot: int
    bid_cents: int
    ask_cents: int
    bid_size: int

    @property
    def second(self) -> int:
        return self.start + 10 * self.slot

    @property
    def token(self) -> str:
        return f"{self.start}-down"


def market_ticks(split: Split):
    rng = random.Random(split.book_seed)
    for opening in range(split.start, split.end, 300):
        # Stratified private/public *market input*. Each eligible window has a threshold tie,
        # 13 strictly shallow queues and 14 deep queues in random positions.
        sizes = [rng.randrange(10, 1600) for _ in range(13)] + [1600]
        sizes.extend(rng.randrange(1601, 4201) for _ in range(14))
        rng.shuffle(sizes)
        sizes = [rng.randrange(10, 4201), *sizes, rng.randrange(10, 4201)]
        for slot, size in enumerate(sizes):
            bid = rng.randrange(3, 91)
            yield Tick(opening, slot, bid, bid + rng.randrange(1, 5), size)


def snapshot(tick: Tick, receipt_ns: int, bid_size: int) -> bytes:
    return orjson.dumps({
        "event_type": "book", "asset_id": tick.token, "market": str(tick.start),
        "timestamp": str(receipt_ns // 1_000_000 - 20),
        "bids": [{"price": f"{tick.bid_cents / 100:.2f}", "size": str(bid_size)}],
        "asks": [{"price": f"{tick.ask_cents / 100:.2f}", "size": "250"}],
    })


def public_panel(split: Split, root: Path) -> dict:
    """Observe the tape with the production scanner. The private actions never enter this feed."""
    writer = SegmentWriter(root, STREAM)
    queries = []
    listing_time: dict[str, int] = {}
    for tick in market_ticks(split):
        tw = tick.second * NS
        prior = tw - NS // 2
        queries.append((tw, tick.token, tick.second))
        if tick.slot == 0:
            writer.append("", tw=prior, tm=prior, meta={"ctl": "subscribe_add", "token": tick.token, "outcome": "Down"})
            listing_time[tick.token] = prior
        else:
            writer.append(snapshot(tick, prior, tick.bid_size), tw=prior, tm=prior)
        if tick.slot == 29:
            writer.append("", tw=tw, tm=tw, meta={"ctl": "disconnect"})
        else:
            # At exactly the decision clock the next snapshot crosses the queue threshold.
            # Including it would reverse the shallow strategy's answer on each usable cut.
            opposite = 3700 if tick.bid_size <= 1600 else 200
            writer.append(snapshot(tick, tw, opposite), tw=tw, tm=tw)
    writer.close()
    result = scan(root, STREAM, split.start - 1, split.end, queries, {}, set(), {}, 300,
                  grid_from=split.end, details=True)
    cells = []
    for _, token, second in queries:
        state = result["detail"][second]
        book = None
        if state["known"]:
            book = {"bid": state["best_bid"], "ask": state["best_ask"], "bid_shares": state["bid_size"],
                    "depth_tw_ns": state["last_depth_tw_ns"], "identity_tw_ns": listing_time[token]}
        capture = "covered" if result["state_integrity"][second] == "covered" else "gap"
        cells.append({"cutoff_s": second, "book": book, "capture": capture})
    return {"schema": "rival-public/1", "scope": "controlled", "start": split.start, "end": split.end, "cells": cells}


def private_journal(split: Split):
    """Run the wallets from exchange state and a private coin. A new 60-share Down bid is a positive action."""
    coin = random.Random(split.private_seed)
    for tick in market_ticks(split):
        submit = (True, tick.bid_size <= 1600, coin.getrandbits(1) == 1)
        for wallet, accepted in zip(SUBJECTS, submit, strict=True):
            order = ({"token": tick.token, "side": "BUY", "limit_cents": tick.bid_cents, "shares": 60}
                     if accepted else None)
            yield {"wallet": wallet, "cutoff_s": tick.second, "order": order}


def encode(doc) -> bytes:
    return orjson.dumps(doc, option=orjson.OPT_SORT_KEYS) + b"\n"


def publish(path: Path, raw: bytes) -> None:
    try:
        atomic_write(path, raw, replace=False)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError(f"existing controlled artifact differs: {path}") from None


def public_bytes(split: Split) -> bytes:
    with TemporaryDirectory() as folder:
        return encode(public_panel(split, Path(folder)))


def labels(split: Split, public: Path, predictions: Path, journal: Path, output: Path) -> None:
    sealed_public = public.read_bytes()
    if sealed_public != public_bytes(split):
        raise ValueError("controlled observation differs from the registered source and split")
    raw = b"".join(encode(r) for r in private_journal(split))
    submit = {subject: [] for subject in SUBJECTS}
    for line in raw.splitlines():
        r = orjson.loads(line)
        submit[r["wallet"]].append({"cutoff_s": r["cutoff_s"], "action": r["order"] is not None})
    # Predictions are opaque to the wallets. Their exact file hash binds the truth disclosure.
    receipt = {"schema": "rival-labels/1", "scope": "controlled", "start": split.start, "end": split.end,
               "public_sha256": hashlib.sha256(sealed_public).hexdigest(), "predictions_sha256": digest(predictions),
               "subjects": submit}
    publish(journal, raw)
    publish(output, encode(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = parser.add_subparsers(required=True, dest="stage")
    observations = stages.add_parser("public", help="write strictly prior public observations")
    oracle = stages.add_parser("oracle", help="run private wallets after predictions have been frozen")
    for stage in (observations, oracle):
        stage.add_argument("--split", required=True, choices=SPLITS)
        stage.add_argument("--out", required=True, type=Path)
    oracle.add_argument("--public", required=True, type=Path)
    oracle.add_argument("--predictions", required=True, type=Path)
    oracle.add_argument("--journal", required=True, type=Path)
    args = parser.parse_args()
    split = SPLITS[args.split]
    if args.stage == "public":
        publish(args.out, public_bytes(split))
    else:
        labels(split, args.public, args.predictions, args.journal, args.out)


if __name__ == "__main__":
    main()
