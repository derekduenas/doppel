"""Derive the admission archetype from a sealed genome and verify maker evidence."""

from __future__ import annotations

import math
import re
from typing import TypeGuard

from ..twin.engine import SimConfig
from .genome import Genome


_MAKERS = ("maker", "structural_twap")
_OFFSHORE = re.compile(r"(?:binance|hyperliquid|offshore|lead)(?:[_-].+)?")
_NON_LEAD_SOURCES = frozenset(("pmus", "kalshi", "brti", "polymarket_intl", "rtds", "clob"))
_TAPE_LIMITS = SimConfig()


def archetype(genome: Genome) -> str:
    """Use the executable side and the genome's hashed research declarations."""
    evidence = genome.evidence
    declared = evidence.get("archetype", "maker" if genome.mode == "maker" else None)
    if genome.mode == "maker" and genome.maker is not None and not genome.taker:
        if declared in _MAKERS and "taker_trigger_source" not in evidence:
            return str(declared)
    if genome.mode == "taker" and genome.taker and genome.maker is None:
        trigger = evidence.get("taker_trigger_source")
        if trigger in ("lead", "fair"):
            derived = f"taker_{trigger}"
            if "archetype" not in evidence or declared == derived:
                return derived
    raise ValueError("shadow archetype contradicts the sealed genome's execution rules or trigger")


def requires_offshore(genome: Genome) -> bool:
    if archetype(genome) not in _MAKERS:
        return True
    sources = genome.evidence.get("cancel_guard_sources", ())
    if not isinstance(sources, (list, tuple)) or not all(
        type(source) is str and (source in _NON_LEAD_SOURCES or _OFFSHORE.fullmatch(source))
        for source in sources
    ) or len(set(sources)) != len(sources):
        raise ValueError("cancel_guard_sources must list distinct canonical source names")
    assert genome.maker is not None  # required by archetype()
    return any(_OFFSHORE.fullmatch(source) for source in sources) or any(
        _OFFSHORE.fullmatch(condition.feature) for condition in genome.maker.pause_when
    )


def _sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _count(value: object, low: int = 0) -> TypeGuard[int]:
    return type(value) is int and low <= value


def _finite(value: object) -> TypeGuard[int | float]:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def maker_evidence(
    genome: Genome,
    raw_doc: dict,
    replay_sha256: str,
    input_sha256: str,
    *,
    expected_windows: int | None = None,
    base_cancel_latency_ms: float | None = None,
) -> dict:
    """Check signed queue, markout and stress evidence for the bound maker replay.

    The approving operator checks the source receipts and the locked stress plan
    before signing. These structural checks protect the canonical replay identity,
    the sample that each field claims to represent, and the quantitative gates.
    """
    kind = archetype(genome)
    if (kind not in _MAKERS or raw_doc.get("schema") != "doppel.shadow-maker/v1"
            or raw_doc.get("genome_id") != genome.id or raw_doc.get("archetype") != kind
            or not _sha(replay_sha256) or raw_doc.get("replay_sha256") != replay_sha256
            or not _sha(input_sha256) or raw_doc.get("input_sha256") != input_sha256):
        raise ValueError("maker evidence does not bind the sealed genome and canonical replay")

    queue = raw_doc.get("queue")
    if not isinstance(queue, dict):
        raise ValueError("missing L2 maker queue and fill receipts")
    windows = queue.get("windows")
    fills = queue.get("fills")
    fill_windows = queue.get("fill_windows")
    known = queue.get("known_queue_observations")
    quotes = queue.get("quote_observations")
    if (queue.get("model") != "pessimistic_l2_fifo" or not _sha(queue.get("source_sha256"))
            or not _count(windows, 2) or (expected_windows is not None and windows != expected_windows)
            or not _count(fills, 1) or not _count(fill_windows, 1) or fill_windows > min(fills, windows)
            or not _count(quotes, fill_windows) or not _count(known, 1) or known > quotes
            or not _count(queue.get("verified_fills"), 1) or queue["verified_fills"] != fills
            or not _count(queue.get("feed_gap_windows")) or queue["feed_gap_windows"] != 0):
        raise ValueError("maker receipt has incomplete queue, fill or window counts")
    for name, low, high in (
        ("min_trade_coverage", _TAPE_LIMITS.min_trade_coverage, 1.02),
        ("max_inferred_share", 0, _TAPE_LIMITS.max_inferred_share),
        ("max_unidentified_share", 0, _TAPE_LIMITS.max_unidentified_share),
    ):
        value = queue.get(name)
        if not _finite(value) or not low <= value <= high:
            raise ValueError(f"maker receipt violates canonical L2 coverage: {name}")

    markouts = raw_doc.get("markouts")
    if not isinstance(markouts, dict) or set(markouts) != {"2s", "10s"}:
        raise ValueError("maker receipt needs independently measured +2s and +10s post-fill markouts")
    for mark in markouts.values():
        if (not isinstance(mark, dict) or not _sha(mark.get("source_sha256"))
                or not _finite(mark.get("value")) or mark["value"] <= 0
                or not _count(mark.get("observations"), 1) or mark["observations"] > fills
                or not _count(mark.get("windows"), 1)
                or mark["windows"] > min(fill_windows, mark["observations"])):
            raise ValueError("maker post-fill markout lacks positive edge or coherent observation counts")

    stress = raw_doc.get("stress")
    if not isinstance(stress, dict) or stress.get("method") != "delayed_cancel":
        raise ValueError("maker receipt needs canonical delayed-cancel adverse-selection stress")
    base = stress.get("base_cancel_latency_ms")
    extra = stress.get("extra_cancel_latency_ms")
    average = stress.get("net_c_per_window")
    lower = stress.get("ci_low_c_per_window")
    if (not _sha(stress.get("source_sha256")) or not _sha(stress.get("plan_sha256"))
            or not _count(stress.get("windows"), 2) or stress["windows"] != windows
            or not _finite(base) or base < 0
            or (base_cancel_latency_ms is not None and base != base_cancel_latency_ms)
            or not _finite(extra) or extra < 0.000001 or not _finite(base + extra) or base + extra <= base
            or not _finite(average) or average <= 0 or not _finite(lower) or not 0 <= lower <= average):
        raise ValueError("adverse-selection stress lacks full window coverage, locked provenance or nonnegative lower confidence")
    return {"queue": queue, "markout_2s": markouts["2s"], "markout_10s": markouts["10s"], "stress": stress}
