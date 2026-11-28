import os
import random

import orjson
import pytest

from doppel.lab import cohort_v2 as cv
from doppel.lab import markout as mo
from doppel.lab.scan import rank_members, scan
from doppel.tape.segment import SegmentWriter

NS = 10**9
T0 = 1795449600                 # hour-aligned, in the past
NA = 84                         # Period-A 5m windows: T0 .. T0 + 7 h
FREEZE = T0 + NA * 300
NB = 12                         # Period-B windows (one more hour)


def W(tag, i):
    return "0x" + f"{tag}{i:02d}".encode().hex().rjust(40, "0")


SNIPERS = [W("sn", i) for i in range(5)]
DONORS = [W("do", i) for i in range(4)]
LUCKY = [W("lu", i) for i in range(2)]
NOISE = [W("no", i) for i in range(15)]
WHALES = [W("wh", i) for i in range(100)]


def _tok(k, side):
    return f"{k}{side}"


def _book(tok, mk, mid, tw):
    return orjson.dumps({"event_type": "book", "asset_id": tok, "market": mk, "timestamp": str(tw // 10**6),
                        "bids": [{"price": f"{mid - 0.01:.4f}", "size": "100"}],
                        "asks": [{"price": f"{mid + 0.01:.4f}", "size": "100"}]}).decode()


def _write_tape(root, windows, *, flip=False, seed=7):
    """windows: iterable of window indexes. ``flip`` (Period B) turns noise wallets into perfect snipers."""
    rng = random.Random(seed)
    rtds, clob = [], []
    outcomes = {}
    n = 0
    for k in windows:
        s = T0 + 300 * k
        slug = f"btc-updown-5m-{s}"
        up, dn, mk = _tok(k, "u"), _tok(k, "d"), f"0xm{k}"
        J = rng.choice((-1, 1)) * rng.uniform(0.05, 0.15)
        m0, m1 = 0.5, 0.5 + J
        for tok, a, b in ((up, m0, m1), (dn, 1 - m0, 1 - m1)):
            clob += [((s + 1) * NS, _book(tok, mk, a, (s + 1) * NS)), ((s + 100) * NS, _book(tok, mk, b, (s + 100) * NS)),
                     ((s + 299) * NS, _book(tok, mk, b, (s + 299) * NS))]
        outcomes[up], outcomes[dn] = {"outcome": int(J > 0)}, {"outcome": int(J < 0)}
        riser, faller = (up, 0) if J > 0 else (dn, 1), (dn, 1) if J > 0 else (up, 0)
        mid0 = {up: m0, dn: 1 - m0}
        mid1 = {up: m1, dn: 1 - m1}

        def fill(wallet, tok_idx, side, px, size, t, fee=0.0):
            nonlocal n
            n += 1
            rtds.append((int(t * NS) + 50_000_000, orjson.dumps({"payload": {
                "transactionHash": f"0x{n:x}", "proxyWallet": wallet, "eventSlug": slug, "asset": tok_idx[0],
                "side": side, "outcomeIndex": tok_idx[1], "price": round(px, 4), "size": size, "fee": fee,
                "timestamp": int(t), "name": None}}).decode()))

        for w in SNIPERS:
            fill(w, riser, "BUY", mid0[riser[0]] + 0.01 + rng.uniform(-0.004, 0.004), 200, s + 97, 0.01)
        for w in DONORS:
            fill(w, faller, "BUY", mid0[faller[0]] + 0.015 + rng.uniform(-0.004, 0.004), 200, s + 97, 0.01)
        for i, w in enumerate(NOISE + LUCKY):
            if flip and w in NOISE:
                fill(w, riser, "BUY", mid0[riser[0]], 200, s + 97)
                continue
            if w in LUCKY and k == 10 + 7 * LUCKY.index(w):      # one huge, lucky window
                fill(w, riser, "BUY", mid0[riser[0]] + 0.01, 20000, s + 97)
                continue
            tok = rng.choice((riser, faller))
            t = s + rng.randint(120, 230)
            if w == NOISE[0] and k == NA - 1:
                t = s + 250                                       # +60 s lands after the freeze
            fill(w, tok, rng.choice(("BUY", "SELL")), mid1[tok[0]] + rng.uniform(-0.01, 0.01), 200, t)
        if k < 5:
            for w in WHALES:
                fill(w, riser, "BUY", 0.5, 100000, s + 150)
    if flip:
        # Period-B book move of the last Period-A window, just after the freeze.
        clob.append(((FREEZE + 5) * NS, _book(_tok(NA - 1, "u"), f"0xm{NA - 1}", 0.99, (FREEZE + 5) * NS)))
        clob.append(((FREEZE + 5) * NS, _book(_tok(NA - 1, "d"), f"0xm{NA - 1}", 0.01, (FREEZE + 5) * NS)))
    for stream, recs in (("rtds_a", rtds), ("rtds_b", rtds), ("clob_btc_5m", clob)):
        recs = sorted(recs, key=lambda r: r[0])
        if stream == "rtds_b":
            recs = [r for i, r in enumerate(recs) if i % 5]   # socket B drops some; the union recovers them
        w = SegmentWriter(root, stream)
        for tw, raw in recs:
            if not flip or tw >= FREEZE * NS:
                w.append(raw, tw=tw)
        w.close()
    return outcomes


@pytest.fixture()
def tape(tmp_path):
    root = tmp_path / "tape"
    outc = _write_tape(root, range(NA))
    tokens = tmp_path / "tokens.json"
    tokens.write_bytes(orjson.dumps(outc))
    return root, tokens


def _build(root, tokens, **kw):
    return cv.build(root, tokens, FREEZE, n_sniper=5, n_donor=4, n_ctrl=5, **kw)


def test_slices_recover_planted_wallets_and_lucky_is_not_picked(tape):
    root, tokens = tape
    doc, stats = _build(root, tokens)
    assert sorted(doc["sniper"]) == sorted(SNIPERS)
    assert sorted(doc["donor"]) == sorted(DONORS)
    assert not set(doc["controls"]) & set(SNIPERS + DONORS) and len(doc["controls"]) == 5
    assert set(doc["controls"]) <= set(NOISE + LUCKY)
    ws = stats["wallets"]
    assert set(ws) == set(SNIPERS + DONORS + LUCKY + NOISE)          # whales have < 50 windows
    assert all(ws[w]["rank"] > 100 for w in ws)
    for w in LUCKY:
        m = ws[w]["markout"]["10s"]
        assert m["mean_c"] > 2                          # the raw mean is driven by one window...
        assert abs(m["sniper"]["mean_w_c"]) < 1         # ...that winsorizing removes
        assert m["sniper"]["t"] < min(ws[s]["markout"]["10s"]["sniper"]["t"] for s in SNIPERS) / 5
    sn = ws[SNIPERS[0]]["markout"]
    assert sn["2s"]["mean_c"] < 0 < sn["10s"]["mean_c"]                # gross: impact arrives after +2 s
    assert ws[DONORS[0]]["markout"]["10s"]["donor"]["t"] < 0
    assert ws[SNIPERS[0]]["brier"] is None
    assert doc["shortfall"] == {"sniper": 0, "donor": 0, "controls": 0}
    cv.verify(doc, stats)


def test_floors_publish_short_slices_and_null_counts(tape):
    root, tokens = tape
    doc, stats = cv.build(root, tokens, FREEZE, n_sniper=20, n_donor=20, n_ctrl=5)
    ws = stats["wallets"]
    t10 = {a: ws[a]["markout"]["10s"]["sniper"]["t"] for a in ws}
    passing = {a for a in ws if 101 <= ws[a]["rank"] <= 2500 and t10[a] is not None and t10[a] > cv.T_FLOOR}
    assert set(doc["sniper"]) == passing and set(SNIPERS) <= passing      # every pass, nothing below the floor
    assert len(doc["sniper"]) < 20 and doc["shortfall"]["sniper"] == 20 - len(doc["sniper"])
    for a in doc["donor"]:
        m = ws[a]["markout"]
        assert m["2s"]["donor"]["t"] < -cv.T_FLOOR and m["10s"]["donor"]["t"] < -cv.T_FLOOR
    assert set(DONORS) <= set(doc["donor"]) and doc["shortfall"]["donor"] == 20 - len(doc["donor"])
    nl = doc["null"]
    assert nl["sniper_floor"]["observed"] == len(passing) and nl["permutations"] == cv.NULL_PERMS
    # Zero-skill wallets pass t > 1 about 16% of the time; planted skill is far above that.
    assert 0 < nl["sniper_floor"]["expected_null"] < nl["sniper_floor"]["pool"] * 0.4
    assert nl["donor_floor"]["expected_null"] < nl["donor_floor"]["observed"]
    assert doc["rule"]["t_floor"] == 1.0 and "t_floor" in doc["rule"]["sniper"] and "t_floor" in doc["rule"]["donor"]
    # A floor nobody passes gives an empty, short slice rather than weaker wallets.
    stats2 = {a: {**s, "markout": {**s["markout"], "10s": {**s["markout"]["10s"],
                                                           "sniper": {"mean_w_c": 0.0, "t": 0.5}}}}
              for a, s in ws.items()}
    sel = cv.select(stats2, 5, 0, 0)
    assert sel["sniper"] == [] and sel["shortfall"]["sniper"] == 5


def test_volume_matches_v1_scan(tape):
    root, _ = tape
    table = cv.volume_table(root, FREEZE)
    ref = scan(root)["wallets"]
    assert set(table) == set(ref)
    for a, v in table.items():
        assert round(v["usd"], 2) == ref[a]["usd"] and len(v["win"]) == ref[a]["n_windows"]
    n, cands, _ = rank_members({"wallets": ref}, n_cand=100, n_ctrl=5, ctrl_band=(101, 120))
    ranks = cv.volume_ranks(table)
    assert sorted(cands, key=ranks.__getitem__) == [a for a, r in sorted(ranks.items(), key=lambda x: x[1])][:100]
    assert set(cands) == set(WHALES)


def test_stream_marks_equal_markout_intl_paths(tape):
    root, tokens = tape
    outc = cv.load_outcomes(tokens)
    fills = {}
    for f in cv.period_a_fills(root, FREEZE):
        if f.wallet == SNIPERS[1] and f.start < T0 + 3000:
            mf = mo.Fill(f.token, f.tw, "buy" if f.side == "BUY" else "sell", f.price, f.size, f.fee, None, "receive")
            fills.setdefault(f.token, []).append((mf, f.wallet, f.slug, f.start, f.end))
    got = {(r[0].key, r[0].t_ns): m for r, m in cv.stream_marks(root, "clob_btc_5m", T0, FREEZE, fills, outc)}
    meta = {t: {"outcome": outc[t], "end": rows[0][4]} for t, rows in fills.items()}
    paths, _ = mo.intl_paths(root, "clob_btc_5m", T0, T0 + 3600, set(fills), meta)
    assert len(got) == 10
    for tok, rows in fills.items():
        for r in rows:
            ref = mo.marks(r[0], paths[tok], cv.HORIZONS, fair=False)["mid"]
            assert got[(tok, r[0].t_ns)] == {k: ref[k] for k in cv.HK}


def test_period_b_is_never_read(tape, tmp_path):
    root, tokens = tape
    doc_a, stats_a = _build(root, tokens)
    last = stats_a["wallets"][NOISE[0]]
    outb = _write_tape(root, range(NA, NA + NB), flip=True, seed=99)
    tokens2 = tmp_path / "tokens2.json"
    tokens2.write_bytes(orjson.dumps({**orjson.loads(tokens.read_bytes()), **outb,
                                      # a post-freeze outcome for the last Period-A window
                                      "83u": {"outcome": 1}, "83d": {"outcome": 0}}))
    post = [p for p in root.glob("*/*/*.dtape.zst") if _hour(p) >= FREEZE]
    assert post
    for p in post:
        os.chmod(p, 0)                                   # opening any Period-B segment would fail
    try:
        doc_b, stats_b = _build(root, tokens2)
    finally:
        for p in post:
            os.chmod(p, 0o644)
    assert {k: doc_b[k] for k in ("sniper", "donor", "controls", "eligible", "period_a")} == \
           {k: doc_a[k] for k in ("sniper", "donor", "controls", "eligible", "period_a")}
    assert stats_b == stats_a
    assert doc_b["inputs"]["rtds"] == doc_a["inputs"]["rtds"] and doc_b["inputs"]["clob"] == doc_a["inputs"]["clob"]
    assert last["markout"]["60s"]["windows"] == last["markout"]["10s"]["windows"] - 1   # the +60 s mark past the freeze


def _hour(p):
    import calendar
    import time
    return calendar.timegm(time.strptime(p.parent.name + p.name[:2], "%Y%m%d%H"))


def test_deterministic_cli_and_v1_untouched(tape, tmp_path):
    root, tokens = tape
    v1 = {"candidates": [SNIPERS[0], WHALES[0]], "controls": [NOISE[0]], "sha256": "x" * 64}
    outs = []
    for name in ("o1", "o2"):
        out = tmp_path / name
        out.mkdir()
        (out / "cohort-v1.json").write_bytes(orjson.dumps(v1))
        before = (out / "cohort-v1.json").read_bytes()
        v1_ranks = rank_members(scan(root), n_cand=100, n_ctrl=5, ctrl_band=(101, 120))
        assert cv.main(["--data", str(root), "--tokens", str(tokens), "--out", str(out),
                        "--freeze-at", "2026-11-23T23:00:00Z"]) == 0
        assert (out / "cohort-v1.json").read_bytes() == before
        assert rank_members(scan(root), n_cand=100, n_ctrl=5, ctrl_band=(101, 120)) == v1_ranks
        outs.append(((out / "cohort-v2.json").read_bytes(), (out / "cohort-v2-stats.json").read_bytes()))
        assert cv.main(["--data", str(root), "--tokens", str(tokens), "--out", str(out),
                        "--freeze-at", "2026-11-23T23:00:00Z"]) == 1          # write-once
    assert outs[0] == outs[1]
    doc = orjson.loads(outs[0][0])
    assert doc["frozen_tw"] == FREEZE * NS
    cv.verify(doc, orjson.loads(outs[0][1]))
    # Default sizes on a 26-wallet pool: slices are published short, ranked best first.
    assert set(doc["sniper"][:5]) == set(SNIPERS) and set(DONORS) <= set(doc["donor"])
    assert doc["shortfall"]["sniper"] == 100 - len(doc["sniper"]) > 0
    assert doc["shortfall"]["donor"] == 50 - len(doc["donor"]) > 0
    assert len(doc["controls"]) == 26 - len(doc["sniper"]) - len(doc["donor"])
    assert doc["v1_overlap"]["sniper"]["v1_candidates"] == [SNIPERS[0]]
    assert "cohort_v1" in doc["inputs"] and doc["inputs"]["tokens"]
    with pytest.raises(ValueError, match="hour boundary"):
        cv.build(root, tokens, FREEZE + 1)


def test_t_stat_and_winsorizing():
    cells = [((i, ""), 10.0 + (i % 3), 10.0) for i in range(30)]
    a = cv.winsorized_t(cells, "top")
    assert a["t"] is not None and a["t"] > 10
    lucky = cells[:-1] + [((99, ""), 1e6, 10.0)]
    b = cv.winsorized_t(lucky, "top")
    assert b["mean_c"] > 1000 and b["mean_w_c"] < 1.3
    assert cv.winsorized_t(cells[:10], "top")["t"] is None            # too few windows


def test_stream_marks_parity_with_resets_deltas_and_prints(tmp_path):
    root = tmp_path / "tape"
    s, tok, mate, mk = T0, "tu", "td", "0xm"

    def pc(tok_, px, size, side):
        return orjson.dumps({"event_type": "price_change", "market": mk, "timestamp": "1",
                             "price_changes": [{"asset_id": tok_, "price": px, "size": size, "side": side}]}).decode()
    w = SegmentWriter(root, "clob_btc_5m")
    w.append(_book(mate, mk, 0.5, (s + 1) * NS), tw=(s + 1) * NS)
    w.append(_book(tok, mk, 0.5, (s + 2) * NS), tw=(s + 2) * NS)
    w.append(pc(tok, "0.53", "10", "SELL"), tw=(s + 50) * NS)        # a level behind the ask: mid unchanged
    w.append(pc(tok, "0.51", "0", "SELL"), tw=(s + 52) * NS)         # best ask removed: ask 0.53
    w.append("", tw=(s + 60) * NS, meta={"ctl": "disconnect"})       # book unknown until the next snapshot
    w.append(_book(tok, mk, 0.6, (s + 70) * NS), tw=(s + 70) * NS)
    w.append(orjson.dumps({"event_type": "last_trade_price", "asset_id": tok, "market": mk, "price": "0.61",
                           "size": "5", "side": "BUY", "timestamp": "1", "transaction_hash": "0xab"}).decode(),
             tw=(s + 80) * NS)
    w.append(pc(tok, "0.65", "0", "SELL"), tw=(s + 120) * NS)        # unknown level: no change
    w.append(_book(tok, mk, 0.7, (s + 200) * NS), tw=(s + 200) * NS)
    w.close()
    fills = {tok: [(mo.Fill(tok, (s + t) * NS, side, 0.5, 2.0, 0.0, None, "receive"), "w", "slug", s, s + 300)
                   for t, side in ((40, "buy"), (49, "sell"), (55, "buy"), (65, "buy"), (75, "sell"), (130, "buy"))]}
    got = [(r[0].t_ns, m) for r, m in cv.stream_marks(root, "clob_btc_5m", T0, T0 + 3600, fills, {tok: 1})]
    paths, _ = mo.intl_paths(root, "clob_btc_5m", T0, T0 + 3600, {tok}, {tok: {"outcome": 1, "end": s + 300}})
    ref = [(f.t_ns, {k: v for k, v in mo.marks(f, paths[tok], cv.HORIZONS, fair=False)["mid"].items() if k in cv.HK})
           for f, *_ in fills[tok]]
    assert sorted(got, key=lambda x: x[0]) == sorted(ref, key=lambda x: x[0])
    by_t = dict(got)
    assert by_t[(s + 55) * NS]["2s"] is not None and by_t[(s + 55) * NS]["10s"] is None   # +10 s is in the gap
    assert by_t[(s + 49) * NS]["10s"] == pytest.approx(50.0 - 51.0)     # sold at 50; 0.49 / 0.53 -> mid 51
