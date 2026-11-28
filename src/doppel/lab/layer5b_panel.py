"""Join the native witnesses on a registered BTC clock before Layer 5b scoring."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..tape.universe import HORIZONS
from ..twin.tape import NS
from .layer5b_books import _packets, book_witnesses
from .layer5b_fair import FairWitnesses
from .lead_witnesses import LeadWitness, LeadWitnesses


def prepare_rows(
    tape: Path, horizon: str, start_s: int, end_s: int, *,
    binance: Iterable[Path], hyperliquid: Iterable[Path],
    predict_lead_up: Callable[[float, LeadWitness], float | None], step_ms: int = 250,
) -> Iterator[dict]:
    """Build public rows before importing any intent or fill labels.

    The independent feature producer supplies and pins the development-calibrated
    lead-to-BRTI transform. It receives current public inputs with sealed receipt
    clocks and must keep its implementation and parameters fixed for both phases.
    """
    length = HORIZONS.get(horizon)
    if length is None:
        raise ValueError("native panel supports complete BTC 5m or 15m markets")
    books = book_witnesses(tape, horizon, start_s, end_s, step_ms)
    reference = FairWitnesses(_packets(tape, "ref_brti_live", start_s - 3600, end_s))
    offshore = LeadWitnesses.from_segments(binance, hyperliquid)
    for row in books:
        cutoff = row["ts_ns"]
        market_start = cutoff // NS // length * length
        fair = reference.at(cutoff, market_start, market_start + length)
        pair = offshore.at(cutoff)
        lead = None
        if fair is not None and pair is not None:
            estimate = predict_lead_up(fair["p_up"], pair)
            if estimate is not None:
                if type(estimate) not in (int, float) or not math.isfinite(estimate) or not 0 <= estimate <= 1:
                    raise ValueError("registered lead transform returned an invalid probability")
                lead = {"tw_ns": max(fair["tw_ns"], pair.tw_ns),
                        "binance_tw_ns": pair.binance.tw_ns, "hyperliquid_tw_ns": pair.hyperliquid.tw_ns,
                        "p_up": float(estimate)}
        yield {**row, "fair": fair, "lead": lead}
