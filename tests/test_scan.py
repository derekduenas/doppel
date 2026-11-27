import orjson
import pytest
from concurrent.futures import ThreadPoolExecutor

from doppel.lab.scan import NS, cohort_digest, main, rank_members, scan, scan_digest, select_cohort, validate_cohort
from doppel.tape.segment import SegmentWriter


def _row(tx, wallet, slug, side, outcome, price, size, fee, ts):
    return orjson.dumps({"payload": {"transactionHash": tx, "proxyWallet": wallet, "eventSlug": slug,
                                     "asset": f"tok{outcome}", "side": side, "outcomeIndex": outcome,
                                     "price": price, "size": size, "fee": fee, "timestamp": ts,
                                     "name": wallet}}).decode()


def test_scan_dedupes_sockets_and_measures(tmp_path):
    slug = "btc-updown-5m-1795622400"  # ends 1795622700
    rows = [_row("0x1", "W", slug, "BUY", 0, 0.5, 10, 0.1, 1795622640),
            _row("0x2", "W", slug, "BUY", 1, 0.45, 10, 0.0, 1795622650),
            _row("0x3", "V", "nba-game", "BUY", 0, 0.5, 10, 0.1, 1795622650)]  # not a window
    for s in ("rtds_a", "rtds_b"):
        w = SegmentWriter(tmp_path, s)
        for r in rows:
            w.append(r, tw=1795622660 * 10**9)
        w.close()
    doc = scan(tmp_path)
    assert set(doc["wallets"]) == {"w"}
    x = doc["wallets"]["w"]
    assert x["fills"] == 2 and x["taker_fills"] == 1 and x["maker_fills"] == 1
    assert x["both_sides_windows"] == 1 and x["tte_taker_p50"] == 60 and x["tte_maker_p50"] == 50
    assert x["usd"] == 9.5


def test_cohort_is_deterministic_and_profit_blind():
    ws = {f"0x{i:040x}": {"usd": 10000 - i, "n_windows": 10} for i in range(400)}
    doc = {"span": [0, 1], "wallets": ws}
    a = rank_members(doc, n_cand=10, n_ctrl=5)
    for i, row in enumerate(ws.values()):
        row["profit"] = i
    assert a == rank_members(doc, n_cand=10, n_ctrl=5)
    count, cands, ctrls = a
    assert count == 400 and cands == [f"0x{i:040x}" for i in range(10)]
    assert len(ctrls) == 5 and not set(cands) & set(ctrls)
    with pytest.raises(ValueError, match="verified, closed"):
        select_cohort(doc, n_cand=10, n_ctrl=5)
    with pytest.raises(ValueError, match="disjoint"):
        rank_members(doc, n_cand=20, n_ctrl=3, ctrl_band=(10, 100))
    with pytest.raises(ValueError, match="insufficient"):
        rank_members(doc, n_cand=5, n_ctrl=100, ctrl_band=(7, 11))


@pytest.mark.parametrize(("n_cand", "n_ctrl"), [(100, 50), (8, 3), (3, 8), (4, 4), (1, 2)])
def test_interleaved_allocation_covers_adjacent_ranks_before_outcomes(n_cand, n_ctrl):
    total = n_cand + n_ctrl
    addresses = [f"0x{i:040x}" for i in range(total + 6)]
    # All activity is tied, so wallet address (not input order or reported profit) sets rank.
    wallets = {a: {"usd": 100, "n_windows": 5, "profit": i} for i, a in enumerate(reversed(addresses))}
    args = dict(n_cand=n_cand, n_ctrl=n_ctrl, design="interleaved", seed=17)
    eligible, candidates, controls = rank_members({"wallets": wallets}, **args)
    assert eligible == len(wallets)
    assert len(candidates) == n_cand and len(controls) == n_ctrl
    assert not set(candidates) & set(controls)
    assert set(candidates + controls) == set(addresses[:total])
    assert candidates == sorted(candidates) and controls == sorted(controls)
    groups = min(n_cand, n_ctrl)
    minority = set(controls if n_ctrl <= n_cand else candidates)
    for group in range(groups):
        block = addresses[group * total // groups:(group + 1) * total // groups]
        assert len(minority.intersection(block)) == 1
    reversed_wallets = {a: dict(wallets[a], profit=-wallets[a]["profit"]) for a in addresses}
    assert rank_members({"wallets": reversed_wallets}, **args) == (eligible, candidates, controls)


def test_interleaved_refuses_unfilled_or_ambiguous_allocation():
    ws = {f"0x{i:040x}": {"n_windows": 5, "usd": 100} for i in range(3)}
    doc = {"wallets": ws}
    with pytest.raises(ValueError, match="insufficient"):
        rank_members(doc, 2, 2, design="interleaved")
    with pytest.raises(ValueError, match="no lower control band"):
        rank_members(doc, 2, 1, ctrl_band=(2, 3), design="interleaved")
    with pytest.raises(ValueError, match="positive integers"):
        rank_members(doc, 0, 3, design="interleaved")


H = 1795622400
WALLET = "0x" + "1" * 40


def _append(writer, tx, second, received, wallet=WALLET, start=H, size=10):
    raw = _row(tx, wallet, f"btc-updown-5m-{start}", "BUY", 0, 0.5, size, 0.2, second)
    writer.append(raw, tw=received)


def test_freeze_observes_both_clocks_and_socket_multiplicity(tmp_path):
    cutoff = H + 45
    for s, stream in enumerate(("rtds_a", "rtds_b")):
        w = SegmentWriter(tmp_path, stream)
        # Socket A loses the second occurrence at the cutoff. B observed both in time.
        _append(w, "same", H + 20, (H + 30) * NS)
        _append(w, "same", H + 20, cutoff * NS if s == 0 else (cutoff * NS - 1))
        _append(w, "late", H + 44, cutoff * NS)
        _append(w, "early_delivery", cutoff, (cutoff - 1) * NS)
        _append(w, "before", H + 1, (H + 30) * NS)
        w.close()
    result = scan(tmp_path, H + 5, until=cutoff, sealed=True)
    assert result["as_of_tw"] == cutoff * NS
    assert result["wallets"][WALLET]["fills"] == 2
    assert result["wallets"][WALLET]["usd"] == 10.0
    assert len(result["segments"]) == 2
    with pytest.raises(ValueError, match="missing complete RTDS segment"):
        scan(tmp_path, H, until=H + 7200, sealed=True)
    seg = next((tmp_path / "rtds_b").glob("*/*.dtape.zst"))
    data = bytearray(seg.read_bytes())
    data[-1] ^= 1
    seg.write_bytes(data)
    with pytest.raises(ValueError, match="differs from seal"):
        scan(tmp_path, H, until=cutoff, sealed=True)


def test_cli_preserves_immutable_freeze_including_racing_writers(tmp_path):
    from doppel.lab.selection_plan import build_plan, digest, freeze_plan, validate_plan
    from hashlib import sha256

    for stream in ("rtds_a", "rtds_b"):
        w = SegmentWriter(tmp_path, stream)
        for i in range(150):
            for j in range(5):
                start = H + 300 * j
                _append(w, f"{i}-{j}", start + 1, (start + 2) * NS, f"0x{i:040x}", start, i + 1)
        w.close()
    cohort_path = tmp_path / "cohort.json"
    src = tmp_path / "scan.json"
    args = ["--data", str(tmp_path), "--out", str(src), "--freeze-cohort", str(cohort_path)]
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(main, [args, args])) == [0, 1]
    original = cohort_path.read_bytes()
    scan_bytes = src.read_bytes()
    c = orjson.loads(original)
    validate_cohort(c)
    assert c["scan_sha256"] == scan_digest(orjson.loads(scan_bytes))
    assert len(c["candidates"]) == 100 and len(c["controls"]) == 50
    assert main(args) == 1
    alias = tmp_path / "alias"
    alias.symlink_to(cohort_path)
    with pytest.raises(SystemExit):
        main(["--data", str(tmp_path), "--out", str(alias), "--freeze-cohort", str(cohort_path)])
    assert cohort_path.read_bytes() == original and src.read_bytes() == scan_bytes
    with pytest.raises(ValueError, match="checksum"):
        validate_cohort({**c, "frozen_tw": c["frozen_tw"] - 100})
    legacy = {key: value for key, value in c.items() if key not in ("schema_version", "as_of_tw", "scan_sha256")}
    legacy["sha256"] = cohort_digest(legacy)
    validate_cohort(legacy)
    assert legacy["sha256"] == cohort_digest({**legacy, "frozen_tw": legacy["frozen_tw"] - 100})
    with pytest.raises(ValueError, match="checksum"):
        validate_cohort({**legacy, "controls": list(reversed(legacy["controls"]))})
    # The separate prospective policy goes through the same public freeze and replay path.
    inter_scan, inter_cohort = tmp_path / "new-scan.json", tmp_path / "new-cohort.json"
    assert main(["--data", str(tmp_path), "--out", str(inter_scan), "--freeze-cohort", str(inter_cohort),
                 "--cohort-design", "interleaved"]) == 0
    alternate = orjson.loads(inter_cohort.read_bytes())
    assert alternate["rule"]["design"] == "interleaved" and "control_band_ranks" not in alternate["rule"]
    plan = freeze_plan(inter_scan, inter_cohort, tmp_path / "new-plan.json",
                       expected_scan_sha256=sha256(inter_scan.read_bytes()).hexdigest(),
                       expected_cohort_sha256=sha256(inter_cohort.read_bytes()).hexdigest())
    assert plan["controls"] == alternate["controls"] and plan["candidates"] == alternate["candidates"]
    for i in range(150):
        assert plan["intake_ranking"]["by_wallet"][f"0x{i:040x}"]["rank"] == 150 - i
    altered = orjson.loads(orjson.dumps(plan))
    altered["intake_ranking"]["by_wallet"][plan["candidates"][0]]["rank"] = 149
    altered["sha256"] = digest({k: v for k, v in altered.items() if k != "sha256"})
    with pytest.raises(ValueError, match="discovery rank"):
        validate_plan(altered)

    # Discovery rank remains available when the wallet's pre-freeze scan lacks a matching feature.
    incomplete_scan = orjson.loads(inter_scan.read_bytes())
    first_candidate = plan["candidates"][0]
    del incomplete_scan["wallets"][first_candidate]["px_p50"]
    new_cohort = select_cohort(incomplete_scan, design="interleaved")
    with_missing = build_plan(incomplete_scan, new_cohort, scan_sha256="a" * 64, cohort_sha256="b" * 64)
    validate_plan(with_missing)
    assert with_missing["features"][first_candidate] is None
    assert with_missing["intake_ranking"] == plan["intake_ranking"]
    assert cohort_path.read_bytes() == original
