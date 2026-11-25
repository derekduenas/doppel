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
