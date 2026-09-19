import pandas as pd

from quant.engine import hit_performance


def make_market():
    dates = ["20260105", "20260106", "20260107", "20260108"]
    rows = []
    for d in dates:
        rows.append({"ts_code": "600000.SH", "trade_date": d,
                     "open": 10.0, "close": 10.0})
    rows.append({"ts_code": "600002.SH", "trade_date": "20260105",
                 "open": 20.0, "close": 20.0})
    rows.append({"ts_code": "600002.SH", "trade_date": "20260106",
                 "open": 20.0, "close": 21.0})
    rows.append({"ts_code": "600002.SH", "trade_date": "20260107",
                 "open": 21.0, "close": 22.0})
    return pd.DataFrame(rows)


def test_forward_returns_alignment_and_win_loss():
    hits = pd.DataFrame({
        "ts_code": ["600000.SH", "600002.SH"],
        "trade_date": ["20260105", "20260105"],
    })

    out = hit_performance.forward_returns(make_market(), hits).set_index("ts_code")

    assert out.loc["600000.SH", "buy_date"] == "20260106"
    assert out.loc["600000.SH", "sell_date"] == "20260107"
    assert out.loc["600000.SH", "ret_pct"] == 0.0
    assert out.loc["600000.SH", "status"] == "ok"
    assert out.loc["600002.SH", "buy_open"] == 20.0
    assert out.loc["600002.SH", "sell_close"] == 22.0
    assert out.loc["600002.SH", "ret_pct"] == 10.0


def test_forward_returns_skips_suspended_and_missing_tail():
    market = make_market()
    hits = pd.DataFrame({
        "ts_code": ["600001.SH", "600000.SH", "600000.SH"],
        "trade_date": ["20260105", "20260108", "20260107"],
    })

    out = hit_performance.forward_returns(market, hits)

    assert list(out["status"]) == ["suspended", "no_data", "no_data"]


def test_bucket_stats_boundaries():
    stats = hit_performance.bucket_stats(
        pd.Series([-6.0, -5.0, -3.0, -2.0, -0.1, 0.0, 0.1, 2.0, 4.9, 5.0]))

    counts = hit_performance.bucket_counts(stats)
    assert counts == {"<-5": 1, "-5~-2": 3, "-2~0": 2, "0~2": 1,
                      "2~5": 2, ">5": 1}
    assert stats["可计算"] == 10
    assert stats["盈利"] == 4


def test_group_co_hits_keeps_only_multi_strategy_events():
    hits = pd.DataFrame({
        "strategy": ["ma_volume", "ma_volume", "turtle_trade", "rps_breakout",
                     "ma_volume", "turtle_trade"],
        "ts_code": ["600000.SH", "600000.SH", "600000.SH", "600000.SH",
                    "600002.SH", "600002.SH"],
        "trade_date": ["20260105", "20260105", "20260105", "20260105",
                       "20260106", "20260107"],
    })

    events = hit_performance.group_co_hits(hits)

    assert len(events) == 1
    row = events.iloc[0]
    assert row["ts_code"] == "600000.SH"
    assert row["trade_date"] == "20260105"
    assert row["strategies"] == ("ma_volume", "rps_breakout", "turtle_trade")
    assert row["n_strategies"] == 3

    all_events = hit_performance.group_co_hits(hits, min_strategies=1)
    assert len(all_events) == 3


def test_bucket_stats_ignores_nan():
    stats = hit_performance.bucket_stats(pd.Series([1.0, None, 2.0]))

    assert stats["可计算"] == 2
    assert hit_performance.bucket_counts(stats)["0~2"] == 1
