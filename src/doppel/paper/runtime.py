"""The paper runtime: a sealed genome running on the live tape, on paper, journaling every decision.

CLOCK. Everything runs in TAPE time: the receive clock ``tw`` the recorder stamps. A cycle reads
the new complete frames of every input stream, computes a watermark ``W`` (every record with
``tw <= W`` is assumed received), feeds the records with ``tw <= W`` to their windows and runs each
window's engine up to ``W``. So decision second ``d`` runs only once ``W >= d`` and sees exactly the
records received by ``d``, as the twin's replay does. ``W = max(now - grace, min over streams of the
newest tw read)``: the recorder flushes a frame at most once a second (the Python recorder also every
5 s), so ``grace`` (default 6 s) is how long paper waits for a quiet stream. A record that arrives
with ``tw <= W`` after ``W`` passed it is LATE: it is journaled and used from the next instant on,
never retroactively.

CYCLE ORDER. kill switch (halt at the previous watermark, before any new decision) -> records ->
new windows -> engines to ``W`` in the twin's window order (start, horizon length, asset) -> global
max-loss stop -> settlement. A cycle whose work changed the journal, or that saw a late record, is
committed with a ``cycle`` record holding ``W``, the clock reading and the tail offsets that
advanced. Other cycles are merged into the next committed one, which is exact because merging only
changes results when something is processed late, and those cycles are always committed.

RISK. The genome's own limits (|inventory|, notional per window, daily loss stand-down, open
windows) are enforced inside the engine and the cross-window rules, exactly as in the twin. The
runtime adds a kill switch (any of ``kill_files`` exists) and a global max-loss stop over settled
P&L plus the worst-case value of open windows (cash - fees + min(0, inventory)). Both halt every
window: no new decisions, every working quote cancelled. A halt is permanent for the journal: start
a new journal to resume trading.

RESTART. Every ``checkpoint_s`` of tape time a committed cycle also pickles the runtime state; the
cycle record pins the pickle's sha256 inside the hash chain, and the file is loaded only if it
matches. Recovery = verify the journal, cut an uncommitted tail, load the newest valid checkpoint,
re-run every committed cycle after it from the same tape bytes, and require the regenerated records
to equal the journal's. Then inventory, cash and open orders are cross-checked against the journal's
own fill and ack records, and the run continues. Any mismatch halts and refuses to continue.

NO ORDERS. Nothing here opens a network connection, reads a credential or can submit, amend or
cancel a real order: the venue is ``PaperSim``, the twin's fill engine.
"""

from __future__ import annotations

import functools
import hashlib
import os
import pickle
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import orjson

from ..strategy.genome import Genome, from_dict, to_dict, validate
from ..twin.cli import code_sha256
from ..twin.engine import SimConfig
from ..twin.flowfeat import uses_intl
from ..twin.tape import NS, PRE_S
from ..tape.universe import HORIZONS
from .inputs import LiveWindow, RefLive, route_ws
from .journal import Journal, strip
from .tail import StreamTail

REF_STREAMS = ("ref_brti_raw", "ref_brti_live", "ref_prices")
UNSETTLED_AFTER_S = 3600
SRC = Path(__file__).resolve().parent


def paper_code_sha256() -> str:
    h = hashlib.sha256(code_sha256().encode())
    for f in sorted(SRC.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    grace_s: float = 6.0
    max_loss_usd: float | None = None      # global stop; default = genome.risk.max_loss_per_day
    kill_files: tuple[str, ...] = ()
    heartbeat_s: int = 60                  # commit a cycle at least this often (tape time)
    checkpoint_s: int = 300
    keep_checkpoints: int = 3
    max_step_s: int = 60                   # catch-up: the watermark advances at most this much per cycle


def check_genome(g: Genome) -> list[str]:
    errs = validate(g)
    if g.universe.venue != "pmus":
        errs.append(f"paper runs only where the twin replays (pmus), not {g.universe.venue}")
    if uses_intl(g):
        errs.append("intl_* flow features are not available live yet (RTDS informedness scores are batch)")
    for h in g.universe.horizons:
        if h not in HORIZONS:
            errs.append(f"unknown horizon {h}")
    return errs


class Hub:
    """What windows call back into. Pickled empty: a loaded checkpoint is re-attached to the runtime
    that loaded it, never to the one that wrote it."""

    def __init__(self) -> None:
        self.rt: PaperRuntime | None = None

    def __getstate__(self):
        return {}

    def __setstate__(self, _):
        self.rt = None

    def emit(self, rec: dict) -> None:
        assert self.rt is not None
        self.rt._emit(rec)

    def can_open(self, key: tuple, t: int) -> bool:
        assert self.rt is not None
        return self.rt._can_open(key, t)

    def stand_down(self, key: tuple) -> bool:
        assert self.rt is not None
        return self.rt._stand_down(key)


@dataclass
class _Settled:
    slug: str
    key: tuple
    end: int
    net: float
    t_settle: int
    worst: float


@dataclass
class State:
    """Everything a checkpoint holds."""
    cursor: int
    first_start: int
    next_start: dict[str, int]
    tails: dict[str, StreamTail]
    pending: list = field(default_factory=list)
    windows: dict[str, LiveWindow] = field(default_factory=dict)
    settled: list[_Settled] = field(default_factory=list)
    ref: RefLive = field(default_factory=RefLive)
    halted: str | None = None
    last_commit_w: int = 0
    last_ckpt_w: int = 0
    late_records: int = 0
    hub: Hub = field(default_factory=Hub)


class PaperRuntime:
    def __init__(self, g: Genome, data: Path, journal_dir: Path, cfg: SimConfig = SimConfig(),
                 settings: Settings = Settings(), clock=time.time_ns):
        errs = check_genome(g)
        if errs:
            raise ValueError("genome not runnable on paper: " + "; ".join(errs))
        self.g, self.data, self.cfg, self.settings, self.clock = g, Path(data), cfg, settings, clock
        self.journal = Journal(journal_dir)
        self.streams = tuple(f"pmus_ws_{h}" for h in g.universe.horizons) + REF_STREAMS
        self.max_loss = settings.max_loss_usd if settings.max_loss_usd is not None else g.risk.max_loss_per_day
        self.kill_files = tuple(Path(p) for p in settings.kill_files) or (Path(journal_dir) / "KILL",)
        self.st: State | None = None
        self._batch: list[dict] = []
        self._capture: list[dict] | None = None   # replay: regenerated records go here
        self._limits: dict[str, dict[str, int]] = {}  # replay: tail offsets the committed cycles reached

    # ================================================================== start / resume
    def start(self, from_s: int | None) -> None:
        recs = self.journal.open()
        if recs:
            self._recover(recs)
            return
        if from_s is None:
            raise ValueError("new journal: give --from-now or --from")
        L = min(HORIZONS[h] for h in self.g.universe.horizons)
        first = -(-from_s // L) * L
        self.st = self._fresh_state(first, from_s * NS)
        self.st.hub.rt = self
        self.journal.append([{"k": "run", "t": from_s * NS, "wall": self.clock(), "genome": to_dict(self.g),
                              "genome_id": self.g.id, "cfg": asdict(self.cfg), "settings": asdict(self.settings),
                              "first_start": first, "from_s": from_s, "data": str(self.data),
                              "code_sha256": paper_code_sha256()}])

    def _fresh_state(self, first_start: int, from_ns: int) -> State:
        back = (self.g.fair.vol_window_s + 120 + PRE_S + 60) * NS
        tails = {s: StreamTail(self.data, s, first_start * NS - back) for s in self.streams}
        nxt = {h: -(-first_start // HORIZONS[h]) * HORIZONS[h] for h in self.g.universe.horizons}
        return State(cursor=first_start * NS - back - 1, first_start=first_start, next_start=nxt, tails=tails,
                     last_commit_w=first_start * NS - back, last_ckpt_w=first_start * NS - back)

    # ================================================================== one cycle
    def cycle(self, marker: dict | None = None) -> int:
        """Run one cycle; in replay, reproduce the committed ``marker``. Returns W."""
        st = self.st
        assert st is not None
        replay = marker is not None
        now = marker["now"] if marker is not None else self.clock()
        if marker is not None:
            for s, delta in marker.get("offsets", {}).items():
                self._limits.setdefault(s, {}).update(delta)
        for s, tail in st.tails.items():
            for r in tail.poll(now, limits=self._limits.get(s, {}) if replay else None):
                st.pending.append((r["tw"], self.streams.index(s), r.get("seq", 0), s, r))
        W = marker["W"] if marker is not None else self._watermark(now)
        kill = bool(marker.get("kill")) if marker is not None else any(p.exists() for p in self.kill_files)
        late0 = st.late_records + sum(w.late for w in st.windows.values())
        n0 = len(self._batch)
        if kill and st.halted is None:
            self._halt(st.cursor + 1, "kill_switch")
        if W > st.cursor:
            st.pending.sort(key=lambda p: p[:3])
            i = 0
            while i < len(st.pending) and st.pending[i][0] <= W:
                tw, _, seq, s, r = st.pending[i]
                if tw <= st.cursor:
                    st.late_records += 1
                    self._emit({"k": "late", "t": st.cursor + 1, "stream": s, "seq": seq, "tw": tw})
                self._ensure_windows(tw)
                self._route(s, r)
                i += 1
            del st.pending[:i]
            self._ensure_windows(W)
            self._advance(W)
            st.cursor = W
            self._max_loss_check(W)
            self._settle(W)
        late = st.late_records + sum(w.late for w in st.windows.values()) - late0
        hb = W - st.last_commit_w >= self.settings.heartbeat_s * NS
        if not replay and (len(self._batch) > n0 or late or kill or hb):
            self._commit(W, now, kill)
        return W

    def _watermark(self, now: int) -> int:
        st = self.st
        assert st is not None
        seen = [t.last_tw for t in st.tails.values()]
        quiet = now - int(self.settings.grace_s * NS)
        if all(s is not None for s in seen):
            quiet = min(now, max(quiet, min(s for s in seen if s is not None)))
        return min(quiet, st.cursor + self.settings.max_step_s * NS)

    def _route(self, s: str, r: dict) -> None:
        st = self.st
        assert st is not None
        if s.startswith("pmus_ws_"):
            route_ws(s, r, st.windows, st.cursor, self.cfg.depth_levels)
            return
        if s == "ref_prices":
            for a, t in st.ref.on_prices(r):
                for w in st.windows.values():
                    if w.asset == a and t in (w.start - 1, w.end - 1):
                        w.refresh_bounds(st.ref)
            return
        secs = st.ref.on_brti_raw(r) if s == "ref_brti_raw" else st.ref.on_brti_live(r)
        for sec in secs:
            obs = st.ref.brti.get(sec)
            if obs is None:
                continue
            for w in st.windows.values():
                w.set_brti(sec, obs)

    # ================================================================== windows and cross-window risk
    def _ensure_windows(self, t_ns: int) -> None:
        st = self.st
        assert st is not None
        new = []
        for h, nxt in st.next_start.items():
            while (nxt - PRE_S) * NS <= t_ns:
                for a in self.g.universe.assets:
                    new.append((nxt, HORIZONS[h], a, h))
                nxt += HORIZONS[h]
            st.next_start[h] = nxt
        for start, _, a, h in sorted(new):
            key = (start, HORIZONS[h], a)
            w = LiveWindow(a, h, start, self.g, self.cfg, st.ref, emit=st.hub.emit,
                           can_open=functools.partial(st.hub.can_open, key),
                           stand_down_fn=functools.partial(st.hub.stand_down, key))
            st.windows[w.slug] = w
            self._emit({"k": "open", "slug": w.slug, "t": (start - PRE_S) * NS, "start": start, "end": w.end,
                        "horizon": h, "asset": a})
            if st.halted is not None:
                w.sim.halt((start - PRE_S) * NS, st.halted)
        if new:
            lo = min(w.ref.raw_t0 for w in st.windows.values() if w.ref.raw is not None) \
                if any(w.ref.raw is not None for w in st.windows.values()) else None
            if lo is not None:
                st.ref.prune(lo - 600)

    def _can_open(self, key: tuple, t: int) -> bool:
        """twin.engine.run_windows: windows EARLIER in order holding a position at t."""
        st = self.st
        assert st is not None
        n = sum(1 for w in st.windows.values() if w.key < key and w.sim.first_fill_ns is not None
                and w.sim.first_fill_ns <= t < w.end * NS)
        return n < self.g.risk.max_open_windows

    def _worst(self, w: LiveWindow) -> float:
        return w.sim.cash - w.sim.fees + min(0.0, w.sim.inv)

    def _value(self, w: LiveWindow, t: int) -> float:
        """An ended window's P&L as known at t: exact once its outcome was received, else worst case."""
        ts = w.settle_time(self.st.ref) if self.st is not None else None
        o = w.ref.outcome
        if ts is not None and ts <= t and o is not None:
            return w.sim.cash - w.sim.fees + w.sim.inv * o
        return self._worst(w)

    def _stand_down(self, key: tuple) -> bool:
        """twin.engine.run_windows' daily stand-down, with what is known at this window's start: an
        ended window whose outcome was not yet received counts at its worst case."""
        st = self.st
        assert st is not None
        start = key[0]
        day = start // 86400
        t = start * NS
        realized = sum(s.net if s.t_settle <= t else s.worst for s in st.settled
                       if s.end <= start and (s.end - 1) // 86400 == day)
        realized += sum(self._value(w, t) for w in st.windows.values()
                        if w.end <= start and (w.end - 1) // 86400 == day)
        return realized <= -self.g.risk.max_loss_per_day + 1e-12

    def _halt(self, t: int, why: str) -> None:
        st = self.st
        assert st is not None
        st.halted = why
        self._emit({"k": "risk", "t": t, "event": "halt_all", "why": why})
        for w in sorted(st.windows.values(), key=lambda w: w.key):
            w.sim.halt(t, why)

    def _advance(self, W: int) -> None:
        """Every window's events up to W in ONE time order (ties: engine priority, then window order),
        so what one window sees of another never depends on how the tape was chunked into cycles."""
        st = self.st
        assert st is not None
        ws = sorted(st.windows.values(), key=lambda w: w.key)
        while True:
            best = None
            for w in ws:
                h = w.sim._heap
                if h and h[0][0] <= W and (best is None or (h[0][0], h[0][1]) < best[0]):
                    best = ((h[0][0], h[0][1]), w)
            if best is None:
                return
            best[1].sim.step()

    def _max_loss_check(self, W: int) -> None:
        st = self.st
        assert st is not None
        if st.halted is not None:
            return
        total = sum(s.net for s in st.settled) + sum(self._value(w, W) for w in st.windows.values())
        if total <= -self.max_loss + 1e-12:
            self._emit({"k": "risk", "t": W, "event": "max_loss", "worst_case_net_usd": round(total, 6),
                        "limit_usd": self.max_loss})
            self._halt(W, "max_loss")

    def _settle(self, W: int) -> None:
        st = self.st
        assert st is not None
        for w in sorted(list(st.windows.values()), key=lambda w: w.key):
            if W < w.hi:
                continue
            ts = w.settle_time(st.ref)
            if ts is not None and ts <= W:
                w.refresh_bounds(st.ref)
                res = w.sim.close()
                net = res["net_usd"]
                body = {k: res[k] for k in ("ptb", "settle", "outcome", "net_usd", "gross_usd", "fees_usd",
                                            "contracts", "n_fills", "final_inventory", "first_fill_ns")}
            elif W >= w.hi + UNSETTLED_AFTER_S * NS:
                ts = w.hi + UNSETTLED_AFTER_S * NS
                net = round(self._worst(w), 6)
                body = {"outcome": None, "net_usd": net, "status": "unsettled_worst_case"}
            else:
                continue
            self._emit({"k": "settle", "slug": w.slug, "t": ts, "start": w.start, "end": w.end,
                        "horizon": w.horizon, "asset": w.asset, "late": w.late, **body})
            st.settled.append(_Settled(w.slug, w.key, w.end, net, ts, round(self._worst(w), 6)))
            del st.windows[w.slug]

    # ================================================================== journal
    def _emit(self, rec: dict) -> None:
        if self._capture is not None:
            self._capture.append(rec)
        else:
            self._batch.append({**rec, "wall": self.clock()})

    def _commit(self, W: int, now: int, kill: bool) -> None:
        st = self.st
        assert st is not None
        marker: dict = {"k": "cycle", "t": W, "W": W, "now": now, "wall": self.clock(), "kill": kill,
                        "offsets": {s: d for s, t in st.tails.items() if (d := t.take_dirty())},
                        "open": len(st.windows), "halted": st.halted}
        st.last_commit_w = W
        if W - st.last_ckpt_w >= self.settings.checkpoint_s * NS:
            st.last_ckpt_w = W
            marker["ck"] = self._checkpoint(len(self.journal.records) + len(self._batch))
        self._batch.append(marker)
        self.journal.append(self._batch)
        self._batch = []

    def _ckdir(self) -> Path:
        return self.journal.dir / "checkpoints"

    def _checkpoint(self, n: int) -> dict:
        d = self._ckdir()
        d.mkdir(exist_ok=True)
        blob = pickle.dumps(self.st, protocol=pickle.HIGHEST_PROTOCOL)
        name = f"ck-{n:012d}.pkl"
        tmp = d / (name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, d / name)
        olds = sorted(d.glob("ck-*.pkl"))[:-self.settings.keep_checkpoints]
        for p in olds:
            p.unlink()
        return {"file": name, "sha256": hashlib.sha256(blob).hexdigest()}

    # ================================================================== recovery
    def _recover(self, recs: list[dict]) -> None:
        head = recs[0]
        if head.get("k") != "run" or head.get("genome_id") != self.g.id:
            raise RecoveryError("journal belongs to another genome")
        if head.get("cfg") != _json(asdict(self.cfg)) or head.get("settings") != _json(asdict(self.settings)):
            raise RecoveryError("restart must use the journal's own settings (see the run record)")
        # newest checkpoint that the chain pins and that is intact
        base_i, state = 0, None
        for i in range(len(recs) - 1, 0, -1):
            ck = recs[i].get("ck")
            if not ck:
                continue
            p = self._ckdir() / ck["file"]
            if p.exists():
                blob = p.read_bytes()
                if hashlib.sha256(blob).hexdigest() == ck["sha256"]:
                    state, base_i = pickle.loads(blob), i
                    break
        self._limits = {}
        st0 = state if state is not None else self._fresh_state(head["first_start"], head["from_s"] * NS)
        st0.hub.rt = self
        self.st = st0
        if state is not None:
            for s, tail in state.tails.items():
                self._limits[s] = dict(tail.offsets)
        self._capture = []
        for r in recs[base_i + 1:]:
            if r["k"] == "cycle":
                self.cycle(marker=r)
        regen = [strip(r) for r in self._capture]
        self._capture = None
        st = self.st
        assert st is not None
        want = [strip(r) for r in recs[base_i + 1:] if r["k"] not in ("cycle", "recovery", "resume", "run")]
        problems = [] if regen == want else [f"replay regenerated {len(regen)} records, journal has {len(want)}; "
                                             f"first difference at {_first_diff(regen, want)}"]
        problems += self._cross_check(recs)
        if problems:
            self.journal.append([{"k": "risk", "t": st.cursor, "wall": self.clock(), "event": "recovery_mismatch",
                                  "problems": problems}])
            raise RecoveryError("; ".join(problems))
        self.journal.append([{"k": "resume", "t": st.cursor, "wall": self.clock(), "from_checkpoint": base_i,
                              "replayed_records": len(regen), "open_windows": sorted(st.windows)}])

    def _cross_check(self, recs: list[dict]) -> list[str]:
        """Inventory, cash and open orders of every open window, from the journal alone."""
        st = self.st
        assert st is not None
        out = []
        for slug, w in st.windows.items():
            fills = [r for r in recs if r["k"] == "fill" and r.get("slug") == slug]
            inv = fills[-1]["inv"] if fills else 0.0
            cash = fills[-1]["cash"] if fills else 0.0
            if abs(inv - w.sim.inv) > 1e-6 or abs(cash - w.sim.cash) > 1e-6:
                out.append(f"{slug}: journal inventory/cash {inv}/{cash} vs state {w.sim.inv}/{w.sim.cash}")
            live = open_orders(recs, slug)
            have = {s: (round(q.price, 6), round(q.remaining, 6)) for s, q in w.sim.live.items()
                    if q is not None and q.remaining > 1e-9 and q.dead_ns is None}
            if live != have:
                out.append(f"{slug}: journal open orders {live} vs state {have}")
        return out


def open_orders(recs: list[dict], slug: str) -> dict[str, tuple[float, float]]:
    """Resting paper quotes of one window from intent/ack/fill records: side -> (price, remaining)."""
    intents: dict[tuple[str, int], dict] = {}
    live: dict[str, list] = {}
    for r in recs:
        if r.get("slug") != slug:
            continue
        if r["k"] == "intent" and r["kind"] in ("post", "amend", "cancel"):
            intents[(r["side"], r["ver"])] = r
        elif r["k"] == "ack" and "ver" in r and r["status"] != "superseded":
            if r.get("replaced") or r["status"] == "cancelled":
                live.pop(r["side"], None)
            if r["status"] == "live":
                it = intents[(r["side"], r["ver"])]
                live[r["side"]] = [it["oid"], round(it["px"], 6), it["size"]]
        elif r["k"] == "fill" and r["role"] == "maker":
            for v in live.values():
                if v[0] == r["oid"]:
                    v[2] -= r["qty"]
    return {s: (v[1], round(v[2], 6)) for s, v in live.items() if v[2] > 1e-9}


def _first_diff(a: list, b: list) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"record {i}: replay {x} != journal {y}"
    return f"record {min(len(a), len(b))} (one list is longer)"


def _json(x):
    return orjson.loads(orjson.dumps(x))


def load_genome(path: Path) -> Genome:
    return from_dict(orjson.loads(Path(path).read_bytes()))
