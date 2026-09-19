import pandas as pd

from quant.strategies import get_strategy


def make_bars(raw_close, open_, close, low, vols):
    n = len(close)
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(n)],
        "open": open_,
        "high": [max(o, c) + 0.1 for o, c in zip(open_, close)],
        "low": low,
        "close": close,
        "raw_close": raw_close,
        "vol": vols,
        "amount": [1e5] * n,
    })


def test_limit_up_shakeout_true_on_pullback():
    raw_close = [10.0, 11.0, 10.4]
    close = [10.0, 11.0, 10.5]
    open_ = [10.0, 10.8, 11.0]
    low = [9.9, 10.9, 11.0]
    vols = [100, 100, 300]
    s = get_strategy("limit_up_shakeout")

    signals = s.generate_signals(make_bars(raw_close, open_, close, low, vols))

    assert list(signals) == [False, False, True]


def test_limit_up_shakeout_false_without_volume_spike():
    raw_close = [10.0, 11.0, 10.4]
    close = [10.0, 11.0, 10.5]
    open_ = [10.0, 10.8, 11.0]
    low = [9.9, 10.9, 11.0]
    vols = [100, 100, 150]
    s = get_strategy("limit_up_shakeout")
    assert not s.generate_signals(
        make_bars(raw_close, open_, close, low, vols)).any()


def test_limit_up_shakeout_false_when_support_breaks():
    raw_close = [10.0, 11.0, 10.4]
    close = [10.0, 11.0, 10.5]
    open_ = [10.0, 10.8, 11.0]
    low = [9.9, 10.9, 10.9]
    vols = [100, 100, 300]
    s = get_strategy("limit_up_shakeout")
    assert not s.generate_signals(
        make_bars(raw_close, open_, close, low, vols)).any()


def test_limit_up_shakeout_false_without_limit_up():
    raw_close = [10.0, 10.5, 10.0]
    close = [10.0, 10.5, 10.0]
    open_ = [9.8, 10.3, 10.4]
    low = [9.7, 10.2, 9.9]
    vols = [100, 100, 300]
    s = get_strategy("limit_up_shakeout")
    assert not s.generate_signals(
        make_bars(raw_close, open_, close, low, vols)).any()


def test_limit_up_shakeout_defaults_and_warmup():
    s = get_strategy("limit_up_shakeout")
    assert s.p.limit_pct == 0.095
    assert s.p.vol_ratio == 2.0
    assert s.warmup_days == 3
