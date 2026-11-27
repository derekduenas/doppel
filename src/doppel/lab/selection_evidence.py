"""After-fee trading profit by resolved event in one forward wallet week.

The input is the output of absorb_wallet and the ledger token map keyed by token ID.
Cash-only transactions (including deposits and unattributed incentives) are outside
the trade-and-settlement measure. Shares retained at close are valued at their verified
binary payout. The function never uses pairing classifications or aggregate wallet P&L.
"""

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from .absorb import ADAPTERS, CASH, U, ZERO
from .scan import LEN, WINDOW

WEEK = 7 * 86400
OPERATIONS = {"split", "merge", "redeem"}
TRADES = {"buy", "sell"}


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _micro(value):
    _require(type(value) in (int, float), "missing numeric balance or path value")
    amount = Decimal(str(value)) * U
    _require(amount.is_finite() and amount == amount.to_integral_value(), "invalid balance precision")
    return int(amount)


def _condition(value):
    _require(isinstance(value, str), "missing condition identity")
    norm = value.lower().removeprefix("0x")
    _require(len(norm) == 64 and all(ch in "0123456789abcdef" for ch in norm), "invalid condition identity")
    return norm


def _markets(rows, tokens, start, end):
    """Validate identities for every observed token before excluding out-of-scope markets."""
    observed = defaultdict(set)
    for token, position in rows.items():
        observed[_condition(position["condition"])].add(token)
    meta = defaultdict(dict)
    for token, item in tokens.items():
        condition = item.get("condition")
        if isinstance(condition, str) and condition.lower().removeprefix("0x") in observed:
            _require(item["token"] == token, "token map key and record disagree")
            meta[_condition(condition)][token] = item

    result = {}
    slug_owners = {}
    for condition, present in observed.items():
        members = meta[condition]
        _require(present <= members.keys(), "missing token metadata for absorbed position")
        slugs = {m.get("slug") for m in members.values()}
        _require(len(slugs) == 1 and all(isinstance(s, str) and s for s in slugs),
                 "missing or inconsistent condition slug")
        slug = next(iter(slugs))
        _require(all(m.get("status") in ("open", "resolved") for m in members.values()),
                 "unknown token metadata status")
        for token in present:
            _require(type(rows[token]["idx"]) is int and type(members[token].get("idx")) is int and
                     rows[token]["idx"] == members[token]["idx"], "absorption and venue token indices disagree")
        match = WINDOW.fullmatch(slug)
        if not match:
            result[condition] = ("outside", {})
            continue
        _require(slug_owners.setdefault(slug, condition) == condition, "multiple conditions for one event slug")
        _require(len(members) == 2 and {m["idx"] for m in members.values()} == {0, 1} and
                 all(type(m["idx"]) is int for m in members.values()), "incomplete or contradictory binary pair")
        _require(present == members.keys(), "opening or closing balance missing for a binary outcome")
        opened = int(match[3])
        closed = opened + LEN[match[2]]
        for m in members.values():
            expiry = datetime.fromisoformat(m["end"].replace("Z", "+00:00"))
            _require(expiry.tzinfo and expiry.timestamp() == closed, "market close disagrees with slug")
        if closed <= start or opened >= end:
            result[condition] = ("outside", {})
            continue
        _require(start <= opened and closed <= end, "market crosses the weekly boundary")
        payout = {}
        for token, m in members.items():
            _require(m.get("status") == "resolved" and type(m.get("payout")) in (int, float) and
                     m["payout"] in (0, 1), "unresolved or nonbinary payout")
            payout[token] = int(m["payout"])
        _require(sum(payout.values()) == 1, "outcome payouts are not complementary")
        _require(all(_micro(rows[t]["open"]) == 0 for t in members), "opening inventory in selected event")
        result[condition] = ("inside", payout)
    return result


def _verify_operation(event, condition, payout, wallet):
    """CTF cash must match the receipt operation and the wallet's own share legs."""
    kind, legs, cash = event["kind"], event["shares"], event["cash"]
    records = event.get("ctf") or []
    _require(all(r["kind"] == kind and _condition(r["condition"]) == condition and
                 r["stakeholder"] in (wallet, *ADAPTERS) and type(r["amount"]) is int and r["amount"] >= 0
                 for r in records) and records, "ambiguous conditional-token operation")
    allowed = {ZERO} if all(r["stakeholder"] == wallet for r in records) else {
        r["stakeholder"] for r in records if r["stakeholder"] != wallet} | {ZERO}
    counterparties = event.get("share_counterparties")
    _require(counterparties and set(counterparties) <= allowed, "unattributed conditional-token share transfer")
    amount = sum(r["amount"] for r in records)
    if kind in {"split", "merge"}:
        sign = 1 if kind == "split" else -1
        _require(len(legs) == 2 and all(delta == sign * amount for delta in legs.values()) and
                 cash == -sign * amount, "split or merge has unexplained share or cash legs")
    else:
        _require(all(delta <= 0 for delta in legs.values()) and cash == amount and
                 cash == -sum(delta * payout[t] for t, delta in legs.items()),
                 "redemption cash disagrees with burned shares and resolved payout")


def _verify_collateral(contracts, aggregate):
    _require(isinstance(contracts, dict) and set(contracts) == set(CASH), "missing collateral contract evidence")
    totals = Counter()
    for row in contracts.values():
        opened, closed, change, moved, residual = (
            row[k] for k in ("open_micro", "close_micro", "observed_micro", "chain_legs_micro", "residual_micro"))
        _require(all(type(n) is int for n in (opened, closed, change, moved, residual)) and
                 min(opened, closed) >= 0 and closed - opened == change == moved and residual == 0,
                 "collateral contract does not reconcile exactly")
        totals.update({"start": opened, "end": closed, "observed": change, "chain_legs": moved})
    _require(all(total == _micro(aggregate[key]) for key, total in totals.items()),
             "collateral contracts disagree with total cash")


def _profit(report, tokens, wallet, start, end):
    _require(type(start) is int and type(end) is int and end - start == WEEK, "expected one complete week")
    _require(report["wallet"] == wallet and report["window_time"] == [start, end], "wallet or weekly bounds differ")
    _require(report["status"] == "reconciled" and report["not_reconciled"] is False and report["causes"] == [],
             "absorption did not reconcile")
    coverage = report["completeness"]
    _require(coverage["goldsky_complete"] is True and coverage["chain_scan_complete"] is True and
             coverage.get("goldsky_store_verified") is True and coverage.get("block_time_window_verified") is True and
             coverage["chain_error"] is None and coverage["chain_next_block"] is None and coverage["receipts_missing"] == 0,
             "incomplete pagination, chain scan or receipts")
    fill = report["fill_check"]
    check = fill["order_filled"]
    counts_ok = (check["matched"], check["chain"], report["goldsky_fills"])
    anomalies = [check.get(k) for k in ("amount_mismatch", "chain_only", "goldsky_only")]
    anomalies.extend(fill.get(k) for k in ("tx_asset_mismatches", "chain_fill_txs_missing_in_goldsky",
                                           "goldsky_fill_txs_without_chain_legs", "unexplained_gross_share_movements",
                                           "unexplained_gross_cash_movements", "other_collateral_fill_txs"))
    _require(all(type(n) is int and n >= 0 for n in counts_ok) and len(set(counts_ok)) == 1 and
             all(type(n) is int and n == 0 for n in anomalies), "fill-level chain checks disagree")
    b0, b1 = report["window_blocks"]
    _require(type(b0) is int and type(b1) is int and 0 < b0 <= b1, "invalid block range")
    timestamps = coverage.get("chain_window_timestamps")
    _require(isinstance(timestamps, list) and len(timestamps) == 4 and all(type(t) is int for t in timestamps)
             and timestamps[0] < start <= timestamps[1] <= timestamps[2] < end <= timestamps[3],
             "block timestamps do not enclose the weekly interval")
    rows = report["tokens"]
    positions = {t: _micro(r["open"]) for t, r in rows.items()}
    markets = _markets(rows, tokens, start, end)
    by_kind = Counter()
    fees = 0
    counts = Counter()
    path_count = Counter()
    single_leg_cash = Counter()
    single_leg_fees = Counter()
    profit = Counter()
    events = report.get("condition_cash")
    _require(isinstance(events, list), "missing condition cash receipt")
    _require(type(report["chain_legs"]) is int and report["chain_legs"] >= 0 and
             bool(events) == bool(report["chain_legs"]), "chain legs disagree with economic activity")
    for event in events:
        block, kind, legs, cash, fee = (event[k] for k in ("block", "kind", "shares", "cash", "fee"))
        _require(type(cash) is int and type(fee) is int and fee >= 0 and type(block) is int and
                 b0 <= block <= b1 and isinstance(event["tx"], str) and event["tx"] and isinstance(legs, dict),
                 "invalid economic event in condition cash receipt")
        by_kind[kind] += cash
        counts[kind] += 1
        fees += fee
        if kind == "cash":
            _require(not legs and fee == 0, "cash-only transaction contains token legs")
            continue
        _require(bool(legs) and legs.keys() <= positions.keys(), "token leg missing from wallet balances")
        if len(legs) == 1:
            token, = legs
            single_leg_cash[token] += cash
            single_leg_fees[token] += fee
        conditions = {_condition(rows[t]["condition"]) for t in legs}
        _require(len(conditions) == 1, "ambiguous cross-condition transaction")
        condition, = conditions
        scope, payout = markets[condition]
        for token, delta in legs.items():
            _require(type(delta) is int, "invalid share amount")
            positions[token] += delta
            paths = rows[token]["path"]
            i = path_count[token]
            _require(i < len(paths), "economic event has no token path")
            bp, kp, dp, pos = paths[i]
            _require((bp, kp, _micro(dp), _micro(pos)) == (block, kind, delta, positions[token]) and
                     positions[token] >= 0, "economic event disagrees with token path")
            path_count[token] += 1
        if scope == "outside":
            continue
        if kind in TRADES:
            _require(len(legs) == 1, "multiple outcome tokens on one trade leg")
            delta, = legs.values()
            valid = (delta > 0 and cash < 0 and -cash >= fee) if kind == "buy" else (delta < 0 and cash + fee >= 0)
            _require(valid, "trade cash or share direction disagrees with side")
        elif kind in OPERATIONS:
            _require(fee == 0, "unexplained conditional-token fee")
            _verify_operation(event, condition, payout, wallet)
        else:
            raise ValueError("unsupported share transfer in selected event")
        profit[condition] += cash
    _require(counts["buy"] + counts["sell"] == report["goldsky_fills"] and
             {k: v for k, v in counts.items() if k not in TRADES} == report["tx_kinds"],
             "event counts differ from independently reconstructed totals")
    _require(dict(by_kind) == {k: _micro(v) for k, v in report["cash"]["by_kind"].items()} and
             fees == _micro(report["cash"]["fees"]), "condition cash or fee totals disagree with absorption")
    rec = report["reconciliation"]
    _require(set(rec) == set(rows) | {"cash"}, "incomplete balance reconciliation")
    _verify_collateral(report.get("collateral_reconciliation"), rec["cash"])
    for token, balances in rec.items():
        opened, closed, delta, built, on_chain, residual, log_residual = (
            _micro(balances[k]) for k in ("start", "end", "observed", "reconstructed", "chain_legs", "residual", "log_residual"))
        if token == "cash":
            _require(opened == _micro(report["cash"]["open"]) and built == sum(by_kind.values()),
                     "cash balance path does not reconcile")
        else:
            row = rows[token]
            _require(single_leg_cash[token] == _micro(row["fill_cash"]) and single_leg_fees[token] == _micro(row["fees"]),
                     "per-token cash or fees disagree with absorption")
            _require(opened == _micro(row["open"]) and closed == _micro(row["close"]) == positions[token] and
                     len(row["path"]) == path_count[token], "token balance path does not reconcile")
            condition = _condition(row["condition"])
            scope, payout = markets[condition]
            if scope == "inside" and condition in profit:
                profit[condition] += closed * payout[token]
        _require(closed - opened == delta == built == on_chain and residual == log_residual == 0,
                 "balance residual prevents exact profit attribution")
    return dict(profit)


def window_profit(report: dict, tokens: dict, wallet: str, start: int, end: int) -> dict[str, int]:
    """Return microUSD per participating condition, or raise ValueError for unusable evidence."""
    try:
        return _profit(report, tokens, wallet, start, end)
    except (KeyError, TypeError, AttributeError, IndexError) as err:
        raise ValueError(f"missing or malformed absorption/token evidence: {err}") from err
