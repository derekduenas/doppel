import orjson, numpy as np, collections
from pathlib import Path
from doppel.tape.segment import read_segment
W = "0x0cb038487586d1119b165466072e9baf666f3a90"; D = Path("/mnt/volume_nyc1_1793485122853/doppel")
rows = [orjson.loads(l) for l in open(D / "lab/orders/sample-0x0cb0.jsonl", "rb")]
T0 = 1795629600  # 2026-11-25T18:00Z tape start
od = {}
for r in rows:
    if r["created_ms"] / 1000 >= T0 + 120 and 0 <= r["age_s"] < 600 and r["role"] == "maker":
        od.setdefault(r["order_id"], r)
mine = [o for o in od.values() if o["maker"] == W]; oth = [o for o in od.values() if o["maker"] != W]
toks = {o["token_id"] for o in od.values()}
print("orders in tape range: wallet", len(mine), "others", len(oth), "tokens", len(toks))
# level deltas per (asset, price, side): list of (server_ms, tw_ms, delta, new_size)
lev = {}; ev = collections.defaultdict(list); seen_tok = set()
for stream in ("clob_btc_5m", "clob_btc_15m", "clob_eth_5m", "clob_eth_15m", "clob_sol_5m", "clob_sol_15m", "clob_xrp_5m", "clob_xrp_15m"):
    for hh in ("18", "19"):
        f = D / "tape" / stream / "20261125" / f"{hh}.dtape.zst"
        if not f.exists(): continue
        for rec in read_segment(f, verify=False):
            raw = rec.get("raw") or ""
            if not raw or raw[0] not in "[{": continue
            try: ms = orjson.loads(raw)
            except Exception: continue
            for m in ms if isinstance(ms, list) else [ms]:
                if not isinstance(m, dict): continue
                et = m.get("event_type")
                if et == "book" and m.get("asset_id") in toks:
                    a = m["asset_id"]; seen_tok.add(a)
                    for side, key in (("BUY", "bids"), ("SELL", "asks")):
                        for k in [k for k in lev if k[0] == a and k[2] == side]: lev[k] = 0.0
                        for lv in m.get(key) or []: lev[(a, round(float(lv["price"]), 4), side)] = float(lv["size"])
                elif et == "price_change":
                    ts = int(m.get("timestamp") or 0)
                    for ch in m.get("price_changes") or []:
                        a = ch.get("asset_id")
                        if a not in toks: continue
                        k = (a, round(float(ch["price"]), 4), ch["side"]); new = float(ch["size"])
                        if k in lev or a in seen_tok:
                            ev[k].append((ts, rec["tw"] // 10**6, new - lev.get(k, 0.0)))
                        lev[k] = new
def match(o, lo=-300, hi=2500, tol=0.02):
    k = (o["token_id"], round(o["limit"], 4), o["side"]); best = None
    for ts, tw, d in ev.get(k, ()):
        dt = ts - o["created_ms"]
        if lo <= dt <= hi and abs(d - o["size"]) <= tol * max(1.0, o["size"]):
            if best is None or abs(dt) < abs(best[0]): best = (dt, tw - o["created_ms"])
    return best
for name, os_ in (("wallet", mine), ("others", oth)):
    has = [o for o in os_ if (o["token_id"], round(o["limit"], 4), o["side"]) in ev]
    m = [match(o) for o in has]; hit = [x for x in m if x]
    print(name, "orders", len(os_), "with any level events at their price", len(has), "| exact-size increase near created_ms", len(hit),
          "| venue ts - created ms p10/50/90", [int(np.quantile([h[0] for h in hit], q)) for q in (.1, .5, .9)] if hit else None,
          "| our receive - created ms p50", int(np.median([h[1] for h in hit])) if hit else None)
# placebo: same test with the time window shifted by 60 s (chance matches)
pl = [match(o, lo=60000 - 300, hi=60000 + 2500) for o in mine if (o["token_id"], round(o["limit"], 4), o["side"]) in ev]
print("placebo (+60 s window) wallet matches", sum(x is not None for x in pl), "of", len(pl))
