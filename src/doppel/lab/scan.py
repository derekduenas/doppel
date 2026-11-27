"""Wallet activity scan over the live RTDS identity tape, restricted to crypto up/down windows,
plus a frozen, activity-selected study cohort.

Selection never looks at profit, so choosing the cohort cannot leak outcomes. Candidates are the
most active wallets by window notional. Controls are a seeded random sample of wallets in a lower
activity band. The cohort file is written once with its selection rule, input span and sha256.
Later stages (P&L, skill) score the frozen cohort on what happens next.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import re
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import orjson

from ..tape.segment import atomic_write, read_segment, segment_path

WINDOW = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m)-(\d+)$")
LEN = {"5m": 300, "15m": 900}
STREAMS = ("rtds_a", "rtds_b")
NS = 1_000_000_000


def iter_fills(root: Path, streams=STREAMS, since: int = 0, *, until_tw: int | None = None,
               seals: dict[str, str] | None = None):
    """Union by maximum within-socket multiplicity over the observation interval."""
    from .flow import parse_rtds

    end = time.time_ns() if until_tw is None else until_tw
    emitted: Counter = Counter()
    for s in streams:
        seen: Counter = Counter()
        paths = (sorted((root / s).glob("*/*.dtape.zst")) if seals is None else
                 [root / key for key in seals if key.startswith(s + "/")])
        for f in paths:
            key_path = f.relative_to(root).as_posix()
            before = f.stat()
            if seals is not None:
                with f.open("rb") as fh:
                    if hashlib.file_digest(fh, "sha256").hexdigest() != seals[key_path]:
                        raise ValueError(f"RTDS segment differs from seal: {key_path}")
            for rec in read_segment(f, verify=True, strict=seals is not None):
                if not since * NS <= rec["tw"] < end:
                    continue
                raw = rec["raw"]
                parsed = parse_rtds(raw, rec["tw"])
                if parsed is None:
                    if seals is not None and "-updown-" in raw:
                        obj = orjson.loads(raw)
                        payload = obj.get("payload") if isinstance(obj, dict) else None
                        if isinstance(payload, dict) and WINDOW.fullmatch(str(payload.get("eventSlug", ""))):
                            raise ValueError(f"invalid wallet trade in {key_path}")
                    continue
                key, trade = parsed
                if trade.ts < since or trade.ts * NS >= end:
                    continue
                if not (0 <= trade.price <= 1 and math.isfinite(trade.size) and trade.size > 0
                        and math.isfinite(trade.fee)):
                    raise ValueError(f"invalid wallet trade amounts in {key_path}")
                seen[key] += 1
                if seen[key] <= emitted[key]:
                    continue
                emitted[key] = seen[key]
                p = orjson.loads(raw)["payload"]
                yield {**p, "timestamp": trade.ts, "side": trade.side, "fee": trade.fee,
                       "_asset": trade.coin, "_hz": trade.hz, "_tte": trade.end - trade.ts}
            if seals is not None:
                after = f.stat()
                if (before.st_ino, before.st_ctime_ns) != (after.st_ino, after.st_ctime_ns):
                    raise ValueError(f"RTDS segment changed during scan: {key_path}")


def _sealed_range(root: Path, since: int, until: int | None, now: int):
    from .flow import tape_hours

    # Unbounded historical archives end at their last retained complete hour. Explicit
    # bounds must be present on both sockets in every requested hour, including idle hours.
    hours = [h for h in tape_hours(root, STREAMS) if h + 3600 <= now // NS]
    if not hours:
        raise ValueError("no closed RTDS hours available")
    lo = since or hours[0]
    hi = until if until is not None else hours[-1] + 3600
    if not 0 <= lo < hi or hi > now // NS:
        raise ValueError("invalid RTDS scan interval")
    seals = {}
    for stream in STREAMS:
        man = root / stream / "manifest.jsonl"
        if not man.is_file():
            raise ValueError(f"missing RTDS manifest: {stream}")
        entries = {m["file"]: m for m in map(orjson.loads, man.read_bytes().split(b"\n")[:-1])}
        for hour in range(lo // 3600 * 3600, hi, 3600):
            f = segment_path(root, stream, hour * NS)
            entry = entries.get(f.relative_to(root / stream).as_posix())
            if hour + 3600 > now // NS or not entry or not f.is_file() or f.stat().st_size != entry["bytes"]:
                raise ValueError(f"missing complete RTDS segment: {f}")
            seals[f.relative_to(root).as_posix()] = entry["sha256"]
    return lo, hi * NS, seals


def scan(root: Path, since: int = 0, *, until: int | None = None, sealed: bool = False) -> dict:
    now = time.time_ns()
    end = now if until is None else until * NS
    if not 0 <= since * NS < end <= now:
        raise ValueError("invalid RTDS scan interval")
    seals = None
    if sealed:
        since, end, seals = _sealed_range(root, since, until, now)
    w = defaultdict(lambda: {"fills": 0, "taker": 0, "maker": 0, "usd": 0.0, "taker_usd": 0.0,
                             "fees": 0.0, "buys": 0, "windows": set(), "assets": Counter(),
                             "hz": Counter(), "tte_taker": [], "tte_maker": [], "px": [],
                             "outcomes": defaultdict(set), "first": None, "last": None,
                             "name": None})
    span = [None, None]
    for p in iter_fills(root, since=since, until_tw=end, seals=seals):
        a = w[p["proxyWallet"].lower()]
        usd = float(p["price"]) * float(p["size"])
        taker = (p.get("fee") or 0) > 0
        a["fills"] += 1
        a["usd"] += usd
        a["fees"] += float(p.get("fee") or 0)
        if taker:
            a["taker"] += 1
            a["taker_usd"] += usd
            a["tte_taker"].append(p["_tte"])
        else:
            a["maker"] += 1
            a["tte_maker"].append(p["_tte"])
        a["buys"] += p["side"] == "BUY"
        a["windows"].add(p["eventSlug"])
        a["assets"][p["_asset"]] += 1
        a["hz"][p["_hz"]] += 1
        a["px"].append(float(p["price"]))
        if p["side"] == "BUY":
            a["outcomes"][p["eventSlug"]].add(p.get("outcomeIndex"))
        ts = p["timestamp"]
        a["first"] = ts if a["first"] is None else min(a["first"], ts)
        a["last"] = ts if a["last"] is None else max(a["last"], ts)
        a["name"] = a["name"] or p.get("name") or p.get("pseudonym")
        span[0] = ts if span[0] is None else min(span[0], ts)
        span[1] = ts if span[1] is None else max(span[1], ts)
    out = {}
    for addr, a in w.items():
        px = a["px"]
        out[addr] = {
            "name": a["name"], "fills": a["fills"], "taker_fills": a["taker"],
            "maker_fills": a["maker"], "usd": round(a["usd"], 2),
            "taker_share": round(a["taker_usd"] / a["usd"], 4) if a["usd"] else None,
            "fees": round(a["fees"], 4), "buy_share": round(a["buys"] / a["fills"], 4),
            "n_windows": len(a["windows"]), "assets": dict(a["assets"]), "horizons": dict(a["hz"]),
            "both_sides_windows": sum(1 for v in a["outcomes"].values() if {0, 1} <= v),
            "tte_taker_p50": st.median(a["tte_taker"]) if a["tte_taker"] else None,
            "tte_maker_p50": st.median(a["tte_maker"]) if a["tte_maker"] else None,
            "px_p50": round(st.median(px), 4),
            "extreme_px_share": round(sum(1 for x in px if x >= 0.9 or x <= 0.1) / len(px), 4),
            "first": a["first"], "last": a["last"],
        }
    return {"span": span, "since": since, "as_of_tw": end, "built_tw": time.time_ns(),
            "segments": seals, "wallets": out}


def scan_digest(scan_doc: dict) -> str:
    return hashlib.sha256(orjson.dumps(scan_doc, option=orjson.OPT_SORT_KEYS)).hexdigest()


def cohort_digest(doc: dict) -> str:
    ignored = {"sha256"}
    if "schema_version" not in doc:
        # The original frozen cohorts committed membership and span, but not publication time.
        ignored.add("frozen_tw")
    elif type(doc["schema_version"]) is not int or doc["schema_version"] != 2:
        raise ValueError("unknown cohort schema version")
    return scan_digest({k: v for k, v in doc.items() if k not in ignored})


def validate_cohort(doc: dict) -> None:
    if not isinstance(doc, dict):
        raise ValueError("cohort must be a JSON object")
    if doc.get("sha256") != cohort_digest(doc):
        raise ValueError("cohort checksum mismatch")
    rule = doc.get("rule") or {}
    if not isinstance(rule, dict):
        raise ValueError("invalid cohort selection rule")
    groups = []
    for key, count in (("candidates", "n_candidates"), ("controls", "n_controls")):
        members = doc.get(key)
        if (not isinstance(members, list) or not members or len(members) != rule.get(count)
                or any(not isinstance(w, str) or not re.fullmatch(r"0x[0-9a-f]{40}", w) for w in members)
                or len(set(members)) != len(members)):
            raise ValueError(f"invalid or incomplete cohort {key}")
        groups.append(set(members))
    if groups[0] & groups[1]:
        raise ValueError("candidate and control memberships overlap")
    span, frozen = doc.get("span"), doc.get("frozen_tw")
    if (not isinstance(span, list) or len(span) != 2 or any(type(t) is not int for t in span)
            or type(frozen) is not int or not 0 <= span[0] <= span[1] or span[1] * NS > frozen):
        raise ValueError("invalid cohort span or freeze time")
    if doc.get("schema_version") == 2:
        cutoff = doc.get("as_of_tw")
        if (type(cutoff) is not int or not span[1] * NS < cutoff <= frozen
                or not re.fullmatch(r"[0-9a-f]{64}", str(doc.get("scan_sha256", "")))):
            raise ValueError("invalid observation cutoff or scan digest")


def rank_members(scan_doc: dict, n_cand=100, n_ctrl=50, min_windows=5, ctrl_band=(101, 1000),
                 seed=20261125) -> tuple[int, list[str], list[str]]:
    if (not isinstance(ctrl_band, (tuple, list)) or len(ctrl_band) != 2
            or any(type(v) is not int for v in (*ctrl_band, n_cand, n_ctrl, min_windows))
            or not 0 < n_cand < ctrl_band[0] <= ctrl_band[1] or n_ctrl < 1 or min_windows < 1):
        raise ValueError("candidate/control bands must be positive and disjoint")
    ws = [(a, v) for a, v in scan_doc["wallets"].items() if v["n_windows"] >= min_windows]
    ws.sort(key=lambda kv: (-kv[1]["usd"], kv[0]))
    cands = [a for a, _ in ws[:n_cand]]
    band = [a for a, _ in ws[ctrl_band[0] - 1:ctrl_band[1]]]
    if len(cands) < n_cand or len(band) < n_ctrl:
        raise ValueError("insufficient eligible wallets to fill the candidate and control pools")
    ctrls = sorted(random.Random(seed).sample(band, n_ctrl))
    return len(ws), cands, ctrls


def select_cohort(scan_doc: dict, n_cand=100, n_ctrl=50, min_windows=5, ctrl_band=(101, 1000),
                  seed=20261125) -> dict:
    if not scan_doc.get("segments") or not scan_doc.get("as_of_tw"):
        raise ValueError("freezing requires a scan of verified, closed RTDS segments")
    eligible, cands, ctrls = rank_members(scan_doc, n_cand, n_ctrl, min_windows, ctrl_band, seed)
    rule = {"metric": "crypto-window notional (price*size) on RTDS union, profit-blind",
            "min_windows": min_windows, "n_candidates": n_cand, "control_band_ranks": ctrl_band,
            "n_controls": n_ctrl, "seed": seed}
    body = {"schema_version": 2, "rule": rule, "span": scan_doc["span"], "eligible": eligible,
            "candidates": cands, "controls": ctrls, "scan_sha256": scan_digest(scan_doc),
            "as_of_tw": scan_doc["as_of_tw"], "frozen_tw": time.time_ns()}
    cohort = {**body, "sha256": cohort_digest(body)}
    validate_cohort(cohort)
    return cohort


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-scan")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--freeze-cohort", type=Path, help="write cohort file (refuses to overwrite)")
    ap.add_argument("--from", dest="since", type=int, default=0, help="inclusive UTC Unix seconds")
    ap.add_argument("--to", dest="until", type=int, help="exclusive UTC Unix seconds, both event and receive clocks")
    a = ap.parse_args(argv)
    if a.freeze_cohort and (a.freeze_cohort.resolve() == a.out.resolve() or
            a.freeze_cohort.exists() and a.out.exists() and a.freeze_cohort.samefile(a.out)):
        ap.error("scan and cohort outputs must be distinct")
    try:
        if a.freeze_cohort and a.freeze_cohort.exists():
            raise FileExistsError(a.freeze_cohort)
        replace_scan = a.out.is_file() and not a.freeze_cohort
        if replace_scan:
            # A later diagnostic invocation may point its --out at a previous freeze by mistake.
            existing = orjson.loads(a.out.read_bytes())
            if isinstance(existing, dict) and ("frozen_tw" in existing or existing.get("segments")):
                raise ValueError(f"refusing to replace a frozen cohort or sealed scan receipt: {a.out}")
        doc = scan(a.data, since=a.since, until=a.until, sealed=bool(a.freeze_cohort))
        cohort = select_cohort(doc) if a.freeze_cohort else None
        # A successful freeze always references a durable, uniquely named scan receipt.
        atomic_write(a.out, orjson.dumps(doc, option=orjson.OPT_INDENT_2), replace=replace_scan)
        if a.freeze_cohort and cohort:
            atomic_write(a.freeze_cohort, orjson.dumps(cohort, option=orjson.OPT_INDENT_2), replace=False)
    except (ValueError, OSError) as exc:
        print(f"doppel-scan: {exc}", file=sys.stderr)
        return 1
    ws = doc["wallets"]
    tot = sum(v["usd"] for v in ws.values())
    print(f"span {doc['span']} wallets {len(ws)} window notional ${tot:,.0f}")
    if cohort:
        print(f"cohort frozen sha256={cohort['sha256'][:16]} eligible={cohort['eligible']} "
              f"candidates={len(cohort['candidates'])} controls={len(cohort['controls'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
