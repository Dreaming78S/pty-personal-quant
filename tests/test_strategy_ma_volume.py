import pandas as pd

from quant.strategies import get_strategy


def make_bars(closes, vols):
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(len(closes))],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_ma_volume_signal_on_cross_with_volume():
    closes = [10, 10, 11, 9, 10, 11, 11]
    vols = [100, 100, 100, 100, 100, 500, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert list(signals) == [False, False, False, False, False, True, False]


def test_ma_volume_requires_volume_spike():
    closes = [10, 10, 11, 9, 10, 11, 11]
    vols = [100, 100, 100, 100, 100, 150, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_ma_volume_strict_cross_needs_yesterday_below():
    # 昨日 ma2 == ma3（相等不算"昨日小于"），今日上穿不命中
    closes = [10, 10, 10, 9, 11, 11, 11]
    vols = [100, 100, 100, 100, 100, 500, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_ma_volume_defaults_and_warmup():
    s = get_strategy("ma_volume")
    assert s.p.ma_short == 5
    assert s.p.ma_long == 20
    assert s.p.vol_ma == 20
    assert s.p.vol_ratio == 1.5
    assert s.warmup_days == 21


def test_ma_volume_no_signal_when_short_warmup_rows():
    closes = [10, 11]
    s = get_strategy("ma_volume")
    signals = s.generate_signals(make_bars(closes, [100, 100]))
    assert not signals.any()
