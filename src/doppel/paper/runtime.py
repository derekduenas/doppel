"""The paper runtime: a sealed genome running on the live tape, on paper, journaling every decision.

CLOCK. Everything runs in TAPE time: the receive clock ``tw`` the recorder stamps. A cycle reads
the new complete frames of every input stream, computes a watermark ``W`` (every record with
``tw <= W`` is assumed received), feeds the records with ``tw <= W`` to their windows and runs all
windows' events up to ``W`` in one time order. So decision second ``d`` runs only once ``W >= d`` and
sees exactly the records received by ``d``, as the twin's replay does. ``W = min(cursor + max_step,
max(now - grace, min over streams of the newest tw read))``. A record that arrives with
``tw <= cursor`` is LATE: it is journaled, and it is known from cursor + 1 ns on, never retroactively.
Raw BRTI rows from PERELMAN are known at max(PERELMAN's receive stamp, the cycle clock that read them).

CYCLE ORDER. tails -> PERELMAN rows -> kill switch (halt at the cycle's starting cursor + 1 ns, before
any new decision) -> records -> new windows -> engines to ``W`` -> daily max-loss stop -> settlement.
A cycle is COMMITTED (a ``cycle`` record with its starting cursor ``c0``, ``W``, the clock, the tail
offsets that advanced and whether it newly applied the kill) when it wrote records, saw a late event,
read PERELMAN rows, newly applied the kill, or reached the heartbeat. Other cycles are merged into
the next committed one. Replay reproduces a committed cycle exactly: it first advances to ``c0`` with
every record except the ones that cycle journaled as late (those were read only in it), then runs
the cycle itself. Merging is exact because an uncommitted cycle had no late event, no PERELMAN row,
no window creation, settlement or halt.

RISK. The genome's own limits (|inventory|, notional per window, daily-loss stand-down, open
windows) are enforced inside the engine and the cross-window rules, exactly as in the twin. The
runtime adds a kill switch (any of ``kill_files`` exists), a max-loss stop per UTC day over settled
P&L plus the worst case of every open window INCLUDING its resting quotes filling, and a stale-book
bound (no quoting against a WS book older than ``max_book_age_s``). A halt cancels every working
quote and stops every decision; it is permanent for the journal (start a new journal to resume).

RESTART. Every ``checkpoint_s`` of tape time a committed cycle also pickles the runtime state; the
cycle record pins the pickle's sha256 and format version inside the hash chain. Recovery: verify the
journal (streaming) and cut an uncommitted tail; refuse if the code differs from the run record's;
load the newest valid checkpoint; stream the committed cycles after it, re-run each from the same
tape bytes and require its regenerated records to equal the journaled batch; cross-check inventory,
cash and resting orders of every open window against the journal; continue. Any mismatch is
journaled and refused (exit 3). Memory stays bounded: the journal is never held in memory.

FAILURE. A tape CRC/JSON error, or a segment whose bytes never become a complete frame within
``stall_s``, is journaled as a ``tape_error`` risk event and stops the runtime (exit 3).

NO ORDERS. Nothing here opens a network connection, reads a credential or can submit, amend or
cancel a real order: the venue is ``PaperSim``, the twin's fill engine.
"""

from __future__ import annotations

import functools
import hashlib
import math
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
from .epoch import ShadowEpochs
from .inputs import LiveWindow, RefLive, route_ws
from .journal import Journal, iter_records, strip
from .shadow import ShadowSession, authorize
from .tail import PerelmanTail, StreamTail, TapeError

REF_STREAMS = ("ref_brti_live", "ref_prices")
PERELMAN = "perelman"
UNSETTLED_AFTER_S = 3600
CKPT_VERSION = 2
NOT_REPLAYED = ("cycle", "recovery", "resume", "run")
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
    max_loss_usd: float | None = None      # per UTC day; default = genome.risk.max_loss_per_day
    kill_files: tuple[str, ...] = ()
    heartbeat_s: int = 60                  # commit a cycle at least this often (tape time)
    checkpoint_s: int = 300
    keep_checkpoints: int = 3
    max_step_s: int = 60                   # the watermark advances at most this much per cycle
    max_poll_bytes: int = 64 << 20         # per stream per cycle
    stall_s: float = 30.0
    max_book_age_s: float = 30.0           # 0 = no bound
    perelman_src: str | None = None        # PERELMAN raw BRTI rows: the relay's output dir (read-only)


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


def check_binding(g: Genome, cfg: SimConfig, registry_root: Path) -> list[str]:
    """A genome bound to an evaluation protocol runs only with the protocol's simulation settings."""
    from ..strategy.registry import Registry
    reg = Registry(registry_root)
    if not reg.path.exists():
        return [f"no registry at {reg.path}: cannot check the genome's protocol binding"]
    bound = reg.protocol_binding(g.id)
    if bound is None:
        return []
    sim = (bound.get("params") or {}).get("simulation") or {}
    have = orjson.loads(orjson.dumps(asdict(cfg)))
    diff = sorted(k for k, v in sim.items() if have.get(k) != v)
    if diff:
        return [f"genome is bound to {bound['name']} v{bound['version']}: run with its simulation settings "
                + ", ".join(f"{k}={sim[k]!r}" for k in diff)]
    return []


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
    perelman: PerelmanTail | None = None
    pending: list = field(default_factory=list)
    windows: dict[str, LiveWindow] = field(default_factory=dict)
    settled: list[_Settled] = field(default_factory=list)
    ref: RefLive = field(default_factory=RefLive)
    halted: str | None = None
    last_commit_w: int = 0
    last_ckpt_w: int = 0
    late_records: int = 0
    hub: Hub = field(default_factory=Hub)
    shadow_epochs: ShadowEpochs | None = None


class PaperRuntime:
    def __init__(self, g: Genome, data: Path, journal_dir: Path, cfg: SimConfig = SimConfig(),
                 settings: Settings = Settings(), clock=time.time_ns, *, shadow_bundle: Path | None = None):
        errs = check_genome(g)
        if errs:
            raise ValueError("genome not runnable on paper: " + "; ".join(errs))
        self.g, self.data, self.cfg, self.settings, self.clock = g, Path(data), cfg, settings, clock
        self.journal = Journal(journal_dir)
        self.streams = tuple(f"pmus_ws_{h}" for h in g.universe.horizons) + REF_STREAMS
        self.max_loss = settings.max_loss_usd if settings.max_loss_usd is not None else g.risk.max_loss_per_day
        self.kill_files = tuple(Path(p) for p in settings.kill_files) or (Path(journal_dir) / "KILL",)
        self._shadow_bundle = shadow_bundle
        self._shadow: ShadowSession | None = None
        self.st: State | None = None
        self._batch: list[dict] = []
        self._capture: list[dict] | None = None   # replay: regenerated records go here
        self._limits: dict[str, dict[str, int]] = {}  # replay: offsets the committed cycles reached

    @property
    def finished(self) -> bool:
        """Halted with nothing left to settle: the run can exit."""
        return self.st is not None and self.st.halted is not None and not self.st.windows

    # ================================================================== start / resume
    def start(self, from_s: int | None) -> None:
        if self._shadow_bundle is not None:
            if not math.isfinite(self.max_loss) or not 0 < self.max_loss <= self.g.risk.max_loss_per_day:
                raise ValueError("shadow max loss must stay within the sealed risk limit")
            if from_s is not None and abs(from_s - self.clock() // NS) > 1:
                raise ValueError("shadow must start at the current clock or resume its approved journal")
            self._shadow = authorize(self.g, self._shadow_bundle, self.data, self.journal.dir,
                                     self.settings.perelman_src, self.cfg)
            if self._shadow.stop_reason(self.clock()) is not None:
                raise RecoveryError("shadow approval changed or expired during validation")
            if from_s is not None:
                from_s = self.clock() // NS
            self.kill_files = (*self.kill_files, self.journal.dir / "KILL", self.journal.dir.parent / "KILL")
        head = self.journal.open()
        if head is not None:
            self._recover(head)
            return
        if from_s is None:
            raise ValueError("new journal: give --from-now or --from")
        L = min(HORIZONS[h] for h in self.g.universe.horizons)
        first = -(-from_s // L) * L
        st = self._fresh_state(first)
        st.hub.rt = self
        self.st = st
        self.journal.append([{"k": "run", "t": from_s * NS, "wall": self.clock(), "genome": to_dict(self.g),
                              "genome_id": self.g.id, "cfg": asdict(self.cfg), "settings": asdict(self.settings),
                              "first_start": first, "from_s": from_s, "data": str(self.data),
                              "admission": self._admission_binding(),
                              "code_sha256": paper_code_sha256()}], t_ns=from_s * NS)

    def _admission_binding(self) -> dict:
        return self._shadow.journal_binding() if self._shadow is not None else {"state": "RESEARCH_OBSERVE"}

    def _fresh_state(self, first_start: int) -> State:
        back = (self.g.fair.vol_window_s + 120 + PRE_S + 60) * NS
        tails = {s: StreamTail(self.data, s, first_start * NS - back) for s in self.streams}
        per = PerelmanTail(Path(self.settings.perelman_src)) if self.settings.perelman_src else None
        nxt = {h: -(-first_start // HORIZONS[h]) * HORIZONS[h] for h in self.g.universe.horizons}
        return State(cursor=first_start * NS - back - 1, first_start=first_start, next_start=nxt, tails=tails,
                     perelman=per, last_commit_w=first_start * NS - back, last_ckpt_w=first_start * NS - back,
                     shadow_epochs=ShadowEpochs.start(self.g, first_start, self.settings.max_book_age_s)
                     if self._shadow is not None else None)

    # ================================================================== one cycle
    def cycle(self, marker: dict | None = None, late_keys: set | None = None) -> int:
        """Run one cycle; in replay, reproduce the committed ``marker``. Returns W."""
        st = self.st
        assert st is not None
        replay = marker is not None
        c0 = st.cursor
        now = marker["now"] if marker is not None else self.clock()
        try:
            if marker is not None:
                W, c0m = marker["W"], marker["c0"]
                for s, delta in marker.get("offsets", {}).items():
                    self._limits.setdefault(s, {}).update(delta)
                self._poll_tape(now, limits=True)
                self._catch_up(c0m, late_keys or set())
                c0 = st.cursor
            else:
                self._poll_tape(now, limits=False)
                W = self._watermark(now)
            rows = st.perelman.poll(self._limits.get(PERELMAN, {}) if replay else None) if st.perelman else []
        except TapeError as exc:
            if replay:
                raise
            self._fatal(c0, now, exc)
            raise
        if rows:
            for sec in st.ref.on_perelman(rows, now):
                self._brti_to_windows(sec)
        if marker is not None:
            stop_reason = marker.get("stop_reason", "kill_switch") if marker.get("kill") else None
        else:
            stop_reason = self._shadow.stop_reason(now) if self._shadow is not None else None
            if stop_reason is None and any(p.exists() for p in self.kill_files):
                stop_reason = "kill_switch"
        kill_new = stop_reason is not None and st.halted is None
        late0 = st.late_records + sum(w.late for w in st.windows.values())
        n0 = len(self._batch)
        if stop_reason is not None and kill_new:
            self._halt(c0 + 1, stop_reason)
        if W > c0:
            self._ingest(W, None)
            self._ensure_windows(W)
            self._advance(W)
            st.cursor = W
            self._max_loss_check(W)
            self._settle(W)
        late = st.late_records + sum(w.late for w in st.windows.values()) - late0
        hb = W - st.last_commit_w >= self.settings.heartbeat_s * NS
        if not replay and (len(self._batch) > n0 or late or kill_new or hb or rows):
            self._commit(W, now, c0, stop_reason if kill_new else None)
        return W

    def _poll_tape(self, now: int, limits: bool) -> None:
        st = self.st
        assert st is not None
        bound = st.cursor + self.settings.max_step_s * NS
        for s, tail in st.tails.items():
            if limits:
                recs = tail.poll(now, limits=self._limits.get(s, {}))
            else:
                recs = tail.poll(now, max_tw=bound, max_bytes=self.settings.max_poll_bytes,
                                 stall_s=self.settings.stall_s)
            for r in recs:
                st.pending.append((r["tw"], self.streams.index(s), r.get("seq", 0), s, r))

    def _catch_up(self, c0: int, late_keys: set) -> None:
        """Replay: the merged uncommitted cycles before a committed one, i.e. advance to its starting
        cursor with every record read so far except those it journaled as late."""
        st = self.st
        assert st is not None
        if c0 <= st.cursor:
            return
        self._ingest(c0, late_keys)
        self._ensure_windows(c0)
        self._advance(c0)
        st.cursor = c0

    def _ingest(self, W: int, hold: set | None) -> None:
        st = self.st
        assert st is not None
        st.pending.sort(key=lambda p: p[:3])
        keep = []
        for p in st.pending:
            tw, _, seq, s, r = p
            if tw > W or (hold is not None and (s, seq) in hold):
                keep.append(p)
                continue
            late = tw <= st.cursor
            if late:
                st.late_records += 1
                self._emit({"k": "late", "t": st.cursor + 1, "stream": s, "seq": seq, "tw": tw})
            self._ensure_windows(tw)
            self._route(s, r, st.cursor + 1 if late else 0)
        st.pending = keep

    def _watermark(self, now: int) -> int:
        st = self.st
        assert st is not None
        seen = [t.last_tw for t in st.tails.values()]
        w = now - int(self.settings.grace_s * NS)
        if all(s is not None for s in seen):
            w = min(now, max(w, min(s for s in seen if s is not None)))
        for t in st.tails.values():          # a byte cap stopped this stream early: nothing past it is read
            if t.capped and t.last_tw is not None:
                w = min(w, t.last_tw)
        return min(w, st.cursor + self.settings.max_step_s * NS)

    def _route(self, s: str, r: dict, floor_ns: int) -> None:
        st = self.st
        assert st is not None
        if s.startswith("pmus_ws_"):
            route_ws(s, r, st.windows, st.cursor, self.cfg.depth_levels)
            return
        if s == "ref_prices":
            for a, t in st.ref.on_prices(r, floor_ns):
                for w in st.windows.values():
                    if w.asset == a and t in (w.start - 1, w.end - 1):
                        w.refresh_bounds(st.ref)
            return
        for sec in st.ref.on_brti_live(r, floor_ns):
            self._brti_to_windows(sec)

    def _brti_to_windows(self, sec: int) -> None:
        st = self.st
        assert st is not None
        obs = st.ref.brti.get(sec)
        if obs is not None:
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
        age = int(self.settings.max_book_age_s * NS) or None
        if st.halted is not None:
            new = []                       # a halted run opens no new window; it only settles the open ones
        for start, _, a, h in sorted(new):
            key = (start, HORIZONS[h], a)
            w = LiveWindow(a, h, start, self.g, self.cfg, st.ref, emit=st.hub.emit,
                           can_open=functools.partial(st.hub.can_open, key),
                           stand_down_fn=functools.partial(st.hub.stand_down, key), max_book_age_ns=age)
            st.windows[w.slug] = w
            self._emit({"k": "open", "slug": w.slug, "t": (start - PRE_S) * NS, "start": start, "end": w.end,
                        "horizon": h, "asset": a})
        if new:
            los = [w.ref.raw_t0 for w in st.windows.values() if w.ref.raw is not None]
            if los:
                st.ref.prune(min(min(los), min(w.start for w in st.windows.values()) - 1) - 600)

    def _can_open(self, key: tuple, t: int) -> bool:
        """twin.engine.run_windows: windows EARLIER in order holding a position at t."""
        st = self.st
        assert st is not None
        n = sum(1 for w in st.windows.values() if w.key < key and w.sim.first_fill_ns is not None
                and w.sim.first_fill_ns <= t < w.end * NS)
        return n < self.g.risk.max_open_windows

    @staticmethod
    def _worst(w: LiveWindow, t: int) -> float:
        """The window's value if everything goes against it: the worse outcome, and every quote that
        can still fill (live or about to land) filling in full."""
        sim = w.sim
        quotes = []
        if t < w.end * NS:
            quotes = [q for q in sim.live.values() if q is not None and q.remaining > 1e-9 and q.dead_ns is None]
            quotes += [q for _, q in sim.pending.values() if q is not None]
        out = math.inf
        for o in (0, 1):
            v = sim.cash - sim.fees + sim.inv * o
            for q in quotes:
                r = q.remaining if q.live_ns is not None else q.size
                v += min(0.0, r * (o - q.price)) if q.side == "bid" else min(0.0, r * (q.price - o))
            out = min(out, v)
        return out

    def _value(self, w: LiveWindow, t: int) -> float:
        """A window's P&L as known at t: exact once ended and its outcome received, else worst case."""
        st = self.st
        assert st is not None
        ts = w.settle_time(st.ref)
        o = w.ref.outcome
        if ts is not None and ts <= t and o is not None:
            return w.sim.cash - w.sim.fees + w.sim.inv * o
        return self._worst(w, t)

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
        """Stop when today's (UTC) settled P&L plus every open window's worst case reaches the limit."""
        st = self.st
        assert st is not None
        if st.halted is not None:
            return
        day = W // NS // 86400
        total = sum(s.net for s in st.settled if (s.end - 1) // 86400 == day)
        total += sum(self._value(w, W) for w in st.windows.values())
        if total <= -self.max_loss + 1e-12:
            self._emit({"k": "risk", "t": W, "event": "max_loss", "day_worst_case_net_usd": round(total, 6),
                        "limit_usd": self.max_loss})
            self._halt(W, "max_loss")

    def _settle(self, W: int) -> None:
        st = self.st
        assert st is not None
        for w in sorted(list(st.windows.values()), key=lambda w: w.key):
            if W < w.hi:
                continue
            ts = w.settle_time(st.ref)
            res = None
            if ts is not None and ts <= W:
                w.refresh_bounds(st.ref)
                res = w.sim.close()
                net = res["net_usd"]
                body = {k: res[k] for k in ("ptb", "settle", "outcome", "net_usd", "gross_usd", "fees_usd",
                                            "contracts", "n_fills", "final_inventory", "first_fill_ns")}
            elif W >= w.hi + UNSETTLED_AFTER_S * NS:
                ts = w.hi + UNSETTLED_AFTER_S * NS
                net = round(self._worst(w, ts), 6)
                body = {"outcome": None, "net_usd": net, "status": "unsettled_worst_case"}
            else:
                continue
            if st.shadow_epochs is not None:
                body["shadow_monitor"] = st.shadow_epochs.settle(w, res, inactive=st.halted is not None)
            self._emit({"k": "settle", "slug": w.slug, "t": ts, "start": w.start, "end": w.end,
                        "horizon": w.horizon, "asset": w.asset, "late": w.late,
                        "opening_source": w.feats.source, "opening_switches": w.feats.switches,
                        "stale_book_s": w.sim.stale_book_s, **body})
            st.settled.append(_Settled(w.slug, w.key, w.end, net, ts, round(self._worst(w, ts), 6)))
            del st.windows[w.slug]
        today = W // NS // 86400
        st.settled = [s for s in st.settled if (s.end - 1) // 86400 >= today - 1]

    # ================================================================== journal
    def _emit(self, rec: dict) -> None:
        if self._capture is not None:
            self._capture.append(rec)
        else:
            self._batch.append({**rec, "wall": self.clock()})

    def _commit(self, W: int, now: int, c0: int, stop_reason: str | None) -> None:
        st = self.st
        assert st is not None
        offsets = {s: d for s, t in st.tails.items() if (d := t.take_dirty())}
        if st.perelman is not None and (d := st.perelman.take_dirty()):
            offsets[PERELMAN] = d
        marker: dict = {"k": "cycle", "t": W, "W": W, "c0": c0, "now": now, "wall": self.clock(), "kill": stop_reason is not None,
                        "offsets": offsets, "open": len(st.windows), "halted": st.halted}
        if stop_reason not in (None, "kill_switch"):
            marker["stop_reason"] = stop_reason
        st.last_commit_w = W
        if W - st.last_ckpt_w >= self.settings.checkpoint_s * NS:
            st.last_ckpt_w = W
            marker["ck"] = self._checkpoint(self.journal.n + len(self._batch))
        self._batch.append(marker)
        self.journal.append(self._batch, t_ns=W)
        self._batch = []

    def _fatal(self, c0: int, now: int, exc: Exception) -> None:
        """Journal a tape failure (its own committed batch; replay skips it) before stopping."""
        self._batch = []
        self.journal.append([{"k": "risk", "t": c0, "wall": self.clock(), "event": "tape_error",
                              "error": str(exc)[:400]},
                             {"k": "cycle", "t": c0, "W": c0, "c0": c0, "now": now, "wall": self.clock(),
                              "kill": False, "offsets": {}, "fatal": True}], t_ns=c0)

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
        for p in sorted(d.glob("ck-*.pkl"))[:-self.settings.keep_checkpoints]:
            p.unlink()
        return {"file": name, "sha256": hashlib.sha256(blob).hexdigest(), "v": CKPT_VERSION}

    # ================================================================== recovery
    def _recover(self, head: dict) -> None:
        if head.get("k") != "run" or head.get("genome_id") != self.g.id:
            raise RecoveryError("journal belongs to another genome")
        if head.get("admission", {"state": "RESEARCH_OBSERVE"}) != self._admission_binding():
            raise RecoveryError("restart must retain the journal's shadow admission and operating mode")
        if head.get("cfg") != _json(asdict(self.cfg)) or head.get("settings") != _json(asdict(self.settings)):
            raise RecoveryError("restart must use the journal's own settings (see the run record)")
        if head.get("code_sha256") != paper_code_sha256():
            raise RecoveryError("the paper/twin code differs from the code that wrote this journal: start a new "
                                "journal (a run never changes code mid-journal)")
        # pass 1 (streaming): checkpoints pinned by the chain, and each open window's own records
        cks: list[tuple[int, dict]] = []
        per_slug: dict[str, list[dict]] = {}
        for r in iter_records(self.journal.dir):
            if r["k"] == "cycle" and r.get("ck"):
                cks.append((r["n"], r["ck"]))
                cks = cks[-(self.settings.keep_checkpoints + 2):]
            slug = r.get("slug")
            if slug and r["k"] in ("intent", "ack", "fill"):
                per_slug.setdefault(slug, []).append(r)
            elif slug and r["k"] == "settle":
                per_slug.pop(slug, None)
        base_n, state = -1, None
        for n, ck in reversed(cks):
            p = self._ckdir() / ck["file"]
            if ck.get("v") != CKPT_VERSION:
                raise RecoveryError(f"checkpoint {ck['file']} has format v{ck.get('v')}, this code writes v{CKPT_VERSION}")
            if p.exists():
                blob = p.read_bytes()
                if hashlib.sha256(blob).hexdigest() == ck["sha256"]:
                    state, base_n = pickle.loads(blob), n
                    break
        st = state if state is not None else self._fresh_state(head["first_start"])
        st.hub.rt = self
        self.st = st
        self._limits = {}
        if state is not None:
            for s, tail in state.tails.items():
                self._limits[s] = dict(tail.offsets)
            if state.perelman is not None:
                self._limits[PERELMAN] = dict(state.perelman.offsets)
        # pass 2 (streaming): re-run every committed cycle after the checkpoint, batch by batch
        problems: list[str] = []
        batch: list[dict] = []
        replayed = 0
        try:
            for r in iter_records(self.journal.dir):
                if r["n"] <= base_n:
                    continue
                if r["k"] != "cycle":
                    if r["k"] not in NOT_REPLAYED and not (r["k"] == "risk" and r.get("event") in
                                                           ("tape_error", "recovery_mismatch")):
                        batch.append(r)
                    continue
                if r.get("fatal"):
                    batch = []
                    continue
                late = {(x["stream"], x["seq"]) for x in batch if x["k"] == "late"}
                self._capture = []
                self.cycle(marker=r, late_keys=late)
                regen, want = [strip(x) for x in self._capture], [strip(x) for x in batch]
                replayed += len(regen)
                if regen != want:
                    problems.append(f"cycle n={r['n']}: {_first_diff(regen, want)}")
                    break
                batch = []
        except TapeError as exc:
            problems.append(f"replay: {exc}")
        finally:
            self._capture = None
        if not problems:
            problems += self._cross_check(per_slug)
        if problems:
            self.journal.append([{"k": "risk", "t": st.cursor, "wall": self.clock(), "event": "recovery_mismatch",
                                  "problems": problems},
                                 {"k": "recovery", "t": st.cursor, "wall": self.clock(), "refused": True}], t_ns=None)
            raise RecoveryError("; ".join(problems))
        self.journal.append([{"k": "resume", "t": st.cursor, "wall": self.clock(), "from_checkpoint": base_n,
                              "replayed_records": replayed, "open_windows": sorted(st.windows)}], t_ns=None)

    def _cross_check(self, per_slug: dict[str, list[dict]]) -> list[str]:
        """Inventory, cash and resting orders of every open window, from the journal alone."""
        st = self.st
        assert st is not None
        out = []
        for slug, w in st.windows.items():
            recs = per_slug.get(slug, [])
            fills = [r for r in recs if r["k"] == "fill"]
            inv = fills[-1]["inv"] if fills else 0.0
            cash = fills[-1]["cash"] if fills else 0.0
            if abs(inv - w.sim.inv) > 1e-6 or abs(cash - w.sim.cash) > 1e-6:
                out.append(f"{slug}: journal inventory/cash {inv}/{cash} vs state {w.sim.inv}/{w.sim.cash}")
            if any(r.get("oid") is None for r in fills):
                out.append(f"{slug}: a journaled fill has no order id")
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
    return f"replay regenerated {len(a)} records, journal has {len(b)}"


def _json(x):
    return orjson.loads(orjson.dumps(x))


def load_genome(path: Path) -> Genome:
    return from_dict(orjson.loads(Path(path).read_bytes()))
