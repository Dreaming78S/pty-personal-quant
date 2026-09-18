import pandas as pd

from quant.strategies import get_strategy

SMALL_PARAMS = dict(lookback=5, min_gain=0.2, tight_days=3, max_range=0.05,
                    vol_shrink=0.5, vol_base_days=3, high_days=5, near_high=0.05)


def make_bars(closes, vols):
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(len(closes))],
        "open": closes,
        "high": [c + 0.05 for c in closes],
        "low": [c - 0.05 for c in closes],
        "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_high_tight_flag_true_on_last_bar():
    closes = [10, 10, 10, 10, 10, 13, 13.5, 13.2, 13.3, 13.4]
    vols = [100, 100, 100, 100, 100, 300, 300, 300, 20, 20]
    s = get_strategy("high_tight_flag", **SMALL_PARAMS)

    signals = s.generate_signals(make_bars(closes, vols))

    assert list(signals)[-1] is True or signals.iloc[-1]
    assert not signals.iloc[:-1].any()


def test_high_tight_flag_false_without_volume_dry_up():
    closes = [10, 10, 10, 10, 10, 13, 13.5, 13.2, 13.3, 13.4]
    vols = [100, 100, 100, 100, 100, 300, 300, 300, 300, 300]
    s = get_strategy("high_tight_flag", **SMALL_PARAMS)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_high_tight_flag_warmup():
    s = get_strategy("high_tight_flag")
    assert s.warmup_days == 260
