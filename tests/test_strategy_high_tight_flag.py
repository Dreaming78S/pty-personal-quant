import pandas as pd

from quant.strategies import get_strategy

PARAMS = dict(momentum_days=5, momentum_ratio=1.6, tight_days=3,
              tight_ratio=1.15, support_ratio=0.8, vol_base_days=3,
              vol_shrink=0.6)

LOWS = [10.0, 10.0, 10.0, 16.2, 16.1, 16.2, 16.1]
HIGHS = [10.3, 10.3, 20.0, 16.6, 16.5, 16.5, 16.5]
CLOSES = [10.1, 10.2, 19.5, 16.4, 16.3, 16.4, 16.3]
VOLS = [100, 100, 300, 100, 100, 100, 30]


def make_bars(lows, highs, closes, vols):
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(len(closes))],
        "open": closes, "high": highs, "low": lows, "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_high_tight_flag_true_on_last_bar():
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(LOWS, HIGHS, CLOSES, VOLS))
    assert list(signals) == [False] * 6 + [True]


def test_high_tight_flag_false_without_volume_dry_up():
    vols = [100, 100, 300, 100, 100, 100, 100]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(LOWS, HIGHS, CLOSES, vols))
    assert not signals.any()


def test_high_tight_flag_false_when_support_breaks():
    lows = LOWS[:-1] + [15.0]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(lows, HIGHS, CLOSES, VOLS))
    assert not signals.any()


def test_high_tight_flag_defaults_and_warmup():
    s = get_strategy("high_tight_flag")
    assert s.p.momentum_days == 40
    assert s.p.momentum_ratio == 1.6
    assert s.p.tight_days == 10
    assert s.p.tight_ratio == 1.15
    assert s.p.support_ratio == 0.8
    assert s.p.vol_base_days == 20
    assert s.p.vol_shrink == 0.6
    assert s.warmup_days == 40
