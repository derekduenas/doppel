"""Python side of the crates/doppel-fair parity fixtures (tests/fixtures_fair/).

The Rust test (crates/doppel-fair/tests/parity.rs) checks the ports against the outputs stored in
the fixtures; these tests check that the stored outputs are what the Python references produce
today, so a drift in either language is caught. The BRTI reference is PERELMAN's brti.py, vendored
verbatim (perelman_brti.py), so nothing here needs ~/perelman.
"""

import hashlib
import importlib.util
import json
import math
import warnings
from pathlib import Path

import numpy as np
import pytest

from doppel.lab.fair import fair_up_raw

FIX = Path(__file__).parent / "fixtures_fair"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_fixtures", FIX / "gen_fixtures.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _gen()


@pytest.fixture(scope="module")
def fair_fx():
    return json.loads((FIX / "fair_v1.json").read_text())


@pytest.fixture(scope="module")
def replica_fx():
    return json.loads((FIX / "brti_replica.json").read_text())


def _close(a, b, tol):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def test_fair_inputs_are_reproducible(fair_fx):
    paths = gen.fair_paths()
    assert paths == fair_fx["paths"]
    cases = gen.fair_cases(paths)
    assert [{k: v for k, v in c.items() if k != "p"} for c in fair_fx["cases"]] == cases


def test_fair_outputs_match_fair_py(fair_fx):
    got = gen.fair_outputs(fair_fx["paths"], fair_fx["cases"])
    want = [c["p"] for c in fair_fx["cases"]]
    bad = [(i, g, w) for i, (g, w) in enumerate(zip(got, want)) if not _close(g, w, 1e-12)]
    assert not bad, bad[:5]
    assert sum(w is None for w in want) > 100 and sum(w not in (None, "0.0", "1.0") for w in want) > 1000
    assert "0.5" in want  # exact ties (diff == 0 with variance left) are covered


def test_fair_py_never_reads_after_decision(fair_fx):
    p = fair_fx["paths"][1]
    raw = np.array([gen.s2f(v) for v in p["raw"]])
    for c in fair_fx["cases"][::37]:
        if c["path"] != 1:
            continue
        g = raw.copy()
        g[max(0, c["d"] - p["t0"] + 1):] = 1e12
        args = (float(c["ptb"]), c["d"], c["end"], c["lag"], c["vol_window"])
        assert fair_up_raw(g, p["t0"], *args) == fair_up_raw(raw, p["t0"], *args)


def test_vendored_brti_is_the_recorded_perelman_source(replica_fx):
    sha = hashlib.sha256((FIX / "perelman_brti.py").read_bytes()).hexdigest()
    assert sha == replica_fx["meta"]["brti_py_sha256"]
    live = Path.home() / "perelman" / "brti.py"
    if live.exists() and hashlib.sha256(live.read_bytes()).hexdigest() != sha:
        warnings.warn("~/perelman/brti.py differs from the vendored copy: re-vendor with "
                      "gen_fixtures.py --vendor and re-check the Rust port", stacklevel=1)


def test_replica_outputs_match_perelman_brti(replica_fx):
    brti = gen.load_ref()
    scenarios = json.loads(json.dumps(replica_fx["scenarios"]))
    index = json.loads(json.dumps(replica_fx["index_cases"]))
    for s in scenarios:
        for st in s["steps"]:
            st.pop("expect")
    gen.replica_outputs(scenarios, index, brti)
    assert scenarios == replica_fx["scenarios"]
    assert index == replica_fx["index_cases"]
    valued = [st["expect"] for s in scenarios for st in s["steps"] if "value" in st["expect"]]
    assert len(valued) >= 18
    assert {"all books delayed or erroneous", "all books potentially erroneous"} <= {
        st["expect"].get("error") for s in scenarios for st in s["steps"]}


def test_replica_synthetic_inputs_are_reproducible(replica_fx):
    names = [s["name"] for s in gen.replica_scenarios()]
    assert names == [s["name"] for s in replica_fx["scenarios"]]
    fresh = {s["name"]: s for s in gen.replica_scenarios()}
    for s in replica_fx["scenarios"]:
        for a, b in zip(fresh[s["name"]]["steps"], s["steps"]):
            assert a["venues"] == b["venues"] and a["calc_ns"] == b["calc_ns"]


def test_float_encoding_round_trips():
    for x in (0.1, 60258.69660259405, 1e-300, 3.5000000000000004):
        assert float(gen.f2s(x)) == x
    assert gen.f2s(float("nan")) is None and math.isnan(gen.s2f(None))
