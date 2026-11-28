"""Bounded, declarative candidate probability kept separate from the frozen genome."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import stat
from dataclasses import dataclass

import orjson

from ..tape.universe import HORIZONS
from .genome import Genome

SCHEMA = "doppel.shadow-probability/v1"
FEATURES = frozenset(("fair_up", "mid_up_c", "spread_c", "depth_top_up", "depth_top_down",
                      "brti_move_1s", "brti_move_5s", "brti_move_30s", "dist_to_open_usd"))
MAX_BYTES = 32_768


def _digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value))


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class ShadowProbability:
    sha256: str
    offset_s: int
    intercept: float
    weights: tuple[tuple[str, float], ...]

    def predict(self, features: dict[str, float]) -> float | None:
        inputs = [(features.get(key, math.nan), coefficient) for key, coefficient in self.weights]
        if any(not math.isfinite(value) for value, _ in inputs):
            return None
        terms = [value * coefficient for value, coefficient in inputs]
        if any(not math.isfinite(term) for term in terms):
            return None
        try:
            z = math.fsum([self.intercept, *terms])
        except OverflowError:
            return None
        s = math.exp(-abs(z))
        return 1.0 / (1.0 + s) if z >= 0 else s / (1.0 + s)


def load_probability(path: Path, expected_sha256: str, genome: Genome, *, before_ns: int) -> ShadowProbability:
    """Load the exact signer-approved model and enforce its pre-replay training bound."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("expected a small regular probability model")
        raw = file.read(MAX_BYTES + 1)
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_BYTES or digest != expected_sha256:
        raise ValueError("probability model SHA-256 mismatch")
    doc = orjson.loads(raw)
    canonical = orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)
    if raw not in (canonical, canonical + b"\n"):
        raise ValueError("expected canonical probability model JSON")
    return parse_probability(doc, digest, genome, before_ns=before_ns)


def parse_probability(doc: object, expected_sha256: str, genome: Genome, *, before_ns: int) -> ShadowProbability:
    """Validate the schema and replay bound of an independently hashed model document."""
    keys = {"schema", "genome_id", "offset_s", "fit_end_ns", "fit_sha256", "intercept", "weights"}
    if (not _digest(expected_sha256) or not isinstance(doc, dict) or set(doc) != keys
            or doc["schema"] != SCHEMA or doc["genome_id"] != genome.id):
        raise ValueError("probability model schema, genome or digest differs")
    cutoff, train_end = doc["offset_s"], doc["fit_end_ns"]
    if (type(cutoff) is not int or not 0 < cutoff < min(HORIZONS[h] for h in genome.universe.horizons)
            or type(train_end) is not int or not 0 < train_end < before_ns or not _digest(doc["fit_sha256"])):
        raise ValueError("probability model lacks a prereplay fitting receipt or a valid market cutoff")
    coefficients = doc["weights"]
    if (not _number(doc["intercept"]) or not isinstance(coefficients, dict) or not coefficients
            or not coefficients.keys() <= FEATURES or not all(_number(v) and v != 0 for v in coefficients.values())):
        raise ValueError("probability model coefficients must be finite over supported public features")
    return ShadowProbability(expected_sha256, cutoff, float(doc["intercept"]),
                             tuple((key, float(value)) for key, value in sorted(coefficients.items())))
