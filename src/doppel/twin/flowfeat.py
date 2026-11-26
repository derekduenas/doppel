"""Twin integration of the W3 intl informed-flow guard.

For genomes that use any ``intl_*`` feature, each window's per-second intl features come from
``lab.flow.window_flow_features``. They use only RTDS rows and intl books RECEIVED by that second,
merged into the twin's feature vector. Wallet informedness scores are point-in-time: computed as of
00:00 UTC of the window's day (only windows that ended before then count), cached per day.
"""

from __future__ import annotations

from pathlib import Path

from ..lab.ledger import TokenMap
from ..strategy.genome import Genome
from .features import Features, FeatureConfig, WindowRef

DAY = 86400


def rtds_hours_present(root: Path, lo: int, hi: int) -> bool:
    """True when every hour covering [lo, hi] has an RTDS segment on at least one socket."""
    from ..lab.flow import HOUR, NS, RTDS_STREAMS
    from ..tape.segment import segment_path
    return all(any(segment_path(Path(root), s, h * NS).exists() for s in RTDS_STREAMS)
               for h in range(lo // HOUR * HOUR, hi + 1, HOUR))


def uses_intl(g: Genome) -> bool:
    names = {c.feature for r in g.taker for c in r.when}
    if g.maker is not None:
        names |= {c.feature for c in g.maker.pause_when}
    return any(n.startswith("intl_") for n in names)



class ScoreCache:
    def __init__(self, root: Path, lab: Path):
        from ..lab import flow
        self.flow, self.root = flow, Path(root)
        self.tmap = TokenMap(Path(lab) / "tokens.jsonl")
        self._by_day: dict[int, dict] = {}
        self.headers: dict[int, dict] = {}   # day -> snapshot header (provenance, audit A2-7)

    def scores(self, t: int) -> dict:
        day = t // DAY * DAY
        if day not in self._by_day:
            doc = self.flow.informedness_doc(self.root, self.tmap, day)
            self._by_day[day] = doc["wallets"]
            self.headers[day] = {k: v for k, v in doc.items() if k != "wallets"}
        return self._by_day[day]


class FlowFeatures(Features):
    def __init__(self, genome: Genome, ref: WindowRef, cfg: FeatureConfig, root: Path, cache: ScoreCache,
                 delay_s: int = 0):
        super().__init__(genome, ref, cfg)
        self.delay_s = delay_s  # delay sweep: the guard sees intl flow ``delay_s`` seconds late
        from ..lab.flow import window_flow_features
        hz = f"{(ref.end - ref.start) // 60}m"
        self._flow = window_flow_features(root, ref.asset, hz, ref.start, cache.scores(ref.start))

    def at(self, d, book, inventory_up):
        x = super().at(d, book, inventory_up)
        for k, v in (self._flow.get(d - self.delay_s) or {}).items():
            if k in x and v is not None:
                x[k] = float(v)
        return x
