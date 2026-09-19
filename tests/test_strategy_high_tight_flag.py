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


def test_high_tight_flag_momentum_only_clause_missing():
    # 末根：动量 10.0/6.5≈1.538<1.6 不满足，其余（收敛/支撑/缩量）均满足。
    lows = [10.0, 10.0, 6.5, 6.5, 9.5, 9.6, 9.4]
    highs = [10.3, 10.3, 9.8, 9.9, 9.9, 10.0, 9.95]
    closes = [10.1, 10.2, 7.0, 7.0, 9.7, 9.8, 9.6]
    vols = [100, 100, 100, 100, 100, 100, 30]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(lows, highs, closes, vols))
    assert not signals.any()


def test_high_tight_flag_tight_only_clause_missing():
    # 末根：收敛 10.0/8.2≈1.22≥1.15 不满足，其余（动量/支撑/缩量）均满足。
    lows = [10.0, 10.0, 5.0, 5.0, 8.2, 9.5, 9.4]
    highs = [10.3, 10.3, 9.8, 9.9, 9.9, 10.0, 9.95]
    closes = [10.1, 10.2, 7.0, 7.0, 9.0, 9.8, 9.6]
    vols = [100, 100, 100, 100, 100, 100, 30]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(lows, highs, closes, vols))
    assert not signals.any()


def test_high_tight_flag_quiet_excludes_today():
    # 末根 60 < 0.6×mean(150,100,100)=70 满足；若均量含当日则
    # 0.6×mean(100,100,60)=52，60<52 不成立。vol[2] 压低使前几根不缩量。
    vols = [100, 100, 1, 150, 100, 100, 60]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(LOWS, HIGHS, CLOSES, vols))
    assert list(signals) == [False] * 6 + [True]


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
