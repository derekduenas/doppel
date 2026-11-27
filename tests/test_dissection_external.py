from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import orjson
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug

# Use the actual historical evidence program for the cutoff regression.
path = Path(__file__).resolve().parents[1] / "evidence/2026-11-27-wallet-dissection/case_crosscheck.py"
spec = spec_from_file_location("dissection_case", path)
case = module_from_spec(spec)
spec.loader.exec_module(case)


def test_external_case_excludes_exact_and_later_records_in_each_stream(tmp_path):
    start = 1795629600
    pm_slug = pmus_slug("btc", "5m", start)
    ticker = "KXBTC15M-26NOV251315-15"
    cutoff = (start+2)*10**9
    marks = {"test": {"tw_ns": cutoff}}
    def row(stream, price):
        px = str(price)
        if stream == "spot_coinbase":
            return {"type": "match", "product_id": "BTC-USD", "price": px, "time": "test"}, None
        if stream == "kalshi_books":
            return {"orderbook_fp": {"yes_dollars": [[px, 5]], "no_dollars": [["0.02", 7]]}}, {"key":ticker,"status":200}
        return {"marketData": {"marketSlug":pm_slug,"bids":[{"px":{"value":px},"qty":"5"}],
                               "offers":[{"px":{"value":"0.99"},"qty":"7"}]}}, {"key":pm_slug,"status":200}
    for stream in ("pmus_books", "kalshi_books", "spot_coinbase"):
        writer = SegmentWriter(tmp_path, stream)
        for t,p in ((cutoff-1,.1),(cutoff,.75),(cutoff+1,.9)):
            raw, meta = row(stream, p)
            writer.append(orjson.dumps(raw),tw=t,meta=meta)
        writer.close()
    report = case.public_context(tmp_path, marks, start)
    for stream, result in report.items():
        val = result["states"]["test"]
        assert val["seq"] == 1 and val["tw_ns"] == cutoff-1
        assert val["trade_price_usd" if stream=="spot_coinbase" else "bid"] == .1
