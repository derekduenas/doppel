"""As-of PM US book access for research. WebSocket streams ONLY.

FINDINGS E22: the PM US REST book (`pmus_books`) is served from cache: 88% of polls match an OLDER
WS book, median 8 s and p90 20 s old. REST snapshots must never feed an as-of decision, fill or
feature. This module reads the WS streams (`pmus_ws` before 2026-11-25 22:53Z, then `pmus_ws_5m` and
`pmus_ws_15m`). Book messages are full snapshots, and the record's `tw` is our receive time.
"""

from __future__ import annotations

from pathlib import Path

import orjson

from ..tape.segment import read_segment

WS_STREAMS = ("pmus_ws", "pmus_ws_5m", "pmus_ws_15m")


def ws_books(root: Path, slug_prefix: str) -> dict[str, list[tuple[float, float, float, float, float]]]:
    """slug -> sorted [(recv_s, best_bid, bid_qty, best_ask, ask_qty)] from WS book snapshots."""
    out: dict[str, list] = {}
    for st in WS_STREAMS:
        for f in sorted((root / st).glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True):
                raw = rec["raw"]
                if not raw or '"marketData"' not in raw:
                    continue
                md = orjson.loads(raw).get("marketData") or {}
                slug = md.get("marketSlug", "")
                if not slug.startswith(slug_prefix):
                    continue
                b, o = md.get("bids") or [], md.get("offers") or []
                if not b or not o:
                    continue
                out.setdefault(slug, []).append(
                    (rec["tw"] / 1e9, float(b[0]["px"]["value"]), float(b[0]["qty"]),
                     float(o[0]["px"]["value"]), float(o[0]["qty"])))
    for v in out.values():
        v.sort()
    return out
