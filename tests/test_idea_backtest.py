import pandas as pd
import pytest

from quant.engine import idea_backtest

DATES = ["20240102", "20240103", "20240104", "20240105", "20240108"]


def make_hits(rows):
    return pd.DataFrame(rows, columns=["strategy", "ts_code", "trade_date"])


def test_screen_ideas_default_keeps_every_hit_stock_day():
    hits = make_hits([
        ("ma_volume", "600001.SH", "20240102"),
        ("ma_volume", "600001.SH", "20240103"),
        ("ma_volume", "600002.SH", "20240102"),
        ("rps_breakout", "600002.SH", "20240102"),
        ("turtle_trade", "600002.SH", "20240102"),
        ("rps_breakout", "600003.SH", "20240103"),
        ("turtle_trade", "600003.SH", "20240103"),
        ("ma_volume", "600004.SH", "20240105"),
    ])

    out = idea_backtest.screen_ideas(hits, DATES, "20240102", "20240108")

    assert list(out.columns) == ["ts_code", "trade_date", "n_t1", "n_t2",
                                 "n_cum", "strategies"]
    assert list(zip(out["trade_date"], out["ts_code"])) == [
        ("20240102", "600001.SH"),
        ("20240102", "600002.SH"),
        ("20240103", "600001.SH"),
        ("20240103", "600003.SH"),
        ("20240105", "600004.SH"),
    ]
    assert list(out["n_t1"]) == [1, 3, 1, 2, 1]
    assert list(out["n_t2"]) == [0, 0, 1, 0, 0]
    assert list(out["n_cum"]) == [1, 3, 1, 2, 1]
    assert out.loc[1, "strategies"] == ("ma_volume", "rps_breakout",
                                        "turtle_trade")


def test_screen_ideas_requires_all_named_strategies_on_t1():
    hits = make_hits([
        ("ma_volume", "600001.SH", "20240102"),
        ("ma_volume", "600002.SH", "20240102"),
        ("rps_breakout", "600002.SH", "20240102"),
        ("turtle_trade", "600002.SH", "20240102"),
        ("rps_breakout", "600003.SH", "20240103"),
        ("turtle_trade", "600003.SH", "20240103"),
    ])

    out = idea_backtest.screen_ideas(
        hits, DATES, "20240102", "20240108",
        t1_strategies=("ma_volume", "rps_breakout"), t1_min_count=2)

    assert list(zip(out["trade_date"], out["ts_code"])) == [
        ("20240102", "600002.SH")]

    none = idea_backtest.screen_ideas(
        hits, DATES, "20240102", "20240108",
        t1_strategies=("ma_volume", "rps_breakout"), t1_min_count=4)
    assert none.empty
    assert list(none.columns) == ["ts_code", "trade_date", "n_t1", "n_t2",
                                  "n_cum", "strategies"]


def test_screen_ideas_t2_conditions():
    hits = make_hits([
        ("ma_volume", "600001.SH", "20240103"),
        ("rps_breakout", "600001.SH", "20240104"),
        ("ma_volume", "600002.SH", "20240104"),
        ("turtle_trade", "600003.SH", "20240103"),
        ("ma_volume", "600003.SH", "20240103"),
        ("ma_volume", "600003.SH", "20240104"),
    ])

    def codes(**kwargs):
        out = idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                         **kwargs)
        return sorted(out["ts_code"])

    assert codes() == ["600001.SH", "600002.SH", "600003.SH"]
    assert codes(t2_min_count=1) == ["600001.SH", "600003.SH"]
    assert codes(t2_min_count=2) == ["600003.SH"]
    assert codes(t2_strategies=("ma_volume",)) == ["600001.SH", "600003.SH"]
    assert codes(t2_strategies=("turtle_trade",)) == ["600003.SH"]
    assert codes(t2_strategies=("rps_breakout",)) == []


def test_screen_ideas_cumulative_window():
    hits = make_hits([
        ("turtle_trade", "600001.SH", "20240102"),
        ("ma_volume", "600001.SH", "20240103"),
        ("rps_breakout", "600001.SH", "20240104"),
        ("turtle_trade", "600002.SH", "20240103"),
        ("rps_breakout", "600002.SH", "20240104"),
        ("rps_breakout", "600003.SH", "20240104"),
    ])

    def codes(**kwargs):
        out = idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                         **kwargs)
        return sorted(out["ts_code"])

    out = idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104")
    assert list(out["n_cum"]) == [3, 2, 1]
    assert codes(cum_strategies=("turtle_trade",)) == ["600001.SH", "600002.SH"]
    assert codes(cum_min_count=3) == ["600001.SH"]
    assert codes(cum_strategies=("ma_volume",)) == ["600001.SH"]
    assert codes(cum_strategies=("limit_up_shakeout",)) == []


def test_screen_ideas_respects_start_window():
    hits = make_hits([
        ("ma_volume", "600001.SH", "20240102"),
        ("ma_volume", "600004.SH", "20240105"),
    ])

    out = idea_backtest.screen_ideas(hits, DATES, "20240105", "20240108")

    assert list(out["ts_code"]) == ["600004.SH"]


def test_screen_ideas_max_counts():
    hits = make_hits([
        ("ma_volume", "600001.SH", "20240103"),
        ("rps_breakout", "600001.SH", "20240104"),
        ("turtle_trade", "600002.SH", "20240103"),
        ("ma_volume", "600002.SH", "20240103"),
        ("rps_breakout", "600002.SH", "20240104"),
        ("rps_breakout", "600003.SH", "20240104"),
    ])

    def codes(**kwargs):
        out = idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                         **kwargs)
        return sorted(out["ts_code"])

    # n_t2：600001=1、600002=2、600003=0
    assert codes() == ["600001.SH", "600002.SH", "600003.SH"]
    assert codes(t2_max_count=1) == ["600001.SH", "600003.SH"]
    assert codes(t2_max_count=0) == ["600003.SH"]
    assert codes(t2_min_count=1, t2_max_count=1) == ["600001.SH"]
    # n_cum：600001=2、600002=3、600003=1
    assert codes(cum_max_count=2) == ["600001.SH", "600003.SH"]
    assert codes(cum_min_count=2, cum_max_count=2) == ["600001.SH"]

    with pytest.raises(ValueError):
        idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                   t2_min_count=2, t2_max_count=1)
    with pytest.raises(ValueError):
        idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                   cum_min_count=3, cum_max_count=2)
    with pytest.raises(ValueError):
        idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                   cum_max_count=-1)


def test_screen_ideas_cum_min_none_means_no_minimum():
    hits = make_hits([
        ("ma_volume", "600001.SH", "20240103"),
        ("rps_breakout", "600001.SH", "20240104"),
        ("turtle_trade", "600002.SH", "20240103"),
        ("ma_volume", "600002.SH", "20240103"),
        ("rps_breakout", "600002.SH", "20240104"),
        ("rps_breakout", "600003.SH", "20240104"),
    ])

    def codes(**kwargs):
        out = idea_backtest.screen_ideas(hits, DATES, "20240104", "20240104",
                                         **kwargs)
        return sorted(out["ts_code"])

    # n_cum：600001=2、600002=3、600003=1；cum_min=None 时没有下限
    assert codes(cum_min_count=None) == ["600001.SH", "600002.SH", "600003.SH"]
    assert codes(cum_min_count=None, cum_max_count=1) == ["600003.SH"]
    assert codes(cum_min_count=None, cum_max_count=2) == ["600001.SH", "600003.SH"]


def test_screen_ideas_rejects_invalid_counts():
    hits = make_hits([("ma_volume", "600001.SH", "20240102")])

    with pytest.raises(ValueError):
        idea_backtest.screen_ideas(hits, DATES, "20240102", "20240108",
                                   t1_min_count=0)
    with pytest.raises(ValueError):
        idea_backtest.screen_ideas(hits, DATES, "20240102", "20240108",
                                   cum_min_count=-1)


def make_market_mv():
    return pd.DataFrame([
        ("600001.SH", "20240102", 2_000_000.0),   # 200 亿
        ("600001.SH", "20240103", 1_000_000.0),   # 100 亿
        ("600002.SH", "20240102", 300_000_000.0),  # 30000 亿
        ("600004.SH", "20240105", 500_000.0),     # 50 亿
    ], columns=["ts_code", "trade_date", "total_mv"])


def make_entries():
    return pd.DataFrame([
        ("600001.SH", "20240102"),
        ("600002.SH", "20240102"),
        ("600003.SH", "20240103"),   # 缺市值数据
        ("600004.SH", "20240105"),
        ("600001.SH", "20240103"),
    ], columns=["ts_code", "trade_date"])


def test_filter_by_market_cap_uses_total_mv_in_yi():
    out = idea_backtest.filter_by_market_cap(
        make_entries(), make_market_mv(), 0.0, 50000.0)

    assert list(out["ts_code"]) == ["600001.SH", "600002.SH", "600004.SH",
                                    "600001.SH"]
    assert out.loc[0, "total_mv_yi"] == pytest.approx(200.0)
    assert out.loc[1, "total_mv_yi"] == pytest.approx(30000.0)


def test_filter_by_market_cap_bounds_and_validation():
    entries = make_entries()
    market = make_market_mv()

    small = idea_backtest.filter_by_market_cap(entries, market, 0.0, 1000.0)
    assert list(small["ts_code"]) == ["600001.SH", "600004.SH", "600001.SH"]

    exact = idea_backtest.filter_by_market_cap(entries, market, 100.0, 100.0)
    assert list(zip(exact["ts_code"], exact["trade_date"])) == [
        ("600001.SH", "20240103")]

    with pytest.raises(ValueError):
        idea_backtest.filter_by_market_cap(entries, market, 100.0, 50.0)
