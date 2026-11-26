"""H3 fifth arm (audit 2026-11-26, A2-2): block placebo. The original placebo pauses on i.i.d.
seconds (~40 pause episodes per window, each re-post loses queue priority), while the flow guards
pause in runs (30 s rolling sums). This arm pauses whole 30 s blocks with the same 17.4% probability,
so it matches the guards' pause STRUCTURE as well as their rate. Same frozen policy, same window.
"""
import orjson
from pathlib import Path

from doppel.strategy.genome import Condition, from_dict, to_dict, validate

SRC = Path(__file__).parent / "h3" / "h3-placebo.json"
d = orjson.loads(SRC.read_bytes())
d["name"] = "h3-placebo-block"
d["maker"]["pause_when"] = [c for c in d["maker"]["pause_when"] if c["feature"] != "placebo_u"] + \
    [{"feature": "placebo_block_u", "op": "<", "value": 0.1742, "value_hi": None}]
d["evidence"] = {**d["evidence"], "arm": "placebo_block",
                 "basis": "audit A2-2: i.i.d.-second placebo is not like-for-like; 30 s block placebo"}
d["source"] = {**d["source"], "operator": "manual"}
g = from_dict(d)
assert validate(g) == [], validate(g)
(Path(__file__).parent / "h3" / f"{g.name}.json").write_bytes(orjson.dumps(to_dict(g), option=orjson.OPT_INDENT_2))
print(g.id[:12], g.name, [c.feature for c in g.maker.pause_when])
