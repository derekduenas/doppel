"""Paper journal -> (a) the public footprint a real wallet would leave and (b) the full decision
record, so DOPPEL's reconstruction tools can be scored on accounts whose rules we hold (gap G3).

Layout (the answer-key lab's, ``lab/answerkey.write_public``):
  public/orders.jsonl   ``lab.ordertape.SCHEMA`` rows for orders that filled at least once, one row
                        per fill as a complete call (our verified row + the anonymous counterparty),
                        what ``doppel-ordermatch --orders`` and ``doppel-rulefit --orders`` read
  public/goldsky/       ``goldsky_activity`` tape of our fills, compacted to public/fills/ by
                        ``lab.fillstore.compact`` (what absorb and fidelity read)
  truth/decisions.jsonl every journal record (decisions with their feature vectors, intents, acks,
                        skips, fills, risk events, settlements), chain fields removed
  truth/orders.jsonl    every paper order, filled or not, with its life: decided, landed, ended,
                        status, fills
  footprint.json        wallet, counts, sha256 of each file, the journal head hash
  NEVER in public/: unfilled, cancelled or rejected orders, skips, features, parameters.

Venue mapping. PM US trades one YES instrument; the reconstruction tools follow intl, where Up and
Down are separate tokens bought with BUY orders. So BUY YES at p is BUY token ``pmus:<slug>:up`` at
p, and SELL YES at p is BUY token ``pmus:<slug>:down`` at 1 - p (economically the same position).
Order sizes, limits and ``created_ms`` (the decision time) are the paper order's own. Block times
are synthetic: ceil(fill second) + 1, like the answer key.

Known limit: ``doppel-ordermatch`` and ``doppel-rulefit`` locate orders in recorded INTL CLOB books
(``lab.intlbook``). Paper quotes rest in a PM US book that no intl stream contains, so placement
matching needs a PM US book overlay like the answer key's rewritten tape. That overlay is not built.
The order rows load (tested through ``ordermatch.load_orders``); matching them to a book is open.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import orjson

from ..lab.fillstore import compact
from ..tape.segment import SegmentWriter
from .journal import load, strip

JOB = "cohort-paper"
ZERO32 = "0" * 64
MS = 1_000_000


def _h(*parts) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()


def wallet_of(head: dict) -> str:
    return "0x" + _h("doppel-paper", head["genome_id"], head["t"], head["h"])[:40]


def token_view(slug: str, yes_side: str, px: float) -> tuple[str, float]:
    """(token id, price in that token) for a YES buy/sell on PM US."""
    return (f"pmus:{slug}:up", px) if yes_side == "buy" else (f"pmus:{slug}:down", round(1.0 - px, 6))


def orders_from_journal(recs: list[dict]) -> list[dict]:
    """Every paper order with its life, from intent / ack / fill records."""
    orders: dict[str, dict] = {}
    by_ver: dict[tuple[str, str, int], str] = {}
    live: dict[tuple[str, str], str] = {}
    for r in recs:
        k, slug = r["k"], str(r.get("slug"))
        if k == "intent" and r["kind"] in ("post", "amend"):
            orders[r["oid"]] = {"oid": r["oid"], "slug": slug, "kind": "maker",
                                "yes_side": "buy" if r["side"] == "bid" else "sell", "px": r["px"],
                                "size": r["size"], "decided_ns": r["t"], "land_ns": None, "end_ns": None,
                                "status": "pending", "fills": []}
            by_ver[(slug, r["side"], r["ver"])] = r["oid"]
        elif k == "intent" and r["kind"] == "take":
            orders[r["oid"]] = {"oid": r["oid"], "slug": slug, "kind": "taker", "yes_side": r["side"],
                                "px": r["limit"], "size": r["qty"], "decided_ns": r["t"], "land_ns": r["land"],
                                "end_ns": r["land"], "status": "pending", "fills": [], "rule": r["rule"]}
        elif k == "ack" and "ver" in r:
            side = r["side"]
            if r["status"] != "superseded" and (r.get("replaced") or r["status"] == "cancelled"):
                old = live.pop((slug, side), None)
                if old is not None and orders[old]["status"] == "live":
                    orders[old].update(status="cancelled", end_ns=r["t"])
            oid = by_ver.get((slug, side, r["ver"]))
            if oid is None:
                continue
            o = orders[oid]
            o["status"] = r["status"]
            if r["status"] == "live":
                o["land_ns"] = r["t"]
                live[(slug, side)] = oid
        elif k == "ack" and "oid" in r:
            orders[r["oid"]]["status"] = r["status"]
        elif k == "fill" and r.get("oid") in orders:
            o = orders[r["oid"]]
            o["fills"].append({"t_ns": r["t"], "px": r["px"], "qty": r["qty"], "fee": r["fee"], "via": r["via"]})
            if o["kind"] == "maker" and sum(f["qty"] for f in o["fills"]) >= o["size"] - 1e-9:
                o.update(status="filled", end_ns=r["t"])
        elif k == "settle":
            for (s, side), oid in list(live.items()):
                if s == slug:
                    if orders[oid]["status"] == "live":
                        orders[oid].update(status="expired", end_ns=r["end"] * 10**9)
                    del live[(s, side)]
    return list(orders.values())


def ordertape_rows(orders: list[dict], wallet: str) -> list[dict]:
    rows: list[dict] = []
    for o in orders:
        if not o["fills"]:
            continue
        token, limit = token_view(o["slug"], o["yes_side"], o["px"] if o["px"] is not None else o["fills"][0]["px"])
        ma, ta = round(limit * o["size"] * 1e6), round(o["size"] * 1e6)
        created_ms = o["decided_ns"] // MS
        for j, f in enumerate(o["fills"]):
            tx = _h("tx", wallet, o["oid"], j)
            bts = math.ceil(f["t_ns"] / 1e9) + 1
            q = f["qty"]
            base = {"tx": tx, "block": 60_000_000 + bts // 2, "block_ts": bts, "token_id": token, "builder": ZERO32,
                    "condition_id": o["slug"], "n_makers": 1, "sig_type": 0}
            me = base | {"order_id": _h("order", wallet, o["oid"]), "maker": wallet, "signer": wallet, "side": "BUY",
                         "size": o["size"], "limit": limit, "maker_amount": ma, "taker_amount": ta,
                         "fill_amount": round(q * limit * 1e6), "fee_amount": round(f["fee"] * 1e6),
                         "created_ms": created_ms, "age_s": round(bts - created_ms / 1000, 3), "ts_flag": None,
                         "verified": True, "salt": _h("salt", o["oid"])[:12], "role": o["kind"]}
            cp = "0x" + _h("counterparty", tx)[:40]
            other = base | {"order_id": _h("cporder", tx), "maker": cp, "signer": cp, "side": "SELL", "size": q,
                            "limit": limit, "maker_amount": round(q * 1e6), "taker_amount": round(q * limit * 1e6),
                            "fill_amount": round(q * 1e6), "fee_amount": 0,
                            "role": "taker" if o["kind"] == "maker" else "maker",
                            "created_ms": created_ms - 1000, "age_s": round(bts - created_ms / 1000 + 1, 3),
                            "ts_flag": None, "verified": False, "salt": _h("salt", tx)[:12]}
            rows += [other, me] if o["kind"] == "maker" else [me, other]
    return rows


def goldsky_rows(orders: list[dict], wallet: str) -> list[dict]:
    out = []
    for o in orders:
        for j, f in enumerate(o["fills"]):
            token, price = token_view(o["slug"], o["yes_side"], f["px"])
            tx = _h("tx", wallet, o["oid"], j)
            bts = math.ceil(f["t_ns"] / 1e9) + 1
            out.append({"block_number": 60_000_000 + bts // 2, "id": f"log_{tx}_{j}", "transaction_hash": tx,
                        "address": wallet, "token_id": token, "amount_usdc": round(f["qty"] * price, 6),
                        "amount_shares": f["qty"], "price": price, "event_type": "TRADE", "side": "BUY",
                        "order_id": _h("order", wallet, o["oid"]), "counterparty_address": None,
                        "order_type": o["kind"], "fee": f["fee"], "builder": ZERO32, "block_timestamp": bts})
    return out


def convert(journal_dir: Path, out: Path, fillstore: bool = True) -> dict:
    recs = load(journal_dir)
    head = recs[0]
    wallet = wallet_of(head)
    orders = orders_from_journal(recs)
    pub, truth = Path(out) / "public", Path(out) / "truth"
    pub.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)
    rows = ordertape_rows(orders, wallet)
    (pub / "orders.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    gs = goldsky_rows(orders, wallet)
    if fillstore and gs:
        w = SegmentWriter(pub / "goldsky", "goldsky_activity")
        for i in range(0, len(gs), 500):
            w.append(orjson.dumps({"data": gs[i:i + 500]}).decode(),
                     meta={"job": JOB, "status": 200, "url": "doppel-paper", "rows": len(gs[i:i + 500])})
        w.close()
        compact(pub / "goldsky", pub / "fills")
    (truth / "decisions.jsonl").write_bytes(b"".join(orjson.dumps(strip(r)) + b"\n" for r in recs if r["k"] != "cycle"))
    (truth / "orders.jsonl").write_bytes(b"".join(orjson.dumps(o) + b"\n" for o in orders))
    files = ("public/orders.jsonl", "truth/decisions.jsonl", "truth/orders.jsonl")
    doc = {"wallet": wallet, "genome_id": head["genome_id"], "journal_head": recs[-1]["h"],
           "journal_records": len(recs), "orders": len(orders), "orders_filled": sum(1 for o in orders if o["fills"]),
           "order_rows": len(rows), "fills": len(gs),
           "sha256": {f: hashlib.sha256((Path(out) / f).read_bytes()).hexdigest() for f in files},
           "venue_mapping": "BUY YES -> BUY pmus:<slug>:up at p; SELL YES -> BUY pmus:<slug>:down at 1-p",
           "not_built": "PM US book overlay for ordermatch/rulefit placement matching"}
    (Path(out) / "footprint.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    return doc


def fills_from_public(orders_jsonl: Path, wallet: str) -> list[tuple]:
    """The inverse view: (order id, token, limit, shares, block_ts) of the wallet's verified rows."""
    from ..lab.ordermatch import filled_shares
    out = []
    for line in Path(orders_jsonl).read_bytes().splitlines():
        r = orjson.loads(line)
        if r["maker"] == wallet and r["verified"]:
            out.append((r["order_id"], r["token_id"], r["limit"], round(filled_shares(r), 6), r["block_ts"]))
    return sorted(out)
