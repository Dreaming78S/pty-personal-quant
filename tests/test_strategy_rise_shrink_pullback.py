import pandas as pd
import pytest
from pydantic import ValidationError

from quant.strategies import get_strategy


def make_bars(closes, opens, vols):
    return pd.DataFrame({
        "trade_date": [f"202601{i + 1:02d}" for i in range(len(closes))],
        "open": opens,
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
        "raw_close": closes,
    })


def test_rise_shrink_pullback_fires_when_all_conditions_met():
    bars = make_bars(
        closes=[10, 10, 11, 12, 13],
        opens=[10, 10, 10.5, 12.5, 13.2],
        vols=[100, 100, 100, 50, 50],
    )
    s = get_strategy("rise_shrink_pullback", rise_days=3, rise_pct=25.0,
                     pullback_days=2, min_bearish=1, vol_shrink=0.80)

    signals = s.generate_signals(bars)

    assert list(signals) == [False, False, False, False, True]


def test_rise_shrink_pullback_requires_rise_above_threshold():
    bars = make_bars(
        closes=[10, 10, 11, 12, 12.4],
        opens=[10, 10, 10.5, 12.5, 13.2],
        vols=[100, 100, 100, 50, 50],
    )
    s = get_strategy("rise_shrink_pullback", rise_days=3, rise_pct=25.0,
                     pullback_days=2, min_bearish=1, vol_shrink=0.80)

    assert not s.generate_signals(bars).any()


def test_rise_shrink_pullback_rise_exactly_at_threshold_not_enough():
    bars = make_bars(
        closes=[10, 10, 11, 12, 12.5],
        opens=[10, 10, 10.5, 12.5, 13.2],
        vols=[100, 100, 100, 50, 50],
    )
    s = get_strategy("rise_shrink_pullback", rise_days=3, rise_pct=25.0,
                     pullback_days=2, min_bearish=1, vol_shrink=0.80)

    assert not s.generate_signals(bars).any()


def test_rise_shrink_pullback_requires_enough_bearish_bars():
    bars = make_bars(
        closes=[10, 10, 11, 12, 13],
        opens=[10, 10, 11, 12, 13],
        vols=[100, 100, 100, 50, 50],
    )
    s = get_strategy("rise_shrink_pullback", rise_days=3, rise_pct=25.0,
                     pullback_days=2, min_bearish=1, vol_shrink=0.80)

    assert not s.generate_signals(bars).any()


def test_rise_shrink_pullback_bearish_count_exactly_min_passes():
    bars = make_bars(
        closes=[10, 10, 11, 12, 13],
        opens=[10, 10, 10.5, 12.5, 13.2],
        vols=[100, 100, 100, 50, 50],
    )
    s = get_strategy("rise_shrink_pullback", rise_days=3, rise_pct=25.0,
                     pullback_days=2, min_bearish=2, vol_shrink=0.80)

    signals = s.generate_signals(bars)

    assert list(signals) == [False, False, False, False, True]


def test_rise_shrink_pullback_requires_volume_shrink():
    bars = make_bars(
        closes=[10, 10, 11, 12, 13],
        opens=[10, 10, 10.5, 12.5, 13.2],
        vols=[100, 100, 100, 150, 160],
    )
    s = get_strategy("rise_shrink_pullback", rise_days=3, rise_pct=25.0,
                     pullback_days=2, min_bearish=1, vol_shrink=0.80)

    assert not s.generate_signals(bars).any()


def test_rise_shrink_pullback_bearish_mode_switch():
    closes = [10, 10, 13, 14, 13.5]
    opens = [10, 10, 12.5, 13.5, 13.4]
    vols = [100, 100, 100, 50, 40]
    kwargs = dict(rise_days=3, rise_pct=25.0, pullback_days=2,
                  min_bearish=1, vol_shrink=0.80)

    body = get_strategy("rise_shrink_pullback", **kwargs)
    prev_close = get_strategy("rise_shrink_pullback",
                              bearish_mode="close_below_prev_close", **kwargs)

    body_signals = body.generate_signals(make_bars(closes, opens, vols))
    prev_signals = prev_close.generate_signals(make_bars(closes, opens, vols))

    assert not body_signals.any()
    assert list(prev_signals) == [False, False, False, False, True]


def test_rise_shrink_pullback_defaults_and_warmup():
    s = get_strategy("rise_shrink_pullback")

    assert s.p.rise_days == 10
    assert s.p.rise_pct == 25.0
    assert s.p.pullback_days == 3
    assert s.p.min_bearish == 2
    assert s.p.vol_shrink == 0.80
    assert s.p.bearish_mode == "close_below_open"
    assert s.warmup_days == 11


def test_rise_shrink_pullback_no_signal_when_short_warmup_rows():
    s = get_strategy("rise_shrink_pullback")
    bars = make_bars(closes=[10, 11], opens=[10, 11], vols=[100, 100])

    assert not s.generate_signals(bars).any()


def test_rise_shrink_pullback_rejects_rise_days_not_above_pullback_days():
    with pytest.raises(ValidationError):
        get_strategy("rise_shrink_pullback", rise_days=3, pullback_days=3)


def test_rise_shrink_pullback_rejects_unknown_bearish_mode():
    with pytest.raises(ValidationError):
        get_strategy("rise_shrink_pullback", bearish_mode="close_below_whatever")
