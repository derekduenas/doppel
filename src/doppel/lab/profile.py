"""Wallet profile: software fingerprint and reconstruction ledger from decoded signed orders.

Input is ``doppel-ordertape`` output (cohort parquet directory, or the older JSONL sample), and
optionally ``ordermatch.json`` (``doppel-ordermatch``) and a directory of dossiers
(``doppel-dossier``, ``<wallet>.json``). No network. A wallet is the lowercased ``maker`` field.

WHICH ROWS COUNT
- Parquet (has a ``verified`` column): only rows with ``verified == True`` are used (the order's
  EIP-712 hash is an ``order_id`` the fill store reports in that tx). False and null rows are
  counted and left out.
- Legacy JSONL (no ``verified`` column): every row is used and the ledger carries
  ``unflagged_input: true``. The rows are still decoded from executed calldata; they were not
  re-checked against the fill store by this file. ``ts_flag`` is recomputed when absent.
- Rows are per fill; statistics about orders use one record per ``order_id`` (its first row by
  block time gives the role and ``age_s``).
- Only FILLED orders are on chain. Gaps between creations bound the re-post cadence from above,
  ``age_s`` is time to a fill (not resting time), and sizes/prices are those of quotes that
  traded. Nothing here sees a quote that never filled; only ordermatch estimates those.

A. SOFTWARE FINGERPRINT (evidence of shared software or operator, never proof)
Per wallet, from orders with a plausible stamp unless stated:
- salt: digit-length histogram and a kind, tested in this order on (salt, created_ms) pairs
  (needs >= 30; else ``insufficient``):
  ``timestamp_{ns,us,ms,s}``  >= 90 % of salts within 5 s of the order's own stamp in that unit;
  ``counter``                 >= 90 % of consecutive orders (distinct stamps) have a larger salt;
  ``uniform_below_now_ms``    >= 99 % of salts <= created_ms and median(salt / created_ms) in
                              [0.25, 0.75];
  ``uniform_below_now_s``     the same with created_ms / 1000;
  ``other_le_{32,53,64,128,256}bit``  none of the above; the bucket holds the largest salt.
  Digit length alone does not discriminate (uniform below 1.8e12 is ~44 % 13-digit, ~50 %
  12-digit); the ratio to the stamp does. Stability: the orders are cut into 4 equal parts in
  creation order, each with >= 30 pairs is classified, ``stable`` = they all agree (null when
  fewer than 2 parts classify).
- timestamp: granularity = the coarsest of 1000 / 100 / 10 ms that >= 99 % of stamps are a
  multiple of, else 1 (all orders with a positive stamp); shares by ``ts_flag``.
- signing: sig_type shares, maker != signer share, builder shares.
- size grammar (maker orders): the fewest sizes (2 dp) covering 90 %, integer-size share, cv of
  size and of notional (size * limit); rule = ``fixed_sizes`` (<= 8 sizes cover 90 %), else
  ``fixed_notional`` (cv of notional <= 0.05), else ``variable``. Tick: share of limits on the
  1c grid (within 0.01c); grid = ``cent`` when >= 99 %, else ``subcent``.
- timing: gaps between consecutive creations, overall and within a token (quantiles, share
  under 100 ms, burstiness (sd - mean) / (sd + mean): -1 periodic, 0 Poisson, -> 1 bursty).
- roles: maker/taker share of orders and of filled shares; ``age_s`` quantiles by role.
Keys: ``software_key`` = sha256 of {salt kind, stamp granularity, modal sig_type, proxy
(maker != signer for most orders), main builder}; ``operator_key`` adds {size rule, the fixed
sizes, tick grid}. Null below 30 orders or without a salt kind. Wallets with equal keys form a
cluster: candidates for the same client software (software_key) or the same configured bot
(operator_key). ``similarity`` = mean over those 8 fields of equality (Jaccard for the size
sets). A cluster on a stock-client pattern is low-information: everyone on the default client
shares it.

REFERENCE: how open clients make salts (public source read 2026-11-26; main branches)
- Polymarket/clob-order-utils ``src/utils.ts``: ``Math.round(Math.random() * Date.now())``.
- Polymarket/clob-client-v2 ``src/order-utils/utils.ts``: the same expression.
- Polymarket/py-clob-client-v2 ``py_clob_client_v2/order_utils/utils.py`` (head 9c7c9e8):
  ``int(random.random() * (time.time_ns() // 1_000_000))``; its
  ``exchange_order_builder_v2.py`` stamps ``timestamp = time.time_ns() // 1_000_000`` unless
  the caller supplies one.
  => all three: uniform in [0, now in ms). Reported as ``uniform_below_now_ms``. The two V2
  clients cannot be told apart by salt.
- Polymarket/python-order-utils ``py_order_utils/utils.py`` (used by py-clob-client v1):
  ``round(now_seconds * random())``.
- Polymarket/rs-clob-client ``src/clob/order_builder.rs``: ``round(seconds * rand)`` masked to
  53 bits. => both: uniform in [0, now in s). Reported as ``uniform_below_now_s``.
- UNKNOWN: how clob-client-v2 (TypeScript) stamps ``timestamp``; whether rs-clob-client signs
  V2 orders at all (the file read builds the V1 struct with feeRateBps/nonce/expiration).
A match is reported as "consistent with <generator>-style salt". It is never an identification:
any custom client can copy a one-line generator, and a client accepts a caller-supplied salt.

B. RECONSTRUCTION LEDGER (every part of the strategy to recover is a named obligation)
Status: VERIFIED = measured directly from verified orders, chain fills or exact matches, with
n and the number; ESTIMATED = inferred, with a stated false-positive / placebo rate; UNKNOWN =
with what data would resolve it. Evidence is always attached, whatever the status. "stamps
validated" = ordermatch observable >= 30, match_rate >= 0.9 and placebo_rate <= 0.1.

  identity_software   VERIFIED: >= 200 orders, a salt kind, salt stable, a stamp granularity.
  universe            VERIFIED: >= 200 orders and a dossier asset-horizon mix.
  size_rule           VERIFIED: >= 200 maker orders and rule fixed_sizes or fixed_notional.
  price_rule          VERIFIED: dossier maker_mid_dist_c n >= 200 and book_share >= 0.5.
  placement_cadence   The measured number is the gap between FILLED orders, an upper bound on
                      the re-post cadence. VERIFIED: >= 200 maker orders with a plausible stamp,
                      stamps validated, and a fill footprint with fill ratio >= 0.5 (so filled
                      orders are most of the quoting). ESTIMATED: the same with a lower fill
                      ratio (value adds the estimated quotes per active token-hour; rate
                      stated = the footprint's chance share), or without a footprint, or with
                      >= 30 such orders, observable >= 30 and match_rate > placebo_rate (rate
                      stated = placebo_rate).
  cancel_replace      VERIFIED: stamps validated, >= 200 followed orders, unknown outcomes
                      <= 10 %, >= 90 % of cancels ``alone``. ESTIMATED: >= 30 followed orders
                      and a placebo rate (stated).
  fill_footprint      ESTIMATED: ordermatch fingerprint with a net estimate; fill ratio =
                      (own + own_unmatched) / (own + own_unmatched + net_estimate), all
                      fingerprint-size bids in the ordermatch window; rate stated =
                      expected_by_chance / candidates. No VERIFIED path: unfilled quotes are
                      anonymous.
  inventory_rule      VERIFIED: dossier inventory over >= 30 windows.
  taker_behaviour     VERIFIED: >= 200 orders and (>= 200 taker orders, or taker orders <= 5 %:
                      negligible).
  pnl_source          VERIFIED: dossier realized P&L over >= 200 resolved fills.
  copyability         VERIFIED when the verdict is TAKER; ESTIMATED for RESTING / FLICKER
                      (rate stated = the footprint's chance share, else placebo_rate).
Anything else is UNKNOWN. Coverage = sum of WEIGHTS over VERIFIED obligations + half the
weight of ESTIMATED ones. Copyability verdict, first rule that holds (COPY, provisional):
  TAKER    >= 200 orders and taker share of filled shares >= 0.7;
  FLICKER  median life of unfilled fingerprint quotes < 500 ms (when net_estimate > 0), or
           fill ratio < 0.05;
  RESTING  every known resting-time median (filled, cancelled; n >= 10 each) >= 2 s and fill
           ratio >= 0.2;
  UNKNOWN  otherwise (no ordermatch, no rate, or between the thresholds).

Outputs: ``<out>/<wallet>.ledger.json``, ``<wallet>.ledger.md``, ``cohort.json``, ``cohort.md``.
Analysis only; nothing here is a decision input.
"""

from __future__ import annotations

import argparse
import hashlib
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import orjson
import pyarrow.parquet as pq

from .ordermatch import filled_shares
from .ordertape import committed_parts, ts_flag

VERIFIED, ESTIMATED, UNKNOWN = "VERIFIED", "ESTIMATED", "UNKNOWN"

# obligation -> weight in the coverage score (sums to 1)
WEIGHTS = {
    "identity_software": 0.05,
    "universe": 0.10,
    "size_rule": 0.10,
    "price_rule": 0.15,
    "placement_cadence": 0.10,
    "cancel_replace": 0.10,
    "fill_footprint": 0.10,
    "inventory_rule": 0.10,
    "taker_behaviour": 0.05,
    "pnl_source": 0.10,
    "copyability": 0.05,
}

# status thresholds
T = {
    "min_n": 200,  # orders / fills / followed orders for VERIFIED
    "min_fp_n": 30,  # orders for a fingerprint key or an ESTIMATED status
    "size_cover": 0.9, "max_sizes": 8, "notional_cv": 0.05,
    "match_min": 0.9, "placebo_max": 0.1, "min_observable": 30,
    "unknown_outcomes_max": 0.1, "alone_min": 0.9,
    "cadence_fill_ratio_min": 0.5,
    "min_windows": 30, "book_share_min": 0.5, "taker_negligible": 0.05,
}

# copyability thresholds: PROVISIONAL, set from one wallet-hour (E39), not fitted
COPY = {
    "our_latency_ms": 250,  # context only: flicker_life_ms is twice this
    "taker_share_min": 0.7,
    "flicker_life_ms": 500,
    "flicker_fill_ratio": 0.05,
    "resting_ms": 2000,
    "resting_fill_ratio": 0.2,
    "min_rest_n": 10,
}

REFERENCE_SALT = {
    "uniform_below_now_ms": "consistent with clob-order-utils / clob-client-v2 / py-clob-client-v2 "
                            "style salt (round(random * now_ms)); stock generator, low information",
    "uniform_below_now_s": "consistent with python-order-utils / rs-clob-client style salt "
                           "(round(random * now_s))",
}
SALT_MIN_N = 30
SALT_UNITS = (("ns", 1, 10**6), ("us", 1, 10**3), ("ms", 1, 1), ("s", 1000, 1))  # salt*a ~ created_ms*b
NOTE = ("evidence of shared software or operator, not proof; only filled orders are visible "
        "(gaps bound cadence from above, age_s is time to a fill)")


def _r(x, n: int = 4):
    return None if x is None else round(float(x), n)


def _q(xs: list, qs=(0.1, 0.5, 0.9)) -> dict | None:
    if not xs:
        return None
    a = np.asarray(xs, dtype=float)
    return {f"p{int(q * 100)}": _r(np.quantile(a, q)) for q in qs} | {"n": len(xs)}


def _shares(c: Counter, top: int | None = None) -> dict:
    n = sum(c.values())
    return {str(k): _r(v / n) for k, v in c.most_common(top)}


# ---------------------------------------------------------------- input
def load_rows(path: Path, wallets: set[str] | None = None,
              block_range: tuple[int, int] | None = None) -> tuple[list[dict], bool]:
    """(rows of the wallets (all makers when None), whether the input has a ``verified`` column)."""
    rows: list[dict] = []
    def keep(row: dict) -> bool:
        return (wallets is None or row["maker"] in wallets) and (
            block_range is None or isinstance(row.get("block_ts"), int)
            and block_range[0] <= row["block_ts"] < block_range[1])

    if path.is_dir():
        for p in committed_parts(path, allow_legacy=True):
            for r in pq.read_table(p).to_pylist():
                r["maker"] = r["maker"].lower()
                if keep(r):
                    rows.append(r)
        return rows, True
    flagged = False
    with open(path, "rb") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = orjson.loads(line)
            r["maker"] = r["maker"].lower()
            flagged |= "verified" in r
            if not keep(r):
                continue
            if "ts_flag" not in r:
                r["ts_flag"] = ts_flag(r.get("created_ms"), r["block_ts"])
            rows.append(r)
    return rows, flagged


def prepare(rows: list[dict], has_verified: bool) -> dict:
    """One wallet's usable rows and one record per order_id (see WHICH ROWS COUNT)."""
    use = [r for r in rows if r.get("verified") is True] if has_verified else list(rows)
    use.sort(key=lambda r: (r["block_ts"], r["tx"], r["order_id"]))
    orders: dict[str, dict] = {}
    for r in use:
        orders.setdefault(r["order_id"], r)
    return {"rows": use, "orders": list(orders.values()),
            "input": {"rows": len(rows), "rows_used": len(use),
                      "rows_excluded_by_role": dict(Counter(r["role"] for r in rows)
                                                    - Counter(r["role"] for r in use)),
                      "verified_false": sum(r.get("verified") is False for r in rows),
                      "verified_null": sum(r.get("verified") is None for r in rows) if has_verified else 0,
                      "unflagged_input": not has_verified}}


# ---------------------------------------------------------------- A. fingerprint
def salt_kind(pairs: list[tuple[int, int]]) -> str:
    """Kind of the salts given (salt, created_ms) pairs; rules in the module docstring."""
    n = len(pairs)
    if n < SALT_MIN_N:
        return "insufficient"
    for unit, a, b in SALT_UNITS:
        if sum(abs(s * a - c * b) <= 5000 * b for s, c in pairs) >= 0.9 * n:
            return f"timestamp_{unit}"
    seq = sorted(pairs, key=lambda p: p[1])
    steps = [y[0] > x[0] for x, y in zip(seq, seq[1:]) if y[1] != x[1]]
    if len(steps) >= SALT_MIN_N - 1 and sum(steps) >= 0.9 * len(steps):
        return "counter"
    for name, k in (("uniform_below_now_ms", 1), ("uniform_below_now_s", 1000)):
        if sum(s * k <= c for s, c in pairs) >= 0.99 * n and 0.25 <= st.median(s * k / c for s, c in pairs) <= 0.75:
            return name
    bits = max(s.bit_length() for s, _ in pairs)
    return f"other_le_{next(b for b in (32, 53, 64, 128, 256) if bits <= b)}bit"


def _salt(orders: list[dict]) -> dict:
    seq = sorted(((int(o["salt"]), o["created_ms"]) for o in orders if o["ts_flag"] is None),
                 key=lambda p: p[1])
    q = len(seq) // 4
    parts = [salt_kind(seq[i * q:(i + 1) * q if i < 3 else len(seq)]) for i in range(4)] if q else []
    known = [k for k in parts if k != "insufficient"]
    kind = salt_kind(seq)
    return {"n": len(seq), "kind": kind, "by_quarter": parts,
            "stable": len(set(known)) == 1 and known[0] == kind if len(known) >= 2 else None,
            "digits": {str(k): v for k, v in sorted(Counter(len(str(int(o["salt"]))) for o in orders).items())},
            "reference": REFERENCE_SALT.get(kind)}


def _size(mk: list[dict]) -> dict:
    mk = [o for o in mk if o.get("limit")]
    if not mk:
        return {"n": 0, "rule": None, "sizes_90": None, "tick_grid": None}
    c = Counter(round(o["size"], 2) for o in mk)
    top: list[float] = []
    got = 0
    for s, k in c.most_common():
        if got >= T["size_cover"] * len(mk):
            break
        top.append(s)
        got += k
    sizes = np.array([o["size"] for o in mk])
    notional = np.array([o["size"] * o["limit"] for o in mk])
    cv_n = float(notional.std() / notional.mean())
    rule = ("fixed_sizes" if len(top) <= T["max_sizes"] else
            "fixed_notional" if cv_n <= T["notional_cv"] else "variable")
    cent = sum(abs(o["limit"] * 100 - round(o["limit"] * 100)) <= 0.01 for o in mk) / len(mk)
    return {"n": len(mk), "rule": rule, "n_sizes_90": len(top),
            "sizes_90": sorted(top) if rule == "fixed_sizes" else None,
            "integer_share": _r(sum(abs(o["size"] - round(o["size"])) < 1e-6 for o in mk) / len(mk)),
            "cv_size": _r(sizes.std() / sizes.mean()), "cv_notional": _r(cv_n),
            "notional_usd": _q(notional.tolist()), "limit": _q([o["limit"] for o in mk]),
            "cent_share": _r(cent), "tick_grid": "cent" if cent >= 0.99 else "subcent"}


def _gaps(xs: list[float]) -> dict | None:
    if len(xs) < 2:
        return _q(xs)
    m, sd = st.fmean(xs), st.pstdev(xs)
    return (_q(xs) or {}) | {"under_100ms_share": _r(sum(x < 100 for x in xs) / len(xs)),
                             "burstiness": _r((sd - m) / (sd + m)) if sd + m else None}


def _timing(orders: list[dict]) -> dict:
    ok = sorted((o for o in orders if o["ts_flag"] is None), key=lambda o: o["created_ms"])
    by: dict[str, list[int]] = defaultdict(list)
    for o in ok:
        by[o["token_id"]].append(o["created_ms"])
    return {"n": len(ok),
            "gap_ms_overall": _gaps([b["created_ms"] - a["created_ms"] for a, b in zip(ok, ok[1:])]),
            "gap_ms_same_token": _gaps([y - x for v in by.values() for x, y in zip(v, v[1:])])}


def fingerprint(w: dict) -> dict:
    """Software fingerprint of one prepared wallet (``prepare`` output)."""
    orders, rows = w["orders"], w["rows"]
    n = len(orders)
    mk = [o for o in orders if o["role"] == "maker"]
    stamps = [o["created_ms"] for o in orders if o.get("created_ms")]
    gran = (next((g for g in (1000, 100, 10) if sum(c % g == 0 for c in stamps) >= 0.99 * len(stamps)), 1)
            if len(stamps) >= SALT_MIN_N else None)
    sh: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.get("fill_amount") is not None and (r["side"] == "SELL" or r.get("maker_amount") or r.get("limit")):
            sh[r["role"]] += filled_shares(r)
    days = Counter(datetime.fromtimestamp(o["block_ts"], UTC).strftime("%Y-%m-%d") for o in orders)
    hours = Counter(datetime.fromtimestamp(o["block_ts"], UTC).hour for o in orders)
    salt, size = _salt(orders), _size(mk)
    builders = Counter(o["builder"] for o in orders)
    sig = Counter(o["sig_type"] for o in orders)
    proxy = sum(o["maker"] != o["signer"].lower() for o in orders) / n if n else None
    fp: dict = {
        "n_orders": n, "n_maker_orders": len(mk),
        "n_maker_plausible_stamp": sum(o["ts_flag"] is None for o in mk),
        "salt": salt,
        "timestamp": {"granularity_ms": gran, "flags": _shares(Counter(o["ts_flag"] or "ok" for o in orders))},
        "signing": {"sig_type": _shares(sig), "maker_ne_signer_share": _r(proxy),
                    "builder": _shares(builders, 5)},
        "size": size, "timing": _timing(orders),
        "roles": {"orders": _shares(Counter(o["role"] for o in orders)),
                  "taker_orders": n - len(mk),
                  "filled_shares": {k: _r(v, 2) for k, v in sh.items()},
                  "taker_share_of_shares": _r(sh["taker"] / sum(sh.values())) if sum(sh.values()) else None,
                  "age_s": {role: _q([o["age_s"] for o in orders if o["role"] == role and o["ts_flag"] is None])
                            for role in ("maker", "taker")}},
        "universe": {"tokens": len({o["token_id"] for o in orders}), "days": dict(sorted(days.items())),
                     "hours_utc": {str(h): hours[h] for h in sorted(hours)},
                     "first_block_ts": min((o["block_ts"] for o in orders), default=None),
                     "last_block_ts": max((o["block_ts"] for o in orders), default=None)},
        "descriptor": None, "software_key": None, "operator_key": None, "note": NOTE}
    if n >= T["min_fp_n"] and salt["kind"] != "insufficient":
        top_b, top_n = builders.most_common(1)[0]
        soft = {"salt": salt["kind"], "ts_granularity_ms": gran, "sig_type": sig.most_common(1)[0][0],
                "proxy": proxy is not None and proxy >= 0.5,
                "builder": top_b if top_n >= 0.5 * n else "mixed"}
        full = soft | {"size_rule": size["rule"], "sizes": size["sizes_90"], "tick_grid": size["tick_grid"]}
        fp |= {"descriptor": full, "software_key": _key(soft), "operator_key": _key(full)}
    return fp


def _key(d: dict) -> str:
    return hashlib.sha256(orjson.dumps(d, option=orjson.OPT_SORT_KEYS)).hexdigest()[:12]


def similarity(a: dict | None, b: dict | None) -> float | None:
    """Mean agreement of two descriptors over their 8 fields (Jaccard for the size sets)."""
    if a is None or b is None:
        return None
    s = 0.0
    for k in a:
        if k == "sizes":
            x, y = set(a[k] or ()), set(b[k] or ())
            s += len(x & y) / len(x | y) if x | y else 1.0
        else:
            s += a[k] == b[k]
    return round(s / len(a), 4)


def clusters(fps: dict[str, dict], key: str) -> dict[str, list[str]]:
    """key value -> wallets sharing it (2 or more)."""
    by: dict[str, list[str]] = defaultdict(list)
    for w, fp in sorted(fps.items()):
        if fp[key]:
            by[fp[key]].append(w)
    return {k: v for k, v in by.items() if len(v) > 1}


# ---------------------------------------------------------------- B. ledger
def _ob(status: str, source: str | None, *, n=None, value=None, rate=None, resolve=None,
        evidence=None) -> dict:
    return {"status": status, "n": n, "value": value, "false_positive_rate": rate,
            "resolve": resolve, "evidence": evidence, "source": source}


def _stamps_validated(om: dict | None) -> bool:
    return bool(om and om["observable"] >= T["min_observable"] and om["match_rate"] is not None
                and om["match_rate"] >= T["match_min"] and om["placebo_rate"] <= T["placebo_max"])


def footprint(om: dict | None) -> dict | None:
    """Fill ratio of fingerprint-size bids in the ordermatch window, or None when not estimable."""
    t = ((om or {}).get("fingerprint") or {}).get("total")
    if not t or t["net_estimate"] is None:
        return None
    filled = t["own"] + t["own_unmatched"]
    tot = filled + t["net_estimate"]
    hours = om["fingerprint"].get("active_token_hours") if om else None
    return {"filled_quotes": filled, "unfilled_net_estimate": t["net_estimate"],
            "quotes_per_token_hour": _r(tot / hours, 1) if hours else None,
            "fill_ratio": _r(filled / tot, 5) if tot else None,
            "chance_share": _r(t["expected_by_chance"] / t["candidates"]) if t["candidates"] else 0.0,
            "identity_precision_upper": t["identity_precision_upper"]}


def copyability(fp: dict, om: dict | None) -> dict:
    """{"verdict", "reasons", "rate"} from measured numbers only; thresholds in COPY."""
    tk = fp["roles"]["taker_share_of_shares"]
    if fp["n_orders"] >= T["min_n"] and tk is not None and tk >= COPY["taker_share_min"]:
        return {"verdict": "TAKER", "rate": None,
                "reasons": [f"taker share of filled shares {tk} >= {COPY['taker_share_min']} over {fp['n_orders']} orders"]}
    if not om:
        return {"verdict": "UNKNOWN", "rate": None, "reasons": ["no ordermatch for this wallet"]}
    ft = footprint(om)
    ratio = ft["fill_ratio"] if ft else None
    rate = ft["chance_share"] if ft else om["placebo_rate"]
    if rate is None:
        return {"verdict": "UNKNOWN", "rate": None, "reasons": ["no placebo or chance rate"]}
    life = (om["fingerprint"].get("candidate_rest_ms_cancelled") or {}).get("p50") \
        if ft and ft["unfilled_net_estimate"] > 0 else None
    rests = {k: v["p50"] for k, v in om["rest_ms_known"].items() if v and v["n"] >= COPY["min_rest_n"]}
    why = []
    if life is not None and life < COPY["flicker_life_ms"]:
        why.append(f"median life of unfilled fingerprint quotes {life} ms < {COPY['flicker_life_ms']} ms")
    if ratio is not None and ratio < COPY["flicker_fill_ratio"]:
        why.append(f"fill ratio {ratio} < {COPY['flicker_fill_ratio']}")
    if why:
        return {"verdict": "FLICKER", "rate": rate, "reasons": why}
    if rests and min(rests.values()) >= COPY["resting_ms"] and ratio is not None and ratio >= COPY["resting_fill_ratio"]:
        return {"verdict": "RESTING", "rate": rate,
                "reasons": [f"resting ms p50 {rests} all >= {COPY['resting_ms']}",
                            f"fill ratio {ratio} >= {COPY['resting_fill_ratio']}"]}
    return {"verdict": "UNKNOWN", "rate": None,
            "reasons": [f"between thresholds or missing: resting ms p50 {rests or None}, fill ratio {ratio}, "
                        f"unfilled quote life p50 {life}"]}


def ledger(wallet: str, w: dict, fp: dict, om: dict | None = None, dos: dict | None = None,
           src: dict[str, str | None] | None = None) -> dict:
    """The reconstruction ledger of one wallet. ``src`` names the orders / ordermatch / dossier files."""
    src = src or {}
    S_ord, S_om, S_dos = src.get("orders"), src.get("ordermatch"), src.get("dossier")
    n, n_mk, N = fp["n_orders"], fp["n_maker_orders"], T["min_n"]
    ob: dict[str, dict] = {}

    salt, gran = fp["salt"], fp["timestamp"]["granularity_ms"]
    ev = {"salt": salt, "timestamp": fp["timestamp"], "signing": fp["signing"],
          "software_key": fp["software_key"], "operator_key": fp["operator_key"]}
    if n >= N and salt["kind"] != "insufficient" and salt["stable"] is True and gran is not None:
        ob["identity_software"] = _ob(VERIFIED, S_ord, n=n, evidence=ev, value={
            "salt": salt["kind"], "ts_granularity_ms": gran, "software_key": fp["software_key"],
            "reference": salt["reference"]})
    else:
        ob["identity_software"] = _ob(UNKNOWN, S_ord, n=n, evidence=ev, resolve=(
            f">= {N} verified orders with a stable salt kind (have {n}, kind {salt['kind']}, "
            f"stable {salt['stable']})"))

    mix = ((dos or {}).get("mechanism") or {}).get("mix")
    ev = fp["universe"] | {"asset_horizon_mix": mix}
    if n >= N and mix:
        ob["universe"] = _ob(VERIFIED, S_dos, n=n, evidence=ev, value={
            "mix": mix, "tokens": fp["universe"]["tokens"], "days": len(fp["universe"]["days"])})
    else:
        ob["universe"] = _ob(UNKNOWN, S_ord, n=n, evidence=ev, resolve=(
            f">= {N} verified orders (have {n}) and a dossier mapping tokens to asset and horizon"
            f" ({'present' if mix else 'missing'})"))

    size = fp["size"]
    if n_mk >= N and size["rule"] in ("fixed_sizes", "fixed_notional"):
        ob["size_rule"] = _ob(VERIFIED, S_ord, n=size["n"], evidence=size, value=(
            {"rule": "fixed_sizes", "sizes": size["sizes_90"]} if size["rule"] == "fixed_sizes" else
            {"rule": "fixed_notional", "notional_usd_p50": size["notional_usd"]["p50"]}))
    else:
        ob["size_rule"] = _ob(UNKNOWN, S_ord, n=n_mk, evidence=size, resolve=(
            f">= {N} verified maker orders (have {n_mk}) with fixed sizes or fixed notional (rule "
            f"{size['rule']}); a variable rule needs book, fair and inventory at placement to fit"))

    d_ord, d_cov = (dos or {}).get("orders") or {}, (dos or {}).get("coverage") or {}
    mid, book = d_ord.get("maker_mid_dist_c"), d_cov.get("book_share")
    ev = {"limit": size.get("limit"), "tick_grid": size.get("tick_grid"), "cent_share": size.get("cent_share"),
          "maker_mid_dist_c": mid, "maker_fair_dist_c": d_ord.get("maker_fair_dist_c"), "book_share": book}
    if mid and mid["n"] >= N and book is not None and book >= T["book_share_min"]:
        ob["price_rule"] = _ob(VERIFIED, S_dos, n=mid["n"], evidence=ev, value={
            "maker_mid_dist_c_p50": mid["p50"], "at": "first fill of each maker order"})
    else:
        ob["price_rule"] = _ob(UNKNOWN, S_dos or S_ord, n=mid["n"] if mid else 0, evidence=ev, resolve=(
            f"distance of each quote to book and fair at placement for >= {N} maker orders with book "
            f"state for >= {T['book_share_min']} of fills (dossier book_share {book}); join ordermatch "
            "placements to the intl book"))

    n_ts, gaps, ft = fp["n_maker_plausible_stamp"], fp["timing"], footprint(om)
    ev = gaps | {"sign_to_book_ms": (om or {}).get("sign_to_book_ms"),
                 "match_rate": (om or {}).get("match_rate"), "placebo_rate": (om or {}).get("placebo_rate"),
                 "footprint": ft}
    val = {"filled_order_gap_ms_same_token_p50": (gaps["gap_ms_same_token"] or {}).get("p50"),
           "filled_order_gap_ms_overall_p50": (gaps["gap_ms_overall"] or {}).get("p50"),
           "sign_to_book_ms_p50": ((om or {}).get("sign_to_book_ms") or {}).get("p50"),
           "note": "gaps between FILLED orders: an upper bound on the re-post cadence"}
    ratio = ft["fill_ratio"] if ft else None
    if n_ts >= N and _stamps_validated(om) and ft and ratio is not None:
        if ratio >= T["cadence_fill_ratio_min"]:
            ob["placement_cadence"] = _ob(VERIFIED, S_ord, n=n_ts, value=val, evidence=ev)
        else:
            ob["placement_cadence"] = _ob(ESTIMATED, S_om, n=n_ts, evidence=ev, rate=ft["chance_share"], value=val | {
                "est_quotes_per_token_hour": ft["quotes_per_token_hour"], "fill_ratio": ratio})
    elif (om and n_ts >= T["min_fp_n"] and om["observable"] >= T["min_observable"]
          and om["match_rate"] > om["placebo_rate"]):
        ob["placement_cadence"] = _ob(ESTIMATED, S_ord, n=n_ts, value=val, evidence=ev, rate=om["placebo_rate"])
    else:
        ob["placement_cadence"] = _ob(UNKNOWN, S_ord, n=n_ts, value=val, evidence=ev, resolve=(
            "created_ms is self-reported: run doppel-ordermatch on a tape window holding >= "
            f"{T['min_observable']} of the wallet's orders (match rate vs placebo)"))

    if om:
        oc = Counter()
        for k, v in om["outcomes"].items():
            oc[k.split(":")[0]] += v
        fol, canc = om["matched_all"], oc["cancelled"]
        ev = {"outcomes": om["outcomes"], "rest_ms_known": om["rest_ms_known"],
              "cancelled_alone": om["cancelled_alone"], "wrong_matches": om["fill_before_placement"],
              "placebo_rate": om["placebo_rate"]}
        val = {"filled": oc["filled"], "cancelled_or_replaced": canc, "unknown": oc["unknown"],
               "rest_ms_p50": {k: v and v["p50"] for k, v in om["rest_ms_known"].items()}}
        if (_stamps_validated(om) and fol >= N and oc["unknown"] <= T["unknown_outcomes_max"] * fol
                and om["cancelled_alone"] >= T["alone_min"] * canc):
            ob["cancel_replace"] = _ob(VERIFIED, S_om, n=fol, value=val, evidence=ev)
        elif fol >= T["min_fp_n"] and om["placebo_rate"] is not None:
            ob["cancel_replace"] = _ob(ESTIMATED, S_om, n=fol, value=val, evidence=ev, rate=om["placebo_rate"])
        else:
            ob["cancel_replace"] = _ob(UNKNOWN, S_om, n=fol, value=val, evidence=ev, resolve=(
                f">= {T['min_fp_n']} orders followed in the book (have {fol}) with a placebo rate"))
    else:
        ob["cancel_replace"] = _ob(UNKNOWN, None, n=0, resolve=(
            "doppel-ordermatch follow-forward of the wallet's orders (cancels are not on chain)"))

    if ft and ft["fill_ratio"] is not None:
        ob["fill_footprint"] = _ob(ESTIMATED, S_om, n=ft["filled_quotes"], rate=ft["chance_share"], evidence=ft,
                                   value={"fill_ratio": ft["fill_ratio"], "basis": "fingerprint-size bids, ordermatch window"})
    else:
        ob["fill_footprint"] = _ob(UNKNOWN, S_om, n=0, resolve=(
            "ordermatch fingerprint attribution with control tokens (needs a compact size "
            "fingerprint); unfilled quotes are anonymous, so identity data would be needed to verify"))

    inv = (dos or {}).get("inventory") or {}
    wins = inv.get("windows") or 0
    ev = {k: inv.get(k) for k in ("windows", "flat_close_share", "paired_share_of_gross",
                                  "abs_net_close_over_gross", "peak_inventory_shares", "skew_vs_inventory")}
    if wins >= T["min_windows"]:
        ob["inventory_rule"] = _ob(VERIFIED, S_dos, n=wins, evidence=ev, value={
            "flat_close_share": inv.get("flat_close_share"),
            "paired_share_of_gross": inv.get("paired_share_of_gross")})
    else:
        ob["inventory_rule"] = _ob(UNKNOWN, S_dos, n=wins, evidence=ev if dos else None, resolve=(
            f"dossier inventory over >= {T['min_windows']} windows (have {wins})"))

    roles, n_tk = fp["roles"], fp["roles"]["taker_orders"]
    ev = roles | {"policy": ((dos or {}).get("policy") or {}).get("verdict")}
    if n >= N and n_tk >= N:
        ob["taker_behaviour"] = _ob(VERIFIED, S_ord, n=n_tk, evidence=ev, value={
            "taker_share_of_orders": _r(n_tk / n), "taker_share_of_shares": roles["taker_share_of_shares"],
            "age_s_p50": (roles["age_s"]["taker"] or {}).get("p50")})
    elif n >= N and n_tk <= T["taker_negligible"] * n:
        ob["taker_behaviour"] = _ob(VERIFIED, S_ord, n=n, evidence=ev, value={
            "taker_share_of_orders": _r(n_tk / n), "negligible": True})
    else:
        ob["taker_behaviour"] = _ob(UNKNOWN, S_ord, n=n_tk, evidence=ev, resolve=(
            f">= {N} verified orders (have {n}) of which >= {N} taker orders (have {n_tk})"))

    edge = (dos or {}).get("edge") or {}
    res = edge.get("all_resolved_crypto") or {}
    if (res.get("fills") or 0) >= N:
        ob["pnl_source"] = _ob(VERIFIED, S_dos, n=res["fills"], value={
            "realized_usd": res.get("realized_usd"), "fees_usd": res.get("fees_usd"),
            "maker_realized_usd_btc": (edge.get("maker") or {}).get("realized_usd"),
            "taker_realized_usd_btc": (edge.get("taker") or {}).get("realized_usd")},
            evidence={k: edge.get(k) for k in ("all", "maker", "taker", "all_resolved_crypto")}
            | {"note": "outcome over the dossier's days; persistence is not assessed here (E28)"})
    else:
        ob["pnl_source"] = _ob(UNKNOWN, S_dos, n=res.get("fills") or 0, resolve=(
            f"dossier with >= {N} resolved fills (have {res.get('fills') or 0})"))

    cp = copyability(fp, om)
    ob["copyability"] = _ob(
        VERIFIED if cp["verdict"] == "TAKER" else UNKNOWN if cp["verdict"] == "UNKNOWN" else ESTIMATED,
        S_ord if cp["verdict"] == "TAKER" else S_om, n=n if cp["verdict"] == "TAKER" else (om or {}).get("matched_all", 0),
        value={"verdict": cp["verdict"], "reasons": cp["reasons"]}, rate=cp["rate"],
        resolve="; ".join(cp["reasons"]) if cp["verdict"] == "UNKNOWN" else None,
        evidence={"thresholds_provisional": COPY})

    cov = sum(WEIGHTS[k] * {VERIFIED: 1.0, ESTIMATED: 0.5, UNKNOWN: 0.0}[o["status"]] for k, o in ob.items())
    mech = (dos or {}).get("mechanism") or {}
    return {"wallet": wallet, "input": w["input"], "sources": src, "coverage": round(cov, 4),
            "copyability": cp["verdict"],
            "mechanism": ({"maker_share": mech.get("maker_share"), "mix": mech.get("mix")} if dos else None),
            "obligations": {k: ob[k] for k in WEIGHTS}, "fingerprint": fp,
            "weights": WEIGHTS, "thresholds": T, "copy_thresholds_provisional": COPY}


# ---------------------------------------------------------------- cards
def _s(x) -> str:
    return "" if x is None else orjson.dumps(x).decode().replace("|", "/") if isinstance(x, dict | list) else str(x)


def card(doc: dict) -> str:
    fp = doc["fingerprint"]
    L = [f"# Ledger {doc['wallet']}", "",
         f"coverage **{doc['coverage']}** | copyability **{doc['copyability']}** (thresholds provisional) | "
         f"orders {fp['n_orders']} (maker {fp['n_maker_orders']}) | input {_s(doc['input'])}", "",
         "| obligation | weight | status | n | value | fp rate | resolve by | source |", "|---|---|---|---|---|---|---|---|"]
    for k, o in doc["obligations"].items():
        L.append(f"| {k} | {WEIGHTS[k]} | {o['status']} | {_s(o['n'])} | {_s(o['value'])} | "
                 f"{_s(o['false_positive_rate'])} | {_s(o['resolve'])} | {_s(o['source'])} |")
    L += ["", "## Software fingerprint", "",
          f"- software_key `{fp['software_key']}`, operator_key `{fp['operator_key']}`: {NOTE}",
          f"- salt: {fp['salt']['kind']} (stable {fp['salt']['stable']}, by quarter {fp['salt']['by_quarter']}, "
          f"digits {_s(fp['salt']['digits'])})" + (f"; {fp['salt']['reference']}" if fp["salt"]["reference"] else ""),
          f"- timestamp: granularity {fp['timestamp']['granularity_ms']} ms, flags {_s(fp['timestamp']['flags'])}",
          f"- signing: {_s(fp['signing'])}",
          f"- size: {_s({k: v for k, v in fp['size'].items() if k not in ('notional_usd', 'limit')})}",
          f"- timing: {_s(fp['timing'])}",
          f"- roles: {_s(fp['roles'])}"]
    return "\n".join(L) + "\n"


def cohort(docs: list[dict]) -> tuple[dict, str]:
    fps = {d["wallet"]: d["fingerprint"] for d in docs}
    soft, oper = clusters(fps, "software_key"), clusters(fps, "operator_key")
    ws = sorted(fps)
    sim = [{"a": a, "b": b, "similarity": similarity(fps[a]["descriptor"], fps[b]["descriptor"])}
           for i, a in enumerate(ws) for b in ws[i + 1:] if fps[a]["descriptor"] and fps[b]["descriptor"]]
    rows = [{"wallet": d["wallet"], "mechanism": d["mechanism"], "coverage": d["coverage"],
             "copyability": d["copyability"], "software_key": d["fingerprint"]["software_key"],
             "software_cluster_size": len(soft.get(d["fingerprint"]["software_key"], [])) or 1,
             "operator_key": d["fingerprint"]["operator_key"],
             "operator_cluster_size": len(oper.get(d["fingerprint"]["operator_key"], [])) or 1,
             "salt": d["fingerprint"]["salt"]["kind"]} for d in sorted(docs, key=lambda d: -d["coverage"])]
    doc = {"wallets": rows, "software_clusters": soft, "operator_clusters": oper,
           "similarity": sorted(sim, key=lambda s: -s["similarity"]), "note": NOTE,
           "weights": WEIGHTS, "thresholds": T, "copy_thresholds_provisional": COPY}
    L = ["# Cohort profile", "", f"Clusters are {NOTE}. A cluster on a stock-client salt is low-information.", "",
         "| wallet | mechanism | coverage | copyability | salt | software key (n) | operator key (n) |",
         "|---|---|---|---|---|---|---|"]
    L += [f"| {r['wallet']} | {_s(r['mechanism'])} | {r['coverage']} | {r['copyability']} | {r['salt']} | "
          f"{r['software_key']} ({r['software_cluster_size']}) | {r['operator_key']} ({r['operator_cluster_size']}) |"
          for r in rows]
    return doc, "\n".join(L) + "\n"


# ---------------------------------------------------------------- run
def run(orders: Path, out: Path, ordermatch: Path | None = None, dossiers: Path | None = None,
        wallets: list[str] | None = None, min_orders: int = T["min_n"], *,
        block_range: tuple[int, int] | None = None) -> dict:
    """Write every ledger and the cohort table; returns the cohort document. Without ``wallets``:
    every maker with at least ``min_orders`` usable orders."""
    want = {w.lower() for w in wallets} if wallets else None
    rows, has_verified = load_rows(orders, want, block_range)
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["maker"]].append(r)
    om_doc = orjson.loads(ordermatch.read_bytes())["wallets"] if ordermatch else {}
    out.mkdir(parents=True, exist_ok=True)
    docs = []
    for wal in sorted(want or by):
        w = prepare(by.get(wal, []), has_verified)
        if want is None and len(w["orders"]) < min_orders:
            continue
        dpath = dossiers / f"{wal}.json" if dossiers else None
        dos = orjson.loads(dpath.read_bytes()) if dpath and dpath.is_file() else None
        if block_range is not None:
            w["input"]["order_block_range"] = block_range
        doc = ledger(wal, w, fingerprint(w), om_doc.get(wal), dos, {
            "orders": str(orders), "ordermatch": str(ordermatch) if wal in om_doc else None,
            "dossier": str(dpath) if dos else None})
        (out / f"{wal}.ledger.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
        (out / f"{wal}.ledger.md").write_text(card(doc))
        docs.append(doc)
    cdoc, cmd = cohort(docs)
    (out / "cohort.json").write_bytes(orjson.dumps(cdoc, option=orjson.OPT_INDENT_2))
    (out / "cohort.md").write_text(cmd)
    return cdoc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-profile")
    ap.add_argument("--orders", type=Path, required=True,
                    help="doppel-ordertape output: cohort parquet directory, or a JSONL sample")
    ap.add_argument("--ordermatch", type=Path, help="ordermatch.json (doppel-ordermatch)")
    ap.add_argument("--dossiers", type=Path, help="directory of <wallet>.json dossiers")
    ap.add_argument("--wallet", nargs="+", action="extend", help="maker address(es) (default: makers "
                    "with >= --min-orders usable orders)")
    ap.add_argument("--min-orders", type=int, default=T["min_n"])
    ap.add_argument("--out", type=Path, required=True, help="directory")
    a = ap.parse_args(argv)
    doc = run(a.orders, a.out, a.ordermatch, a.dossiers, a.wallet, a.min_orders)
    print((a.out / "cohort.md").read_text())
    print(f"{len(doc['wallets'])} wallets -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
