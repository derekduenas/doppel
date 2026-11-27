from copy import deepcopy
from pathlib import Path

import orjson
import pytest

from doppel.lab import rivals

PLAN_FILE = Path(__file__).resolve().parents[1] / "evidence/2026-11-27-wallet-rivals/registration.json"


def material(scope="controlled_holdout"):
    plan, sha = rivals.read(PLAN_FILE)
    split = plan["splits"][scope]
    cells = []
    for t in range(split["start"], split["end"], 10):
        shares = 1600 if t % 20 == 0 else 1601
        cells.append({"cutoff_s":t, "capture":"covered",
                      "book":{"bid":.07,"ask":.08,"bid_shares":shares,"depth_tw_ns":t*rivals.NS-1,
                              "identity_tw_ns":split["start"]*rivals.NS-1}})
    public = {"schema":"rival-public/1", **{k:split[k] for k in ("scope","start","end")}, "cells":cells}
    return plan, sha, public


def test_disagreement_threshold_prior_state_and_capture_mask():
    plan, sha, pub = material()
    cells = pub["cells"]
    cells[1]["capture"] = "gap"
    for idx, field, offset in ((2,"depth_tw_ns",0),(3,"identity_tw_ns",0),(4,"depth_tw_ns",-3_000_000_001)):
        cells[idx]["book"][field] = cells[idx]["cutoff_s"]*rivals.NS + offset
    cells[5]["book"]["ask"] = .06
    out = rivals.forecast(plan, sha, pub, "source")["cells"]
    assert out[0]["queue_at_most"] is True
    assert out[1]["touch"] is True and out[1]["queue_at_most"] is False and out[1]["score_exclusion"]=="capture_gap"
    assert [row["score_exclusion"] for row in out[2:6]] == [
        "missing_prior_depth","missing_prior_identity","stale_depth","invalid_two_sided_quote"]
    assert all(row["touch"] is None for row in out[2:6])
    cells[0]["action"] = True
    with pytest.raises(ValueError, match="public cell"):
        rivals.forecast(plan,sha,pub,"source")


def test_full_grid_no_pooling_and_unknowns_remain_unknown():
    plan, sha, pub = material()
    pred = rivals.forecast(plan, sha, pub, "source")
    def labels_for(markets):
        return [{"cutoff_s":r["cutoff_s"], "action":r["queue_at_most"] if i < 30*markets else None}
                for i,r in enumerate(pred["cells"])]
    result = rivals.evaluate(pred, labels_for(7), "action", plan)
    assert result["counts"]["scored"] == 210 and result["counts"]["unknown_target"] == 150
    assert result["winner"] is None and result["verdict"] == "abstain"
    result = rivals.evaluate(pred, labels_for(8), "action", plan)
    assert result["winner"] == "queue_at_most" and result["sign_test_two_sided_p"] == 2/256
    assert result["mean_market_accuracy"]["never_bid"] == .5
    changed = deepcopy(pub)
    changed["cells"][1] = changed["cells"][0]
    with pytest.raises(ValueError,match="every registered cutoff"):
        rivals.forecast(plan,sha,changed,"source")


def test_real_execution_is_separate_even_with_perfect_accuracy(tmp_path):
    plan, sha, pub = material("real_holdout")
    def put(name, data):
        path = tmp_path/name
        path.write_bytes(rivals.encode(data))
        return path
    public = put("public.json", pub)
    prediction_file = tmp_path/"pred.json"
    common = ["--plan",str(PLAN_FILE),"--plan-sha256",sha,"--public",str(public)]
    assert rivals.main(["predict",*common,"--out",str(prediction_file)]) == 0
    pred, predsha = rivals.read(prediction_file)
    rows = [{"cutoff_s":p["cutoff_s"],"action":None,"execution":p["touch"]} for p in pred["cells"]]
    subjects = {w:deepcopy(rows) for w in plan["splits"]["real_holdout"]["subjects"]}
    doc = {"schema":"rival-labels/1","scope":"real","start":pub["start"],"end":pub["end"],
           "public_sha256":pred["public_sha256"],"predictions_sha256":predsha,"subjects":subjects}
    labels = put("labels.json",doc)
    out = tmp_path/"results.json"
    args = ["score",*common,"--predictions",str(prediction_file),"--labels",str(labels),"--out",str(out)]
    assert rivals.main(args) == 0
    result = orjson.loads(out.read_bytes())
    for wallet in result["subjects"].values():
        assert wallet["action"]["counts"]["scored"] == 0
        assert wallet["execution"]["mean_market_accuracy"]["touch"] == 1
        assert wallet["execution"]["winner"] is None and wallet["action"]["winner"] is None
    subjects[next(iter(subjects))][0]["action"] = True
    put("labels.json",doc)
    with pytest.raises(ValueError,match="cannot certify"):
        rivals.main(args)
    doc["predictions_sha256"] = "stale"
    put("labels.json",doc)
    with pytest.raises(ValueError, match="population and prediction"):
        rivals.main(args)
    pred["cells"][0]["touch"] = False
    put("pred.json",pred)
    with pytest.raises(ValueError,match="forecast differs"):
        rivals.main(args)


def test_registered_vote_gate_cannot_favor_opposite_mean_on_twelve_markets():
    # With <=12 bounded differences and p<=.01, the losing side could never supply
    # an effect of .20 in the opposite direction, even if every dissenting vote was maximal.
    for n in range(8,13):
        for votes in range(n//2+1,n+1):
            if rivals.sign_test(votes,n-votes) <= .01:
                assert (n-votes)/n < .20
