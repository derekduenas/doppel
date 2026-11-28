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
| `paper/cli.py` | `doppel-paper {run, agree, footprint, verify}` (also `python -m doppel.paper`). |

### Same strategy code

Decisions are `Features.at` → `Genome.maker_quotes` / `maker_paused` / `taker_actions`, called by
`WindowSim._on_decide` exactly as in replay. The venue is `WindowSim` itself, with its pessimistic
queue, post-only rejects, book-cross fills, IOC at the landed book, latency L, fee rounding and caps.
`PaperSim` calls `super()` in every override and only records the result. A live adapter replaces
only the venue (see below). Paper and twin can therefore disagree only when their inputs differ.

The engine settings (`SimConfig`: latency, BRTI delay, opening rule) are stored in the `run`
record and must not change for the life of a journal. `--opening` has no default. When the genome
has an evaluation-protocol binding in `registry/genomes.jsonl` (`--registry`, read only), every
simulation setting of the binding must match or the run is refused (exit 2); H1–H4 therefore run
only with `--opening received_raw`. A missing registry is also a refusal. Both opening rules are
covered by the agreement test. The opening SOURCE (computed or published) is fixed for a window by
its first decision that knew one, as the twin's rule fixes it; with paper's causal receive stamps it
cannot switch, and a switch would be counted in the window's `settle` record.

### Clock: tape time, never wall time

A cycle reads new frames and sets a watermark `W = min(cursor + 60 s, max(now − grace,
min over streams of newest tw))`. It feeds every record with `tw ≤ W` and runs all windows' events
`≤ W` in one time order. So decision second `d` runs once `W ≥ d` and sees exactly the records
received by `d`.

- `grace` defaults to 6 s. The Python recorder flushes a frame on an append at least 1 s after the
  last one, and every 5 s regardless; the Rust recorder flushes every 1 s.
- A record that arrives with `tw ≤ cursor` is journaled as `late` and known only from cursor + 1 ns
  on: its receive stamp (BRTI second, boundary row) is raised to that, so it never changes what an
  already-taken decision could have known.
- Each poll reads at most one `max_step` (60 s) of tape past the cursor and at most 64 MiB per stream;
  the watermark never passes what a capped stream has read.
- Raw BRTI comes from two causal sources: our own Kalshi poller on the tape (`ref_brti_live`,
  known at its `tw`) and PERELMAN's rows via `doppel-brti-relay` (`--perelman RELAY_DIR`, read
  only), each row known at max(PERELMAN's receive stamp, the relay's copy time `relay_tw`, the cycle
  clock that first read it). The imported
  `ref_brti_raw` tape stream is never read: its rows reach the tape up to 15 min late, so under the
  same rule they could never inform a decision.

### Risk

- **Genome limits.** `|inventory|`, notional per window, daily-loss stand-down and `max_open_windows`
  are enforced by the engine and by the twin's cross-window rules (`run_windows` order: start,
  horizon length, asset).
- **Kill switch.** Any `--kill-file` existing (default `JOURNAL/KILL`) halts at the cycle's starting
  cursor + 1 ns, before any decision of the cycle. Every working quote gets a cancel intent, every
  window stops deciding and no new window is opened. An IOC already in flight still lands; it
  cannot be recalled. The halt is committed once; a kill file left in place does not commit again.
- **Daily max-loss stop.** Today's (UTC) settled net plus the worst case of every open window
  ≤ −`max_loss_usd` (default `risk.max_loss_per_day`) halts the same way. Worst case = the worse
  outcome with every quote that can still fill (live or about to land) filled in full.
- **Stale book.** A WS book older than `max_book_age_s` (30 s) is not traded against: the window
  pulls its quotes and skips the decision (`skips: ["stale_book"]`). The twin has no such rule, so
  this is a deliberate paper-only divergence on a dead feed.
- **Tape failure.** A CRC or JSON error, or segment bytes that never become a complete frame within
  `stall_s` (30 s), are journaled (`risk: tape_error`) and stop the run with exit 3.

A halt is permanent for that journal: start a new journal to trade again. A halted run whose windows
have all settled exits 0.

### Journal

One JSON object per line with `n`, `prev` and `h = sha256(prev ‖ canonical body)`, in daily files
`journal-YYYYMMDD.jsonl` (UTC day of the batch's tape time) that form one chain. Any edit,
insertion, deletion or reorder, or a missing day file, breaks the chain (`doppel-paper verify`).
**The chain is not keyed**: it detects changes to what was written, but a writer with access to the
directory could rewrite the whole chain consistently. Anchor the head hash elsewhere (git, the
registry, an off-host log) for tamper evidence against such a writer. The runtime keeps only the
chain head and line count in memory; every reader streams.

Each cycle is written in one `write` + `fsync` and committed by its final `cycle` record. That
record holds the cycle's starting cursor `c0`, `W`, the clock reading, whether it newly applied the
kill, and the tail and PERELMAN offsets that advanced. A cycle is committed only when it wrote
records, saw a late event, read PERELMAN rows, newly applied the kill, or hit the 60 s heartbeat.
The other cycles are merged into the next committed one: replay first advances to `c0` with every
record read so far except those the committed cycle journaled as `late` (they were read only in it),
then runs the cycle. That is exact because a merged cycle had no late event, no PERELMAN row, no new
window, settlement or halt.

On open, lines after the last commit are cut, and a `recovery` record names their count and sha256.
A torn final line counts as a crash. Any other bad line is tampering, and the journal refuses to
open.

Record kinds:

| Kind | Holds |
|---|---|
| `run` | genome, config, settings, code sha |
| `open` | a window was created |
| `decide` | every decision second: `xh` = feature-vector hash, `sh` = engine-state hash (inventory, cash, live and pending quotes, taker bookkeeping), inventory; the full vector `x` when the decision produced an intent or a skip |
| `intent` | post, amend, cancel (with reason) or take |
| `ack` | live with queue ahead; rejected:post_only, no_book or closed; superseded; cancelled; filled; IOC miss reason |
| `fill` | role, side, price, quantity, fee, via, order id, inventory, cash |
| `skip` | stood_down, no_book, no_fair, paused, self_cross, risk_blocked |
| `risk` | halt_all, halt, max_loss, recovery_mismatch, tape_error |
| `settle` | outcome, net, gross, fees, contracts, opening source, stale-book seconds |
| `late`, `resume` | a late record; a completed recovery |

### Restart recovery

Every `checkpoint_s` (300 s) of tape time, a commit also pickles the runtime state into
`JOURNAL/checkpoints/`, and the `cycle` record pins the pickle's sha256 and format version inside
the chain. A pickle is loaded only if it matches, so a swapped file is never unpickled. Order ids
live on the quote objects, so they survive the pickle.

Recovery runs in six steps, all streaming:

1. Verify the chain and cut an uncommitted tail.
2. Refuse if the paper/twin code hash differs from the `run` record's, or the checkpoint format
   differs from this code's.
3. Load the newest valid checkpoint, or start from the `run` record if none is valid.
4. Re-run every committed cycle after it from the same tape and PERELMAN bytes, and require each
   cycle's regenerated records to equal its journaled batch.
5. Cross-check each open window's inventory, cash, resting orders and fill order ids against the
   journal's own records.
6. Continue.

Any mismatch writes `risk: recovery_mismatch` and refuses to start (exit 3). This covers a changed
tape, other settings or another genome.

## Where the twin knows more than paper can (G5 divergence sources)

The twin replays a finished tape. Seven of its inputs are not causal for a live process, and paper
adds one rule of its own. Paper uses
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
5. **Raw BRTI receive time.** The twin reads PERELMAN's rows from the tape import (`ref_brti_raw`)
   at PERELMAN's own receive stamp. Paper reads the relay's copy and knows a row at
   max(that stamp, `relay_tw`, the cycle clock that first read it), so it is later by the relay's
   (0.1 s) and paper's (0.25 s) poll intervals plus PERELMAN's write lag. Decisions near a BRTI second can differ; `agree` shows it as
   `inputs_identical_share` < 1. Without `--perelman`, paper has only `ref_brti_live` (5 s poller).
6. **Window exclusions.** The twin decides them after the fact (feed gaps, coverage, unsettled).
   Paper trades every window; `agree` lists such windows under `paper_only`.
7. **Daily stand-down.** At a window's start the previous window's outcome is not yet received, so
   paper counts it at its worst case. The twin uses the final net. Paper is therefore stricter.

8. **Stale book.** Paper pulls quotes on a WS book older than 30 s; the twin does not.

`agree` separates "inputs differed" (`inputs_identical_share`: feature vector AND engine state)
from "same inputs, different intent" (`same_input_diff_intent`). The second must always be 0, and
`agree` exits 1 otherwise, or when no window was compared. Shares are weighted by the larger of the
paper and twin counts. Windows the twin excludes are listed with paper's P&L there
(`paper_only_net_usd`). Windows decided long after their time (median wall − t > 120 s, a catch-up)
are reported apart from live ones (`by_mode`). The
synthetic test tape avoids all eight sources: one boundary value, no late prints, no unprinted
volume, live BRTI only. **The 100% agreement in the tests proves the plumbing, not live agreement.**

## Raw BRTI relay

PERELMAN's stream directory is private to its DynamicUser, and its permissions are left alone.
`doppel-brti-relay` (`paper/relay.py`) copies each complete line of the current `*.jsonl` (and of
each new hour file) into a file of the same name in a doppel-readable directory, adding only
`relay_tw` (ns, strictly increasing). Each batch is one append plus `fsync` per file. A partial line
waits for its newline. Progress is the output itself: the k-th output line is the k-th source
line, so a restart recounts the output (cutting a torn line) and never duplicates or skips. It has
no network, and its unit can see only that one directory under `/var/lib/private`.

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

1. `doppel-paper run --from-now --opening received_raw --perelman ...` for one H-family maker on
   real tape, for 2 hours. Then
   measure:
   - the wall-clock lag `wall − t` of `decide` records (expected ~grace + cycle);
   - `late` counts per stream;
   - CPU and RSS against the unit's caps.
2. Check the frame cadence of the **live** tape root on each stream the unit tails, to confirm the
   6 s grace. Do the same for `tape-rust` if the Rust recorder becomes the source.
3. `agree` after each settled hour, and record `inputs_identical_share`. Expect it below 1 from
   divergence 5. Decide whether G5 should run the twin with the same causal BRTI stamps. That would
   be a twin-side option, which is frozen until 2026-12-01.
4. The relay (`ops/systemd/doppel-brti-relay.service`): it reads PERELMAN's 0700/0600 stream with
   CAP_DAC_READ_SEARCH only, sees nothing else under `/var/lib/private`, and its output appears in
   `/mnt/.../doppel/perelman-relay` within ~0.1 s of PERELMAN's write. Check PERELMAN's file naming
   and rotation (`*.jsonl` → `*.jsonl.gz`); the relay does not read `.gz`, so an hour's tail is lost
   if the relay is down across that rotation.
5. Whether PM US sends WS book snapshots on a quiet book often enough for the 30 s stale-book bound.
6. Kill drill: `touch KILL` → a `halt_all` record within one cycle, cancels only afterwards, exit 0
   once every window settled.
7. Restart drill: `systemctl restart` mid-window, and across a BRTI import → a `resume` record, no
   `recovery_mismatch`.
8. Memory: RSS flat over 24 h; journal rotates at UTC midnight.
9. Footprint on a day of paper journal, then fillstore row counts.

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
- A causal-BRTI option for the twin (`ref_brti_live` only), for G5 (divergence 5).
- Kalshi: the twin replays PM US only, so paper refuses other venues. A Kalshi paper run needs a
  Kalshi twin on `lab/kalshi_ws.py` books first. Genomes with `intl_*` flow features are refused
  too, because the informedness scores are batch-computed.
