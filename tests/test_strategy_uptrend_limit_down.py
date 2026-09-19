import pandas as pd

from quant.strategies import get_strategy

PARAMS = dict(ma_short=3, ma_long=5, limit_pct=0.095, vol_ma=3, vol_ratio=2.0)


def make_bars(closes, raw_closes, vols):
    n = len(closes)
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(n)],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "raw_close": raw_closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_uptrend_limit_down_true_on_oversold_dump():
    closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    raw_closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    vols = [100, 100, 100, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)

    signals = s.generate_signals(make_bars(closes, raw_closes, vols))

    assert list(signals) == [False] * 5 + [True]


def test_uptrend_limit_down_false_without_volume_spike():
    closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    vols = [100, 100, 100, 100, 100, 200]
    s = get_strategy("uptrend_limit_down", **PARAMS)
    assert not s.generate_signals(
        make_bars(closes, closes, vols)).any()


def test_uptrend_limit_down_false_without_uptrend():
    closes = [11.0, 10.8, 10.6, 10.4, 10.2, 9.0]
    vols = [100, 100, 100, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)
    assert not s.generate_signals(
        make_bars(closes, closes, vols)).any()


def test_uptrend_limit_down_limit_check_uses_raw_close():
    # 复权收盘 10.6 不满足 -9.5%，原始收盘 9.9 满足；必须用 raw_close 判定。
    closes = [10.0, 10.2, 10.4, 10.6, 11.0, 10.6]
    raw_closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    vols = [100, 100, 100, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)
    signals = s.generate_signals(make_bars(closes, raw_closes, vols))
    assert list(signals) == [False] * 5 + [True]


def test_uptrend_limit_down_volume_mean_includes_today():
    # 末根 500 > 2×mean(100,100,500)≈466.7 成立；若均量不含当日则
    # 2×mean(1000,100,100)=800，500>800 不成立。
    closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    vols = [100, 100, 1000, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)
    signals = s.generate_signals(make_bars(closes, closes, vols))
    assert list(signals) == [False] * 5 + [True]


def test_uptrend_limit_down_treats_tiny_ma_difference_as_equal():
    closes = [10.0, 10.0, 10.0, 10.0, 10.0 + 1e-9, 9.0]
    vols = [100, 100, 100, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)

    signals = s.generate_signals(make_bars(closes, closes, vols))

    assert not signals.any()


def test_uptrend_limit_down_defaults_and_warmup():
    s = get_strategy("uptrend_limit_down")
    assert s.p.ma_short == 20
    assert s.p.ma_long == 60
    assert s.p.limit_pct == 0.095
    assert s.p.vol_ma == 20
    assert s.p.vol_ratio == 2.0
    assert s.warmup_days == 61
