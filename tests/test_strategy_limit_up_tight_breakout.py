import pandas as pd
import pytest
from pydantic import ValidationError

from quant.strategies import get_strategy

# N=4：T-4 涨停，随后 3 日窄幅缩量，T 日放量涨停。
RAW_CLOSE = [11.0, 11.0, 11.05, 11.02, 12.15]
HIGHS = [11.30, 11.15, 11.15, 11.10, 12.30]
LOWS = [10.80, 10.92, 10.98, 10.95, 11.00]
VOLS = [1000, 500, 500, 500, 1500]
RAW_PRE_CLOSE = [10.0, 11.0, 11.0, 11.05, 11.02]


def make_bars(raw_close, highs, lows, vols, raw_pre_close=None):
    n = len(raw_close)
    if raw_pre_close is None:
        raw_pre_close = [raw_close[0] / 1.10] + list(raw_close[:-1])
    return pd.DataFrame({
        "trade_date": [f"202601{i + 1:02d}" for i in range(n)],
        "open": raw_close,
        "high": highs,
        "low": lows,
        "close": raw_close,
        "raw_close": raw_close,
        "raw_pre_close": raw_pre_close,
        "vol": vols,
        "amount": [c * v for c, v in zip(raw_close, vols)],
    })


def signals_for(raw_close=RAW_CLOSE, highs=HIGHS, lows=LOWS, vols=VOLS,
                raw_pre_close=RAW_PRE_CLOSE, **params):
    s = get_strategy("limit_up_tight_breakout", **params)
    return s.generate_signals(
        make_bars(raw_close, highs, lows, vols, raw_pre_close))


def test_limit_up_tight_breakout_true_on_last_bar():
    signals = signals_for()
    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_fires_for_n_inside_range():
    # N=5：首轮涨停在 T-5，之后 4 日整理。
    raw_close = [11.0, 11.0, 11.02, 11.04, 11.03, 12.12]
    highs = [11.30, 11.15, 11.15, 11.15, 11.12, 12.25]
    lows = [10.80, 10.92, 10.95, 10.98, 10.94, 11.01]
    vols = [1000, 500, 500, 500, 500, 1500]
    raw_pre_close = [10.0, 11.0, 11.0, 11.02, 11.04, 11.03]

    signals = signals_for(raw_close, highs, lows, vols, raw_pre_close)

    assert list(signals) == [False] * 5 + [True]


def test_limit_up_tight_breakout_gap_range_is_configurable():
    # N=3 不在默认 [4, 10] 内；显式放宽后才命中。
    raw_close = [11.0, 11.0, 11.02, 12.10]
    highs = [11.30, 11.15, 11.15, 12.25]
    lows = [10.80, 10.92, 10.98, 11.01]
    vols = [1000, 500, 500, 1500]
    raw_pre_close = [10.0, 11.0, 11.0, 11.02]

    default = signals_for(raw_close, highs, lows, vols, raw_pre_close)
    adjusted = signals_for(raw_close, highs, lows, vols, raw_pre_close,
                           min_gap_days=3, max_gap_days=3)

    assert not default.any()
    assert list(adjusted) == [False, False, False, True]


def test_limit_up_tight_breakout_false_when_consolidation_range_too_wide():
    # 整理期最高 12.10、最低 10.92，振幅 10.7% > 10%。
    highs = [11.30, 11.15, 12.10, 11.10, 12.30]
    assert not signals_for(highs=highs).any()


def test_limit_up_tight_breakout_false_when_single_day_move_too_large():
    # 整理期第 2 日 +5%，超过单日 4% 限制。
    raw_close = [11.0, 11.0, 11.55, 11.50, 12.65]
    highs = [11.30, 11.15, 11.70, 11.60, 12.80]
    lows = [10.80, 10.92, 11.00, 11.40, 11.60]
    vols = [1000, 500, 500, 500, 2000]
    raw_pre_close = [10.0, 11.0, 11.0, 11.55, 11.50]

    assert not signals_for(raw_close, highs, lows, vols, raw_pre_close).any()


def test_limit_up_tight_breakout_false_when_cumulative_move_too_large():
    # 整理期累计 +5.45%，超过 5%；单日涨幅均 <4%。
    raw_close = [11.0, 11.3, 11.5, 11.6, 12.75]
    highs = [11.35, 11.45, 11.60, 11.65, 12.90]
    lows = [10.80, 11.20, 11.40, 11.55, 11.65]
    vols = [1000, 500, 500, 500, 2000]
    raw_pre_close = [10.0, 11.0, 11.3, 11.5, 11.6]

    assert not signals_for(raw_close, highs, lows, vols, raw_pre_close).any()


def test_limit_up_tight_breakout_false_when_avg_volume_not_shrunk():
    # 整理期均量 700 > 首轮涨停量 1000 × 60%；单日最高量仍 ≤ 80%。
    vols = [1000, 700, 700, 700, 2000]
    assert not signals_for(vols=vols).any()


def test_limit_up_tight_breakout_false_when_single_day_volume_too_high():
    # 整理期均量 566.7 ≤ 600，但单日最高 900 > 1000 × 80%。
    vols = [1000, 400, 400, 900, 2000]
    assert not signals_for(vols=vols).any()


def test_limit_up_tight_breakout_false_when_breakout_volume_too_low():
    # T 日量 1499：满足 ≥ 均量×2，但不满足 ≥ 首轮涨停量×1.5。
    vols = [1000, 500, 500, 500, 1499]
    assert not signals_for(vols=vols).any()


def test_limit_up_tight_breakout_unlimited_range():
    highs = [11.30, 11.15, 12.10, 11.10, 12.30]
    signals = signals_for(highs=highs, range_pct="unlimited")
    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_unlimited_daily_move():
    raw_close = [11.0, 11.0, 11.55, 11.50, 12.65]
    highs = [11.30, 11.15, 11.70, 11.60, 12.80]
    lows = [10.80, 10.92, 11.00, 11.40, 11.60]
    vols = [1000, 500, 500, 500, 2000]
    raw_pre_close = [10.0, 11.0, 11.0, 11.55, 11.50]

    signals = signals_for(raw_close, highs, lows, vols, raw_pre_close,
                          daily_pct="unlimited")

    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_unlimited_cumulative():
    raw_close = [11.0, 11.3, 11.5, 11.6, 12.75]
    highs = [11.35, 11.45, 11.60, 11.65, 12.90]
    lows = [10.80, 11.20, 11.40, 11.55, 11.65]
    vols = [1000, 500, 500, 500, 2000]
    raw_pre_close = [10.0, 11.0, 11.3, 11.5, 11.6]

    signals = signals_for(raw_close, highs, lows, vols, raw_pre_close,
                          cumulative_pct="unlimited")

    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_unlimited_shrink_avg():
    vols = [1000, 700, 700, 700, 2000]
    signals = signals_for(vols=vols, shrink_avg_ratio="unlimited")
    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_unlimited_shrink_max():
    vols = [1000, 400, 400, 900, 2000]
    signals = signals_for(vols=vols, shrink_max_ratio="unlimited")
    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_unlimited_breakout_volume():
    vols = [1000, 500, 500, 500, 1499]
    signals = signals_for(vols=vols, breakout_avg_vol_ratio="unlimited",
                          breakout_base_vol_ratio="unlimited")
    assert list(signals) == [False, False, False, False, True]


def test_limit_up_tight_breakout_false_when_today_not_limit_up():
    raw_close = [11.0, 11.0, 11.05, 11.02, 12.00]
    highs = [11.30, 11.15, 11.15, 11.10, 12.15]
    lows = [10.80, 10.92, 10.98, 10.95, 11.00]
    assert not signals_for(raw_close=raw_close, highs=highs).any()


def test_limit_up_tight_breakout_false_when_first_day_not_limit_up():
    # 首轮日仅 +4.76%，不构成涨停。
    raw_pre_close = [10.5, 11.0, 11.0, 11.05, 11.02]
    assert not signals_for(raw_pre_close=raw_pre_close).any()


def test_limit_up_tight_breakout_no_lookahead_on_truncated_bars():
    s = get_strategy("limit_up_tight_breakout")
    bars = make_bars(RAW_CLOSE, HIGHS, LOWS, VOLS, RAW_PRE_CLOSE)

    full = s.generate_signals(bars)
    truncated = s.generate_signals(bars.iloc[:4].copy())

    pd.testing.assert_series_equal(full.iloc[:4], truncated)


def test_limit_up_tight_breakout_defaults_and_warmup():
    s = get_strategy("limit_up_tight_breakout")
    assert s.p.min_gap_days == 4
    assert s.p.max_gap_days == 10
    assert s.p.limit_pct == 0.095
    assert s.p.range_pct == 0.10
    assert s.p.daily_pct == 0.04
    assert s.p.cumulative_pct == 0.05
    assert s.p.shrink_avg_ratio == 0.60
    assert s.p.shrink_max_ratio == 0.80
    assert s.p.breakout_avg_vol_ratio == 2.0
    assert s.p.breakout_base_vol_ratio == 1.5
    assert s.warmup_days == 11


def test_limit_up_tight_breakout_rejects_invalid_gap_range():
    with pytest.raises(ValidationError):
        get_strategy("limit_up_tight_breakout", min_gap_days=5,
                     max_gap_days=4)
    with pytest.raises(ValidationError):
        get_strategy("limit_up_tight_breakout", min_gap_days=1)
