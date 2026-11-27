# DOPPEL paper runtime (Phase E1)

Status 2026-11-27: built on branch `pod/paper`, tested on synthetic live tape. Not deployed and not
installed. It places no orders: nothing in `src/doppel/paper/` opens a connection, reads a
credential or signs anything, and the systemd unit has no network at all.

## What it is

A sealed genome runs on the live tape, on paper, through **the twin's own engine**. It journals
every decision, and the journal is checked against a replay of the same genome over the same period.

```
recorder tape (read-only)          paper runtime                                   journal (append-only)
pmus_ws_5m / pmus_ws_15m  ──tail──► StreamTail ─► LiveWindow (WindowTape + WindowRef) ─► PaperSim ─► decide / intent / ack / fill
ref_brti_live / _raw      ──tail──►   (complete zstd frames,     built record by record      (twin.engine.WindowSim,   skip / risk / settle / late
ref_prices                ──tail──►    CRC-checked, offsets)      with the twin's parsers)    observed, not changed)    cycle (commit + checkpoint pin)
                                         │
                       kill file, max-loss stop ─► halt every window (cancel all, no new decisions)
```

| Module | Role |
|---|---|
| `paper/tail.py` | Read-only tail of `<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst`. Advances only past complete frames and checks every CRC. Keeps byte offsets per file. |
| `paper/inputs.py` | `LiveWindow` grows the `WindowTape` / `WindowRef` the twin would build: `ws_event`, gap control records, dedup, `[start-60 s, end+5 s)`, unprinted-volume inference, boundary revisions, BRTI with `_first_received`. |
| `paper/sim.py` | `PaperSim(WindowSim)` overrides only hook points to record what the engine did; `record_windows` = `run_windows` with `PaperSim` (pinned to `run_windows` output by a test). |
| `paper/runtime.py` | Cycle, watermark, window creation, cross-window rules, kill switch, max-loss stop, settlement, commit, checkpoints, restart recovery. |
| `paper/journal.py` | Hash-chained JSONL, batch commit, cut-and-trace of an interrupted batch. |
| `paper/agree.py` | G5 paper-vs-twin agreement. |
| `paper/footprint.py` | Journal → public footprint + full decision record. |
| `paper/cli.py` | `python -m doppel.paper {run, agree, footprint, verify}`. |

### Same strategy code

Decisions are `Features.at` → `Genome.maker_quotes` / `maker_paused` / `taker_actions`, called by
`WindowSim._on_decide` exactly as in replay. The venue is `WindowSim` itself, with its pessimistic
queue, post-only rejects, book-cross fills, IOC at the landed book, latency L, fee rounding and caps.
`PaperSim` calls `super()` in every override and only records the result. A live adapter replaces
only the venue (see below). Paper and twin can therefore disagree only when their inputs differ.

The engine settings (`SimConfig`: latency, BRTI delay, opening rule) are stored in the `run`
record and must not change for the life of a journal. Run a genome with its family's bound
evaluation protocol, for example `--opening received_raw` for H1–H4. Both rules are covered by the
agreement test.

### Clock: tape time, never wall time

A cycle reads new frames and sets a watermark `W = min(cursor + 60 s, max(now − grace,
min over streams of newest tw))`. It feeds every record with `tw ≤ W` and runs all windows' events
`≤ W` in one time order. So decision second `d` runs once `W ≥ d` and sees exactly the records
received by `d`.

- `grace` defaults to 6 s. The Python recorder flushes a frame on an append at least 1 s after the
  last one, and every 5 s regardless; the Rust recorder flushes every 1 s.
- A record that arrives with `tw ≤ W` after `W` passed it is journaled as `late` and used only from
  the next instant on, never retroactively.
- The 60 s cap only matters during catch-up.

### Risk

- **Genome limits.** `|inventory|`, notional per window, daily-loss stand-down and `max_open_windows`
  are enforced by the engine and by the twin's cross-window rules (`run_windows` order: start,
  horizon length, asset).
- **Kill switch.** Any `--kill-file` existing (default `JOURNAL/KILL`) halts at the previous
  watermark + 1 ns, before any decision of the cycle. Every working quote gets a cancel intent, the
  window stops deciding, and windows created later are born halted. An IOC already in flight still
  lands; it cannot be recalled.
- **Global max-loss stop.** Settled net plus the worst case of open windows
  (cash − fees + min(0, inventory)) ≤ −`max_loss_usd` (default `risk.max_loss_per_day`) halts the
  same way.

A halt is permanent for that journal: start a new journal to trade again.

### Journal

One JSON object per line with `n`, `prev` and `h = sha256(prev ‖ canonical body)`. Any edit,
insertion, deletion or reorder breaks the chain (`python -m doppel.paper verify`).

Each cycle is written in one `write` + `fsync` and committed by its final `cycle` record. That
record holds `W`, the clock reading, the kill flag and the tail offsets that advanced. A cycle is
committed only when it wrote records, saw a late event, saw the kill file, or hit the 60 s
heartbeat. Merging the other cycles is exact, because merging changes results only when something
is processed late.

On open, lines after the last commit are cut, and a `recovery` record names their count and sha256.
A torn final line counts as a crash. Any other bad line is tampering, and the journal refuses to
open.

Record kinds:

| Kind | Holds |
|---|---|
| `run` | genome, config, settings, code sha |
| `open` | a window was created |
| `decide` | every decision second: `xh` = feature-vector hash, inventory; the full vector `x` when the decision produced an intent or a skip |
| `intent` | post, amend, cancel (with reason) or take |
| `ack` | live with queue ahead; rejected:post_only, no_book or closed; superseded; cancelled; filled; IOC miss reason |
| `fill` | role, side, price, quantity, fee, via, order id, inventory, cash |
| `skip` | stood_down, no_book, no_fair, paused, self_cross, risk_blocked |
| `risk` | halt_all, halt, max_loss, recovery_mismatch |
| `settle` | outcome, net, gross, fees, contracts |
| `late`, `resume` | a late record; a completed recovery |

### Restart recovery

Every `checkpoint_s` (300 s) of tape time, a commit also pickles the runtime state into
`JOURNAL/checkpoints/`, and the `cycle` record pins the pickle's sha256 inside the chain. A pickle
is loaded only if it matches, so a swapped file is never unpickled.

Recovery runs in five steps:

1. Verify the chain and cut an uncommitted tail.
2. Load the newest valid checkpoint, or start from the `run` record if none is valid.
3. Re-run every committed cycle after it, from the same tape bytes (offsets).
4. Require the regenerated records to equal the journal's, and cross-check each open window's
   inventory, cash and resting orders against the journal's own fill and ack records.
5. Continue.

Any mismatch writes `risk: recovery_mismatch` and refuses to start (exit 3). This covers a changed
tape, other settings or another genome.

## Where the twin knows more than paper can (G5 divergence sources)

The twin replays a finished tape. Seven of its inputs are not causal for a live process. Paper uses
what it had actually received, and `agree` reports the effect:

1. **Book stream.** Per window, the twin uses the WS stream with the most snapshots over the whole
   window. Paper uses the per-horizon stream (`pmus_ws_5m` / `pmus_ws_15m`). Legacy `pmus_ws` is not
   tailed.
2. **Private fill channel.** The twin processes a print at `min(receive, tradeTime + L)`. Paper learns
   it on receipt, so a print delivered late, or an inferred unprinted execution (dated just after
   the previous book), is processed late. Both are counted as `late` on the window.
3. **Unprinted-volume inference.** The twin also counts prints received after the book. Paper counts
   only prints already received.
4. **Settlement value.** The twin uses the last boundary revision. Paper uses the one it had when
   both boundaries were known.
5. **Imported raw BRTI** (`ref_brti_raw`, every 15 min on Apex). It carries receive stamps earlier
   than its arrival on the tape. The twin's fair value uses them; paper's past decisions could not.
   Live, paper's fair value is effectively `ref_brti_live` only. *This is the divergence most
   likely to dominate real G5 numbers.*
6. **Window exclusions.** The twin decides them after the fact (feed gaps, coverage, unsettled).
   Paper trades every window; `agree` lists such windows under `paper_only`.
7. **Daily stand-down.** At a window's start the previous window's outcome is not yet received, so
   paper counts it at its worst case. The twin uses the final net. Paper is therefore stricter.

`agree` separates "inputs differed" (`inputs_identical_share`) from "same inputs, different intent"
(`same_input_diff_intent`). The second must always be 0, and `agree` exits 1 otherwise. The
synthetic test tape avoids all seven sources: one boundary value, no late prints, no unprinted
volume, live BRTI only. **The 100% agreement in the tests proves the plumbing, not live agreement.**

## Footprint for the reconstruction machine (G3)

`python -m doppel.paper footprint --journal J --out D` writes four things, in the answer-key lab's
layout:

- `D/public/orders.jsonl`: `ordertape.SCHEMA` rows for filled orders, one complete call per fill.
  This is what `ordermatch` and `rulefit --orders` read.
- `D/public/goldsky` → `D/public/fills/`: via `fillstore.compact`.
- `D/truth/decisions.jsonl`: every journal record.
- `D/truth/orders.jsonl`: every order, filled or not, with its life.

PM US has one YES instrument, so BUY YES at p maps to BUY token `pmus:<slug>:up` at p, and SELL YES
at p maps to BUY `pmus:<slug>:down` at 1 − p. Tested: the order rows load through
`ordermatch.load_orders`, and `filled_shares` gives back each journal fill.

**Not built.** `ordermatch` and `rulefit` locate orders in recorded intl CLOB books. Paper quotes rest
in a PM US book, so placement matching needs a PM US book overlay like the answer key's rewritten
tape.

## Validate on Apex (in order)

1. `python -m doppel.paper run --from-now` for one H-family maker on real tape, for 2 hours. Then
   measure:
   - the wall-clock lag `wall − t` of `decide` records (expected ~grace + cycle);
   - `late` counts per stream;
   - CPU and RSS against the unit's caps.
2. Check the frame cadence of the **live** tape root on each stream the unit tails, to confirm the
   6 s grace. Do the same for `tape-rust` if the Rust recorder becomes the source.
3. `agree` after each settled hour, and record `inputs_identical_share`. Expect it below 1 from
   divergence 5 whenever the BRTI import filled seconds the live poller missed. Decide whether G5
   should run the twin with `ref_brti_live` only. That would be a twin-side option, which is frozen
   until 2026-12-01.
4. Kill drill: `touch KILL` → a `halt_all` record within one cycle, cancels only afterwards.
5. Restart drill: `systemctl restart` mid-window → a `resume` record, no `recovery_mismatch`.
6. Footprint on a day of paper journal, then fillstore row counts.

## Interfaces a future live adapter implements (documented, not built)

The live path swaps only the venue. Features, genome expression, the cross-window rules, risk and
the journal stay the same. Four interfaces:

```python
class VenueAdapter(Protocol):
    """Implemented by paper (PaperSim's engine) and, later, by a riskd-gated live client."""
    def post(self, slug: str, side: Literal["bid", "ask"], price: float, size: float,
             client_oid: str, decided_ns: int) -> None: ...      # post-only, YES instrument
    def replace(self, slug: str, side: str, old_oid: str, price: float, size: float,
                client_oid: str, decided_ns: int) -> None: ...   # cancel + post; old stays live until acked
    def cancel(self, slug: str, side: str, oid: str, decided_ns: int, why: str) -> None: ...
    def take(self, slug: str, side: Literal["buy", "sell"], qty: float, limit: float | None,
             client_oid: str, decided_ns: int) -> None: ...      # IOC
    def cancel_all(self, why: str) -> None: ...                  # kill switch / max loss

class VenueEvents(Protocol):
    """What the venue reports back; each becomes one journal record (ack / fill)."""
    def on_ack(self, oid: str, status: str, t_ns: int, queue: float | None) -> None: ...
    def on_fill(self, oid: str, px: float, qty: float, fee: float, t_ns: int, role: str) -> None: ...

class PositionSource(Protocol):
    """Reconciliation: venue truth vs the journal, every cycle and on restart."""
    def positions(self) -> dict[str, float]: ...                 # slug -> net YES
    def open_orders(self) -> dict[str, list[tuple[str, str, float, float]]]: ...  # slug -> (oid, side, px, remaining)
    def cash(self) -> float: ...

class Arming(Protocol):
    """Owner arming: live orders only while an owner-signed arming record is valid (riskd)."""
    def armed(self, genome_id: str, now_ns: int) -> bool: ...
```

Mapping to today's engine:

| Interface call | Engine hook |
|---|---|
| `post` / `replace` / `cancel` | `WindowSim._schedule_quote` |
| `take` | the `"ioc"` push |
| `on_ack` | `_on_quote_land` / `_on_ioc` |
| `on_fill` | `_fill` |
| `cancel_all` | `PaperSim.halt` |

Reconciliation uses the same journal-derived `open_orders()` and inventory that restart recovery
already checks. Live market data must come from the WebSocket directly, not the tape, because the
tape's 1–5 s flush lag is fine for paper and fatal live.

## Requested changes outside this package (not made: scoring freeze)

- `twin.engine.run_windows(..., sim_cls=WindowSim)` and named hook methods (`on_intent`, `on_ack`,
  `on_fill`) in `WindowSim`. `PaperSim` currently overrides private methods (`_schedule_quote`,
  `_push`, `_on_quote_land`, `_on_ioc`, `_maker_fill`, `_fill`, `_maker`, `_on_decide`), and
  `record_windows` mirrors `run_windows`. A test pins both to the engine, but a hook API would remove
  the mirror.
- `pyproject.toml`: `doppel-paper = "doppel.paper.cli:main"`. Until then, use
  `python -m doppel.paper`.
- A causal-BRTI option for the twin (`ref_brti_live` only), for G5 (divergence 5).
- Kalshi: the twin replays PM US only, so paper refuses other venues. A Kalshi paper run needs a
  Kalshi twin on `lab/kalshi_ws.py` books first. Genomes with `intl_*` flow features are refused
  too, because the informedness scores are batch-computed.
