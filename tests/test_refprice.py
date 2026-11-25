from doppel.lab.refprice import settlement_value


def test_settlement_window_is_half_open_and_complete():
    s = {t: float(t) for t in range(1000, 1060)}
    assert settlement_value(s, 1060) == sum(range(1000, 1060)) / 60  # [T-60, T)
    s.pop(1030)
    assert settlement_value(s, 1060) is None


def test_updown_open_close_uses_series_points():
    from doppel.lab.refprice import updown_open_close
    s = {99: 100.0, 100: 101.0, 399: 99.5}
    assert updown_open_close(s, 100, 400) == (100.0, 99.5)


def test_load_raw_merges_import_and_live(tmp_path):
    import orjson

    from doppel.lab.brti_import import brti_source_agreement, load_raw
    from doppel.tape.segment import SegmentWriter
    w = SegmentWriter(tmp_path, "ref_brti_raw")
    for s, v in ((100, "1.00"), (101, "2.00")):
        w.append(orjson.dumps({"t_ms": s * 1000, "v": v}).decode(), tw=s * 10**9)
    w.close()
    lv = SegmentWriter(tmp_path, "ref_brti_live")
    lv.append("{}", tw=102 * 10**9, meta={"new": [[101000, "2.00"], [102000, "3.00"]]})
    lv.close()
    assert load_raw(tmp_path) == {100: 1.0, 101: 2.0, 102: 3.0}
    assert brti_source_agreement(tmp_path) == {"overlap_s": 1, "max_abs_diff": 0.0, "mismatch_gt_0.005": 0}
