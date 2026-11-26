"""Select the frozen validation sample, then replay it with the reconstruction adapter."""

import argparse
import hashlib
from collections import Counter
from pathlib import Path

import orjson

from doppel.lab import ordermatch, reconstruct
from doppel.lab.ledger import TokenMap


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=("match", "replay"))
    ap.add_argument("archive", type=Path)
    ap.add_argument("run", type=Path)
    args = ap.parse_args()
    spec_file = Path(__file__).with_name("fresh-sample.json")
    spec = orjson.loads(spec_file.read_bytes())
    start, end = spec["sample_from"], spec["sample_to"]
    wallet, stream = spec["wallet"], spec["stream"]
    archive, orders, out = args.archive, args.run / "orders", args.run / "fresh"
    tape = archive / "tape"
    om = out / "ordermatch"
    if args.stage == "match":
        om.mkdir(parents=True, exist_ok=False)
        p = ordermatch.Params(size_tol_rel=spec["size_tol_rel"])
        screened = [o for o in ordermatch.load_orders(orders, start, end)[0] if o.maker == wallet]
        tokens = TokenMap(archive / "lab/tokens.jsonl").m
        names = {o.order_id: (tokens.get(o.token, {}).get("slug") or "") for o in screened}
        ids = {oid for oid, slug in names.items() if slug.startswith("btc-updown-5m-")}
        _, rows, _ = ordermatch.run(tape, orders, spec["warmup_from"], end, [wallet], p, [stream],
                                     focus=True, quote_sink=lambda _: None)
        selected = [r for r in rows if r["maker"] == wallet and start * 1000 <= r["created_ms"] < end * 1000]
        # The stream itself identifies its matched markets even if the token map has no slug.
        ids.update(r["order_id"] for r in selected)
        matched = {r["order_id"] for r in selected}
        (om / "orders.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in selected))
        report = {"spec_sha256": hashlib.sha256(spec_file.read_bytes()).hexdigest(),
                  "screened_wallet_orders_all_markets": len(screened),
                  "selected_wallet_btc5m_orders": len(ids), "wallet_orders_without_token_slug":
                  sorted(oid for oid, slug in names.items() if not slug), "matched": len(matched),
                  "unmatched_ids": sorted(ids - matched),
                  "independently_identified_placements": 0, "outcomes": dict(Counter(r["outcome"] for r in selected))}
        (out / "selection.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    else:
        c = reconstruct.Cfg(data=tape, orders=orders, store=args.run, cohort=spec_file,
                            t_from=start, t_to=end, out=out, wallets=(wallet,))
        status = reconstruct._unit_orderreplay(c, out / "orderreplay",
                                                {"from": spec["warmup_from"], "to": end}, [stream])
        (out / "replay-status.json").write_bytes(orjson.dumps(status, option=orjson.OPT_INDENT_2))


if __name__ == "__main__":
    main()
