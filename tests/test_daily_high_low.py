import pandas as pd

from quant.data.daily_high_low import (
    aggregate_minute_high_low,
    changed_high_low_rows,
    date_windows,
    merge_daily_high_low,
)


def test_aggregate_minute_high_low_uses_0930_to_1500():
    bars = pd.DataFrame({
        "trade_time": [
            "2026-07-14 09:25:00",
            "2026-07-14 09:30:00",
            "2026-07-14 09:31:00",
            "2026-07-14 11:30:00",
            "2026-07-14 13:00:00",
            "2026-07-14 15:00:00",
            "2026-07-14 15:01:00",
            "2026-07-15 09:31:00",
        ],
        "open": [100, 10, 11, 12, 13, 14, 100, 20],
        "high": [100, 11, 12, 13, 14, 15, 100, 21],
        "low": [100, 9, 10, 8, 11, 7, 100, 19],
        "close": [100, 10, 11, 12, 13, 14, 100, 20],
    })

    agg = aggregate_minute_high_low(bars).set_index("trade_date")

    assert list(agg.index) == ["20260714", "20260715"]
    assert agg.loc["20260714", "minute_high"] == 15
    assert agg.loc["20260714", "minute_low"] == 7
    assert agg.loc["20260714", "minute_open"] == 10
    assert agg.loc["20260714", "minute_close"] == 14
    assert agg.loc["20260714", "minute_bars"] == 5
    assert agg.loc["20260715", "minute_high"] == 21
    assert agg.loc["20260715", "minute_low"] == 19


def test_merge_clamps_high_low_to_open_and_close():
    daily = pd.DataFrame({
        "ts_code": ["002208.SZ"],
        "trade_date": ["20260714"],
        "open": [20.0],
        "close": [18.0],
        "high": [19.0],
        "low": [18.5],
    })
    minute_agg = pd.DataFrame({
        "trade_date": ["20260714"],
        "minute_high": [19.5],
        "minute_low": [18.5],
    })

    merged = merge_daily_high_low(daily, minute_agg, clamp_ohlc=True)
    assert merged.iloc[0]["new_high"] == 20.0
    assert merged.iloc[0]["new_low"] == 18.0

    changed = changed_high_low_rows(merged)
    assert len(changed) == 1


def test_date_windows_keep_all_dates():
    dates = [f"202607{i:02d}" for i in range(1, 8)]
    windows = date_windows(dates, 3)
    assert [len(w) for w in windows] == [3, 3, 1]
    assert [w[0] for w in windows] == ["20260701", "20260704", "20260707"]
