import orjson, numpy as np, pyarrow.parquet as pq, collections
W = "0x0cb038487586d1119b165466072e9baf666f3a90"; L = "/mnt/volume_nyc1_1793485122853/doppel/lab"
rows = [orjson.loads(l) for l in open(f"{L}/orders/sample-0x0cb0.jsonl", "rb")]
fills = pq.read_table(f"{L}/dossiers/{W}.fills.parquet", columns=["tx", "order_id", "role", "shares", "price", "side"]).to_pylist()
txs = {r["tx"] for r in rows}
gf = [f for f in fills if f["tx"] in txs]
ids = {(r["tx"], r["order_id"]) for r in rows}
print("txs decoded", len(txs), "orders", len(rows), "| wallet fills in those txs", len(gf), "with a decoded order of the same id", sum((f["tx"], f["order_id"]) in ids for f in gf))
q = lambda x: [round(float(v), 3) for v in np.quantile(x, [.01, .1, .25, .5, .75, .9, .99])] if len(x) else None
mine = {}
for r in rows:
    if r["maker"] == W: mine.setdefault(r["order_id"], []).append(r)
print("wallet orders", len(mine), "roles", collections.Counter(v[0]["role"] for v in mine.values()), "sig_type", collections.Counter(v[0]["sig_type"] for v in mine.values()))
for role in ("maker", "taker"):
    o = [v for v in mine.values() if v[0]["role"] == role]
    if not o: continue
    age = [min(x["age_s"] for x in v) for v in o]   # age at first fill seen
    filled = [sum(x["fill_amount"] for x in v) / 1e6 for v in o]
    size = [v[0]["size"] for v in o]
    frac = [min(1.0, (f / (s if v[0]["side"] == "SELL" else s * v[0]["limit"])) if s else 0) for f, s, v in zip(filled, size, o)]
    print(role, "n", len(o), "| age at first fill s p1..p99", q(age), "| order size shares", q(size), "| filled fraction (in sample)", q(frac))
    print("   limit prices", q([v[0]["limit"] for v in o]), "| top sizes", collections.Counter(round(s, 1) for s in size).most_common(6))
oth = [r for r in rows if r["maker"] != W]
print("counterparty orders", len(oth), "age p1..p99 maker", q([r["age_s"] for r in oth if r["role"] == "maker"]), "taker", q([r["age_s"] for r in oth if r["role"] == "taker"]))
print("negative ages", sum(r["age_s"] < 0 for r in rows), "of", len(rows), "| builders", collections.Counter(r["builder"][:8] for r in rows).most_common(4))
