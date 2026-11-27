"""Point-in-time listing evidence and a wallet-independent schedule of market observations."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import orjson

from ..tape.segment import read_segment, segment_path
from ..tape.universe import ASSETS, HORIZONS, intl_slug
from .decision_book import GRID_S
from .flow import NS, RTDS_STREAMS, parse_rtds
from .goldsky_proof import digest



def _inputs(root, stream, start, end, sealed):
    base = Path(root) / stream
    seals = {}
    if sealed and (base / "manifest.jsonl").exists():
        for line in (base / "manifest.jsonl").read_bytes().splitlines():
            if line:
                seal = orjson.loads(line)
                seals[seal["file"]] = seal
    sources, missing = [], []
    for hour in range(start - start % 3600, end, 3600):
        path = segment_path(root, stream, hour * NS)
        name = path.relative_to(base).as_posix()
        if not path.is_file():
            missing.append(f"{stream}/{name}")
            continue
        before = path.stat()
        sha = None
        if sealed:
            seal = seals.get(name)
            if not seal or seal["bytes"] != before.st_size or (sha := digest(path)) != seal["sha256"]:
                raise ValueError(f"unsealed market opportunity source {stream}/{name}")
        sources.append((hour, path, before, {"file": f"{stream}/{name}", "bytes": before.st_size, "sha256": sha}))
    return sources, missing


def _unchanged(path, before):
    after = path.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError(f"market opportunity source changed while reading {path}")


def merge_identity(tokens: dict, token: str, info: dict) -> None:
    """A label may be learned earlier from another feed, but cannot identify another market or side."""
    old = tokens.get(token)
    if old is not None:
        if any(old[k] != info[k] for k in ("slug", "idx")):
            raise ValueError(f"conflicting public market identity for token {token}")
        known = old.get("known_tw_ns")
        if known is not None and (info.get("known_tw_ns") is None or known <= info["known_tw_ns"]):
            return
    tokens[token] = info


def public_listings(root, start, end, *, exclude_wallets=(), exclude_txs=(), require_seals=False):
    """First positive RTDS witness of each token's window and outcome, by local receipt time.

    A missing hour or prior quiet market limits the earliest available witness. It never
    establishes that the market was unlisted. Duplicate feeds corroborate the same identity.
    """
    root = Path(root)
    excluded = {w.lower() for w in exclude_wallets}
    txs = {tx.lower() for tx in exclude_txs}
    tokens, receipts, missing = {}, [], []
    for stream in RTDS_STREAMS:
        sources, holes = _inputs(root, stream, start, end, require_seals)
        missing.extend(holes)
        for hour, path, before, receipt in sources:
            last_tw = None
            for rec in read_segment(path, verify=True, strict=True):
                tw, seq = rec["tw"], rec["seq"]
                if (rec.get("stream") != stream or type(tw) is not int or type(seq) is not int
                        or seq < 1 or not hour * NS <= tw < (hour + 3600) * NS):
                    raise ValueError(f"invalid RTDS receipt scope in {path}")
                if (rec.get("meta") or {}).get("ctl") == "receive_time_reversal" or (last_tw is not None and tw < last_tw):
                    raise ValueError(f"non-monotonic receive clock in {path}")
                last_tw = tw
                if not start * NS <= tw < end * NS:
                    continue
                parsed = parse_rtds(rec.get("raw") or "", tw)
                if parsed is None:
                    continue
                key, f = parsed
                if not f.wallet or not key[0] or not f.token or f.wallet in excluded or key[0] in txs:
                    continue
                merge_identity(tokens, f.token, {"slug": f.slug, "asset": f.coin, "hz": f.hz,
                               "start": f.start, "end": f.end, "idx": f.idx, "known_tw_ns": tw,
                               "witness_source": stream, "witness_seq": seq})
            _unchanged(path, before)
            receipts.append(receipt)
    return tokens, {"sources": receipts, "missing": missing}


def observable_identity(info, tw):
    known = info.get("known_tw_ns")
    return type(known) is int and 0 <= known < tw


def opportunity_panel(start, end, pairs, tokens, grid, grid_integrity):
    """Enumerate every full absolute ten-second cell in the declared market families.

    Listing and the raw quote at each cutoff are strictly earlier features. The separate
    retrospective integrity field can exclude a training observation when capture failed.
    """
    from .rulefit import up_view

    by_slug = defaultdict(list)
    for token, info in tokens.items():
        by_slug[info["slug"]].append((info["idx"], token, info))
    for slug, entries in by_slug.items():
        if len({idx for idx, _, _ in entries}) != len(entries):
            raise ValueError(f"multiple tokens for the same market outcome in {slug}")
    cells = []
    for asset, hz in sorted(set(pairs)):
        if asset not in ASSETS or hz not in HORIZONS:
            raise ValueError("unknown market family")
        length = HORIZONS[hz]
        for window in range(start - start % length, end, length):
            slug = intl_slug(asset, hz, window)
            opening = max(window, start)
            first = opening + (-opening) % GRID_S
            for b in range(first, min(end, window + length) - GRID_S + 1, GRID_S):
                known = sorted((idx, t) for idx, t, info in by_slug[slug] if observable_identity(info, b * NS))
                row = {"slug": slug, "window_start": window, "window_end": window + length, "cell_start": b,
                       "identity_observed": bool(known), "book_state": "unknown", "book_token": None,
                       "mid_c": None, "capture_integrity": None, "eligible": False}
                for idx, token in known:
                    key = (slug, b, idx)
                    view = up_view(grid.get(key), idx)
                    bb, ba = view["bb_c"], view["ba_c"]
                    if bb is None or ba is None or bb > ba:
                        if row["book_state"] == "unknown" and grid.get(key) is not None:
                            row["book_state"] = "one_sided" if bb is None or ba is None else "crossed"
                        continue
                    # Keep the first causally available two-sided book. Later capture integrity
                    # cannot retrospectively select a different token's quotes at this cutoff.
                    integrity = grid_integrity.get(key, "right_censored")
                    row.update(book_state="two_sided", book_token=token, mid_c=view["mid_c"],
                               capture_integrity=integrity, eligible=integrity == "covered")
                    break
                cells.append(row)
    return sorted(cells, key=lambda row: (row["cell_start"], row["slug"]))


def study_panel(root, start, end, pairs, activity, exclude_txs=(), *, require_seals=True):
    """Public opportunity and reconciled fill observations for a fixed retrospective interval.

    activity maps each wallet to its full reconciled per-slug fill counts for [start,end),
    or None when its economic coverage is unknown. Each returned cell is a cutoff sample.
    """
    from .rulefit import scan

    root, pairs = Path(root), sorted(set(pairs))
    if not pairs or end <= start:
        raise ValueError("expected market families and an increasing interval")
    for counts in activity.values():
        if counts is not None and any(type(v) is not int or v < 0 for v in counts.values()):
            raise ValueError("invalid reconciled fill counts")
    warmup = start - max(HORIZONS[hz] for _, hz in pairs)
    tokens, evidence = public_listings(root, warmup, end, exclude_wallets=activity, exclude_txs=exclude_txs,
                                       require_seals=require_seals)
    grid, integrity = {}, {}
    for asset, hz in pairs:
        stream = f"clob_{asset}_{hz}"
        inputs, missing = _inputs(root, stream, warmup, end, require_seals)
        evidence["missing"].extend(missing)
        by_start = defaultdict(list)
        for token, info in tokens.items():
            if (info["asset"], info["hz"]) == (asset, hz):
                by_start[info["start"]].append((token, info["slug"], info["idx"]))
        got = scan(root, stream, warmup, end, [], {}, set(), by_start, HORIZONS[hz], grid_from=start)
        grid.update(got["grid"])
        integrity.update(got["grid_integrity"])
        for _, path, before, receipt in inputs:
            _unchanged(path, before)
            evidence["sources"].append(receipt)
    if any((root / name).exists() for name in evidence["missing"]):
        raise ValueError("market opportunity source appeared during replay")
    cells = opportunity_panel(start, end, pairs, tokens, grid, integrity)
    by_slug = defaultdict(list)
    for c in cells:
        by_slug[c["slug"]].append(c)
    windows = []
    for asset, hz in pairs:
        length = HORIZONS[hz]
        for w in range(start - start % length, end, length):
            slug = intl_slug(asset, hz, w)
            group = by_slug[slug]
            covered = sum(c["eligible"] for c in group)
            listing = [dict(token=t, **i) for t, i in tokens.items() if i["slug"] == slug]
            row = {"slug": slug, "window_start": w, "window_end": w+length,
                   "scheduled_cells": len(group), "listed_cells": sum(c["identity_observed"] for c in group),
                   "eligible_cells": covered, "witnesses": sorted(listing, key=lambda i: (i["known_tw_ns"], i["token"])),
                   "wallets": {}}
            for wallet, counts in activity.items():
                n = counts.get(slug, 0) if counts is not None else None
                observation = ("unknown_fill_coverage" if n is None else "observed_fill" if n
                               else "no_observed_fill_on_covered_grid" if covered and covered == len(group)
                               else "no_observed_fill_in_partially_observed_market" if covered
                               else "no_observed_fill_market_unobservable")
                row["wallets"][wallet] = {"reconciled_fills": n, "observation": observation}
            windows.append(row)
    scheduled = {w["slug"] for w in windows}
    return {"from": start, "to": end, "cell_s": GRID_S, "warmup_from": warmup, "sources": evidence,
            "scope": pairs, "windows": windows, "cells": cells,
            "fills_outside_scheduled_windows": {w: None if counts is None else {s:n for s,n in counts.items()
                                               if s not in scheduled and n} for w, counts in activity.items()},
            "interpretation": "Reconciled fills are retrospective labels. Missing wallet fills in an observed market "
                              "leave posting, cancellation and deliberate nonparticipation unknown."}
