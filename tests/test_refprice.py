from doppel.lab.refprice import settlement_value


def test_settlement_window_is_half_open_and_complete():
    s = {t: float(t) for t in range(1000, 1060)}
    assert settlement_value(s, 1060) == sum(range(1000, 1060)) / 60  # [T-60, T)
    s.pop(1030)
    assert settlement_value(s, 1060) is None
