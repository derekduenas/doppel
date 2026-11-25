import copy
import math

import numpy as np
import orjson
import pytest
from sklearn.tree import DecisionTreeClassifier, export_text

import doppel.lab.dossier as dz
from doppel.lab.dossier import BLOCK_SLACK_S, NS, BookIndex, Brti
from doppel.lab.ledger import TokenMap
from doppel.strategy import extract as ex
from doppel.strategy.cli import main as cli
from doppel.strategy.genome import from_dict, to_dict, validate

T0 = 1795046400
WALLET = "0xfeedbeef00112233"


def _tok(token, slug, idx, payout=None, cond="0xm"):
    return {"token": token, "slug": slug, "idx": idx, "condition": cond, "payout": payout,
            "status": "resolved" if payout is not None else "open"}


def _row(i, tok, side, price, sh, t, role, oid):
    return {"id": f"r{i}", "token_id": tok, "side": side, "amount_shares": sh, "amount_usdc": round(sh * price, 6),
            "fee": 0.0, "block_timestamp": t, "block_number": i, "order_type": role, "price": price,
            "order_id": oid, "counterparty_address": None, "transaction_hash": None}


def _tmap(tmp_path, recs):
    tm = TokenMap(tmp_path / "t.jsonl")
    tm.m = {r["token"]: r for r in recs}
    return tm


def _brti(rng, n_win, first):
    n = (first - T0) + n_win * 300 + 100
    raw = {T0 + i: 60000 + float(v) for i, v in enumerate(np.cumsum(rng.normal(0, 4, n)))}
    return raw, Brti(raw, dict(raw), 0)  # published series = raw: priceToBeat = raw[start-1]


def _as_written(doc):
    """The dossier exactly as doppel-dossier writes it to JSON (NaN -> null, numpy -> python)."""
    return orjson.loads(orjson.dumps(dz._jsonable(doc), option=orjson.OPT_INDENT_2))


# ---------------------------------------------------------------- tree rules text
def _fitted_tree(names, seed=0, n=3000):
    rng = np.random.default_rng(seed)
    X = np.column_stack([rng.uniform(-0.2, 0.2, n), rng.uniform(0, 300, n), rng.normal(0, 20, n)])
    y = ((X[:, 0] > 0.05) & (X[:, 1] < 120)).astype(int)
    t = DecisionTreeClassifier(max_depth=3, class_weight="balanced", min_samples_leaf=20, random_state=1)
    return t.fit(X, y), X


@pytest.mark.parametrize("show_weights", [False, True])
def test_parse_real_export_text_paths(show_weights):
    names = ["edge_up", "tte", "mv5"]
    tree, X = _fitted_tree(names)
    text = export_text(tree, feature_names=names, show_weights=show_weights, decimals=6)
    leaves = ex.parse_tree_rules(text)
    assert len(leaves) == tree.get_n_leaves()
    assert all(lf.cls in ("0", "1") for lf in leaves)
    assert all((lf.weights is not None) == show_weights for lf in leaves)
    # every sample falls in exactly one parsed leaf, whose class is the tree's prediction
    pred = tree.predict(X[:400])
    for x, p in zip(X[:400], pred):
        hit = [lf for lf in leaves if all((x[names.index(s.feature)] <= s.threshold) == (s.op == "<=")
                                          for s in lf.path)]
        assert len(hit) == 1 and hit[0].cls == str(p)


def test_parse_default_format_and_collapse():
    text = ("|--- tte <= 119.99\n"
            "|   |--- edge_up <= 0.05\n"
            "|   |   |--- edge_up <= -2.07\n"
            "|   |   |   |--- class: 0\n"
            "|   |   |--- edge_up >  -2.07\n"
            "|   |   |   |--- class: 0\n"
            "|   |--- edge_up >  0.05\n"
            "|   |   |--- class: 1\n"
            "|--- tte >  119.99\n"
            "|   |--- truncated branch of depth 2\n")
    leaves = ex.parse_tree_rules(text)
    assert [lf.cls for lf in leaves] == ["0", "0", "1", None]
    assert leaves[2].path == (ex.Split("tte", "<=", 119.99), ex.Split("edge_up", ">", 0.05))
    assert leaves[1].path[-1] == ex.Split("edge_up", ">", -2.07)
    col = ex.collapse(leaves)
    assert [lf.cls for lf in col] == ["0", "1", None]          # the two class-0 siblings merged
    assert col[0].path == (ex.Split("tte", "<=", 119.99), ex.Split("edge_up", "<=", 0.05))
    assert ex.intervals(col[1].path) == {"tte": (-math.inf, 119.99), "edge_up": (0.05, math.inf)}
    assert ex.intervals((ex.Split("x", ">", 0.05), ex.Split("x", "<=", 0.05))) is None
    with pytest.raises(ValueError):
        ex.parse_tree_rules("|--- edge_up ~ 3\n")


def _mini_doc(took_rules, side=None, **over):
    """Smallest dossier dict the taker extractor reads (shape as doppel-dossier writes it)."""
    doc = {"wallet": WALLET,
           "coverage": {"fills": 900, "btc_fills": 900, "fair_share_btc": 1.0, "book_share": 1.0},
           "mechanism": {"fills": 900, "windows": 300, "maker_share": 0.0, "mix": {"btc-5m": 900},
                         "tte_s_taker": {"p10": 10.0, "p50": 60.0, "p90": 110.0, "n": 900}},
           "orders": {"taker_orders": 900, "maker_orders": 0, "shares_per_order": {"p50": 50.0, "n": 900}},
           "inventory": {}, "edge": {}, "markouts": {},
           "policy": {"verdict": "EXPLAINED",
                      "took_any": {"train_pos": 600, "test_pos": 300, "auc_hgb": 0.9, "auc_tree": 0.85,
                                   "features_used": ["edge_up", "tte"], "features_dropped": [],
                                   "rules": took_rules},
                      "side_given_took": side or {"train_up": 600, "train_down": 0, "test_up": 300,
                                                  "test_down": 0, "result": "insufficient"}}}
    doc.update(over)
    return doc


def test_unmappable_feature_refuses_whole_leaf():
    names = ["depth_imbalance", "tte", "mv5"]   # first column has no genome equivalent
    tree, _ = _fitted_tree(names)
    text = export_text(tree, feature_names=names)
    refusals = []
    gs = ex.taker_genomes(_mini_doc(text), refusals=refusals)
    assert gs == []
    leaf_ref = [r for r in refusals if r["scope"] == "leaf"]
    assert leaf_ref and all("depth_imbalance" in r["reason"] for r in leaf_ref)
    assert any(r["scope"] == "genome" for r in refusals)
    # mapped leaves still emit; only the unmappable one is refused
    text2 = ("|--- tte <= 119.50\n|   |--- edge_up >  0.05\n|   |   |--- class: 1\n"
             "|   |--- edge_up <= 0.05\n|   |   |--- depth_imbalance >  3.00\n|   |   |   |--- class: 1\n"
             "|   |   |--- depth_imbalance <= 3.00\n|   |   |   |--- class: 0\n"
             "|--- tte >  119.50\n|   |--- class: 0\n")
    refusals = []
    [g] = ex.taker_genomes(_mini_doc(text2), refusals=refusals)
    assert len(g.taker) == 1 and [r["path"][-1] for r in refusals] == ["depth_imbalance > 3"]
    assert g.evidence["refused_leaves"][0]["reason"].startswith("no genome equivalent")


def test_conservative_mapping_and_inventory_scale():
    text = ("|--- edge_up <= 0.12\n|   |--- inv <= 100.00\n|   |   |--- edge_up >  0.03\n"
            "|   |   |   |--- class: 1\n|   |   |--- edge_up <= 0.03\n|   |   |   |--- class: 0\n"
            "|   |--- inv >  100.00\n|   |   |--- class: 0\n|--- edge_up >  0.12\n|   |--- class: 0\n")
    [g] = ex.taker_genomes(_mini_doc(text))
    when = {(c.feature, c.op): c.value for c in g.taker[0].when}
    # gross->net: lower bound kept (3 c), upper bound lowered by the max fee (12 - 3 = 9 c)
    assert ex.FEE_BOUND_C == 3.0
    assert when[("edge_buy_up_c", ">")] == 3.0 and when[("edge_buy_up_c", "<=")] == 9.0
    # wallet 50 shares/order -> DOPPEL 5 contracts: inventory threshold 100 -> 10
    assert g.taker[0].size == 5.0 and when[("inventory_up", "<=")] == 10.0
    # every state the genome fires in is inside the dossier leaf (net edge + fee = gross edge)
    for net in np.linspace(-20, 20, 81):
        for fee in (0.0, 1.0, 2.375):
            x = {"tau_s": 250, "tte_s": 50, "edge_buy_up_c": net, "inventory_up": 0.0}
            if g.taker_actions(x):
                assert 3.0 < net + fee <= 12.0
    # no shares-per-order -> an inv split cannot be scaled: leaf refused, not guessed
    d = _mini_doc(text)
    d["orders"]["shares_per_order"] = None
    refusals = []
    assert ex.taker_genomes(d, refusals=refusals) == []
    assert any("shares per order" in r["reason"] for r in refusals)


def test_taker_gates_refuse():
    text = "|--- edge_up >  0.05\n|   |--- class: 1\n|--- edge_up <= 0.05\n|   |--- class: 0\n"
    for over, why in [({"auc_tree": 0.6}, "auc_tree"), ({"test_pos": 5}, "support")]:
        d = _mini_doc(text)
        d["policy"]["took_any"].update(over)
        refusals = []
        assert ex.taker_genomes(d, refusals=refusals) == [] and why in refusals[0]["reason"]
    d = _mini_doc(text, side={"train_up": 300, "train_down": 300, "test_up": 100, "test_down": 100,
                              "auc_tree": 0.55, "rules": "|--- edge_dn <= 0.00\n|   |--- class: 1\n"
                                                         "|--- edge_dn >  0.00\n|   |--- class: 0\n"})
    refusals = []
    assert ex.taker_genomes(d, refusals=refusals) == [] and "side not explained" in refusals[0]["reason"]
    clock_only = "|--- tte <= 60.00\n|   |--- class: 1\n|--- tte >  60.00\n|   |--- class: 0\n"
    refusals = []
    assert ex.taker_genomes(_mini_doc(clock_only), refusals=refusals) == []
    assert "clock-only" in refusals[0]["reason"]
    late = "|--- tte <= 5.00\n|   |--- edge_up >  0.05\n|   |   |--- class: 1\n|   |--- edge_up <= 0.05\n" \
           "|   |   |--- class: 0\n|--- tte >  5.00\n|   |--- class: 0\n"
    refusals = []   # universe tte_min_s = p10 = 10 s: a leaf firing only inside 5 s can never trade
    assert ex.taker_genomes(_mini_doc(late), refusals=refusals) == []
    assert "does not intersect the universe" in refusals[0]["reason"]


# ---------------------------------------------------------------- synthetic taker through the dossier
def _taker_world(tmp_path, two_sided=False, n_win=60, seed=11, p_take=0.5):
    """BTC 5m windows; the wallet takes Up when fair - ask > 0.05 and tte < 120 (and, two-sided,
    Down when (1 - fair) - ask_down > 0.05 and tte < 120)."""
    rng = np.random.default_rng(seed)
    first = T0 + 1200
    raw, brti = _brti(rng, n_win, first)
    recs, rows, idx, k = [], [], BookIndex(), 0
    for w in range(n_win):
        start = first + 300 * w
        end = start + 300
        slug = f"btc-updown-5m-{start}"
        up_pay = 1.0 if raw[end - 1] >= raw[start - 1] else 0.0
        recs += [_tok(f"U{w}", slug, 0, up_pay, cond=f"m{w}"), _tok(f"D{w}", slug, 1, 1 - up_pay, cond=f"m{w}")]
        for s in range(start - 1, end):
            fu = brti.fair(start, end, s)
            if fu is None:
                continue
            ask = float(np.clip(round(fu + rng.uniform(-0.10, 0.10), 2), 0.02, 0.99))
            bid = round(ask - 0.02, 2)
            tw = s * NS + 500_000_000          # seen by the panel row for s + 1
            idx.add_quote(f"U{w}", tw, bid, ask)
            idx.add_quote(f"D{w}", tw, round(1 - ask, 2), round(1 - bid, 2))
            nxt = s + 1
            if nxt >= end or end - nxt >= 120:
                continue
            if fu - ask > 0.05 and rng.random() < p_take:
                rows.append(_row(k, f"U{w}", "BUY", ask, 5, nxt + BLOCK_SLACK_S, "taker", f"o{k}"))
                k += 1
            elif two_sided and (1 - fu) - (1 - bid) > 0.05 and rng.random() < p_take:
                rows.append(_row(k, f"D{w}", "BUY", round(1 - bid, 2), 5, nxt + BLOCK_SLACK_S, "taker", f"o{k}"))
                k += 1
    idx.finalize()
    return _tmap(tmp_path, recs), rows, idx, brti


def test_synthetic_taker_rule_is_recovered(tmp_path):
    tm, rows, idx, brti = _taker_world(tmp_path)
    doc = _as_written(dz.dossier(WALLET, rows, tm, idx, brti)[0])
    refusals = []
    gs = ex.taker_genomes(doc, mechanism="LATE_TAKER", refusals=refusals)
    assert refusals == [] and len(gs) == 1
    g = gs[0]
    assert validate(g) == [] and g.mode == "taker" and g.status == "candidate"
    assert g.universe.venue == "pmus" and g.universe.assets == ("btc",) and g.universe.horizons == ("5m",)
    [r] = g.taker
    assert r.side == "up" and r.size <= ex.TAKER_SIZE_MAX
    when = {(c.feature, c.op): c.value for c in r.when}
    assert set(when) == {("edge_buy_up_c", ">"), ("tte_s", "<=")}, r.when
    assert abs(when[("edge_buy_up_c", ">")] - 5.0) <= 1.0     # 2 dp tree threshold = 1 c
    assert abs(when[("tte_s", "<=")] - 119.5) <= 1.0
    # universe from the wallet's entry tte p10/p90
    q = doc["mechanism"]["tte_s_taker"]
    assert g.universe.tte_min_s == math.floor(q["p10"]) and g.universe.tau_min_s == 300 - math.ceil(q["p90"])
    # executes: fires inside the rule, not outside it
    x = {"tau_s": 240, "tte_s": 60, "edge_buy_up_c": 8.0}
    assert [a.side for a in g.taker_actions(x)] == ["up"]
    assert g.taker_actions({**x, "edge_buy_up_c": 3.0}) == []
    assert g.taker_actions({**x, "tau_s": 150, "tte_s": 150}) == []
    # evidence + identity discipline
    ev = g.evidence
    assert ev["policy"]["took_any"]["auc_tree"] >= ex.MIN_AUC_TREE and ev["side_mode"] == "one_sided:up"
    assert ev["rules"][0]["dossier_path"] and ev["coverage"]["btc_fills"] == len(rows)
    assert g.source.kind == "clone" and g.source.wallets == (WALLET,) and g.source.mechanism == "LATE_TAKER"
    assert g.source.dossier_sha256 == (ex.dossier_sha256(doc),)
    assert "TAKE UP" in g.notes and g.id[:12] in g.notes
    again = ex.taker_genomes(orjson.loads(orjson.dumps(doc)), mechanism="LATE_TAKER")[0]
    assert again.id == g.id and from_dict(orjson.loads(orjson.dumps(to_dict(g)))).id == g.id


def test_two_sided_taker_with_disagreeing_trees_is_refused_not_invented(tmp_path):
    tm, rows, idx, brti = _taker_world(tmp_path, two_sided=True)
    doc = _as_written(dz.dossier(WALLET, rows, tm, idx, brti)[0])
    assert doc["policy"]["side_given_took"]["auc_tree"] >= ex.MIN_AUC_TREE
    refusals = []
    gs = ex.taker_genomes(doc, refusals=refusals)
    # Took tree splits on edge_up, side tree on edge_dn: conjoining them would emit rules for
    # regions neither tree was fit on (e.g. TAKE UP with both edges negative). Every surviving rule
    # must take the side the wallet actually took there; ambiguous leaves are refused.
    for g in gs:
        for r in g.taker:
            when = {(c.feature, c.op): c.value for c in r.when}
            if r.side == "up":
                assert when.get(("edge_buy_up_c", ">"), -1) >= 0
            else:
                assert when.get(("edge_buy_down_c", ">"), -1) >= 0 or when.get(("edge_buy_up_c", "<="), 1) < 0
    assert gs or any("side ambiguous" in r["reason"] for r in refusals)


# ---------------------------------------------------------------- synthetic maker through the dossier
H_C, K_C = 2.0, 0.05     # half-spread (c) and skew (c per share of Up inventory)


def _maker_world(tmp_path, n_win=40, seed=5, p_fill=0.12):
    """Complete-set maker quoting fair -/+ (h + k*inv) on both tokens (buys Up on the bid, buys Down
    = sells Up on the ask), 10 shares per order, leaning against inventory. The book's mid wanders
    +/- 4 c around fair, so distances to fair are tighter than to mid."""
    rng = np.random.default_rng(seed)
    first = T0 + 1200
    raw, brti = _brti(rng, n_win, first)
    recs, rows, idx, k = [], [], BookIndex(), 0
    dists = []
    for w in range(n_win):
        start = first + 300 * w
        end = start + 300
        slug = f"btc-updown-5m-{start}"
        up_pay = 1.0 if raw[end - 1] >= raw[start - 1] else 0.0
        recs += [_tok(f"U{w}", slug, 0, up_pay, cond=f"m{w}"), _tok(f"D{w}", slug, 1, 1 - up_pay, cond=f"m{w}")]
        inv = 0.0
        for s in range(start - 1, end):
            fu = brti.fair(start, end, s)
            if fu is None:
                continue
            c = float(np.clip(fu + rng.uniform(-0.04, 0.04), 0.03, 0.97))
            tw = s * NS + 500_000_000
            idx.add_quote(f"U{w}", tw, round(c - 0.01, 4), round(c + 0.01, 4))
            idx.add_quote(f"D{w}", tw, round(1 - c - 0.01, 4), round(1 - c + 0.01, 4))
            t_dec = s + 1                      # block_s fill at t_dec + slack; fair at t_dec - 1 = s
            if t_dec >= end - 5 or rng.random() >= p_fill:
                continue
            up = rng.random() < 0.5 - 0.4 * math.tanh(inv / 40)
            noise = rng.uniform(-0.3, 0.3)
            d = H_C + K_C * (inv if up else -inv) + noise           # = h + k * udir * inv (+ noise)
            price = round(fu - d / 100, 6) if up else round((1 - fu) - d / 100, 6)
            if not 0.01 <= price <= 0.99:
                continue
            rows.append(_row(k, f"U{w}" if up else f"D{w}", "BUY", price, 10.0, t_dec + BLOCK_SLACK_S,
                             "maker", f"o{k}"))
            dists.append(abs(d))
            k += 1
            inv += 10.0 if up else -10.0
    idx.finalize()
    return _tmap(tmp_path, recs), rows, idx, brti, dists


@pytest.fixture(scope="module")
def maker_dossier(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("maker")
    tm, rows, idx, brti, dists = _maker_world(tmp)
    doc, tables = dz.dossier(WALLET, rows, tm, idx, brti)
    return _as_written(doc), tables, dists


def test_maker_genome_half_spread_skew_sides(maker_dossier):
    doc, tables, dists = maker_dossier
    refusals = []
    [g] = ex.maker_genomes(doc, orders=tables["orders"], fills=tables["fills"], refusals=refusals,
                           mechanism="COMPLETE_SET_MAKER")
    assert refusals == [] and validate(g) == [] and g.mode == "maker"
    m = g.maker
    assert m is not None
    assert m.quote_ref == "fair" and m.sides == "both" and m.pause_when == ()
    assert m.half_spread_c == pytest.approx(float(np.median(dists)), abs=0.05)
    assert m.size == 10.0 and m.skew_c_per_contract == pytest.approx(K_C, abs=0.01)   # 10 shares -> 10 contracts
    reg = g.evidence["skew_regression"]
    assert reg["intercept_c"] == pytest.approx(H_C, abs=0.15) and reg["t"] > ex.MIN_SKEW_T
    f = g.evidence["fields"]
    assert f["skew_c_per_contract"].startswith("derived") and f["half_spread_c"].startswith("derived")
    assert f["requote_threshold_c"].startswith("default") and f["pause_when"].startswith("none")
    # every synthetic order fills once: lifetimes are 0 s, which says nothing about quote age
    assert m.max_quote_age_s == 30 and f["max_quote_age_s"].startswith("default")
    # quotes: long inventory shades both quotes down by skew * inventory
    x = {"tau_s": 100, "tte_s": 100, "fair_up": 0.5, "inventory_up": 0.0}
    b0, a0 = g.maker_quotes(x)
    b1, a1 = g.maker_quotes({**x, "inventory_up": 20.0})
    assert b0 is not None and a0 is not None and b1 is not None and a1 is not None
    assert b1 <= b0 and a1 <= a0
    assert ex.maker_genomes(doc, orders=tables["orders"], fills=tables["fills"],
                            mechanism="COMPLETE_SET_MAKER")[0].id == g.id


def test_maker_without_tables_uses_quantiles_and_default_skew(maker_dossier):
    doc, _, _ = maker_dossier
    [g] = ex.maker_genomes(doc)
    assert g.maker is not None and g.maker.skew_c_per_contract == 0.0
    assert g.evidence["fields"]["skew_c_per_contract"].startswith("default 0")
    assert g.maker.half_spread_c == pytest.approx(doc["orders"]["maker_fair_dist_c"]["p50"], abs=0.01)


def test_maker_reference_and_refusals(maker_dossier):
    doc, tables, _ = maker_dossier
    d = copy.deepcopy(doc)
    q = d["orders"]["maker_fair_dist_c"]
    q["p10"], q["p90"] = q["p10"] - 10, q["p90"] + 10           # fair distances now less stable
    # the mid wanders +/- 4 c, so mid distances change sign: |d| median needs the orders table
    refusals = []
    assert ex.maker_genomes(d, refusals=refusals) == [] and "changes sign" in refusals[0]["reason"]
    [g] = ex.maker_genomes(d, orders=tables["orders"])
    assert g.maker is not None and g.maker.quote_ref == "mid"
    mids = [abs(o["mid_dist_c"]) for o in tables["orders"] if o.get("mid_dist_c") is not None
            and math.isfinite(o["mid_dist_c"])]
    assert g.maker.half_spread_c == pytest.approx(float(np.median(mids)), abs=0.01)
    for path, value, why in [(("inventory", "paired_share_of_gross"), 0.1, "complete-set"),
                             (("mechanism", "maker_share"), 0.3, "maker_share"),
                             (("orders", "maker_orders"), 10, "maker orders")]:
        d = copy.deepcopy(doc)
        d[path[0]][path[1]] = value
        refusals = []
        assert ex.maker_genomes(d, refusals=refusals) == [] and why in refusals[0]["reason"]
    d = copy.deepcopy(doc)
    d["orders"]["maker_fair_dist_c"]["p10"] = -1.0             # sign change: |d| median not identified
    d["orders"]["maker_mid_dist_c"] = None
    refusals = []
    assert ex.maker_genomes(d, refusals=refusals) == [] and "changes sign" in refusals[0]["reason"]


def test_cli_extract_writes_candidates(maker_dossier, tmp_path, capsys):
    doc, tables, _ = maker_dossier
    lab = tmp_path / "dossiers"
    lab.mkdir()
    (lab / f"{WALLET}.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    for name in ("orders", "fills"):
        dz._write_table(lab / f"{WALLET}.{name}.parquet", tables[name])
    out = tmp_path / "out"
    assert cli(["extract", "--dossier", str(lab / f"{WALLET}.json"), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "QUOTE both around fair" in text and "REFUSED taker genome" in text
    [p] = list(out.glob("*.json"))
    g = from_dict(orjson.loads(p.read_bytes()))
    assert validate(g) == [] and p.name == f"{g.name}-{g.id[:12]}.json"
    assert g.evidence["fields"]["skew_c_per_contract"].startswith("derived")  # parquet tables were read
