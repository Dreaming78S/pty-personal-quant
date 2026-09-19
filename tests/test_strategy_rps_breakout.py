import pandas as pd

from quant.engine import selection
from quant.strategies import get_strategy

PARAMS = dict(window=3, rps_threshold=90.0, high_ratio=0.9, high_min_bars=2)

DATES = ["20240101", "20240102", "20240103", "20240104"]


def make_market(series, suspended=None):
    rows = []
    for code, closes in series.items():
        for i, (d, c) in enumerate(zip(DATES[-len(closes):], closes)):
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": c, "high": c, "low": c, "close": c,
                "raw_close": c, "vol": 100.0, "amount": 1000.0,
                "adj_factor": 1.0, "suspended": bool(suspended and code in suspended),
                "is_st": False, "is_new": False, "board": "main",
            })
    return pd.DataFrame(rows)


def test_prepare_adds_rps_and_does_not_mutate_input():
    market = make_market({"600000.SH": [1, 1, 1, 2]})
    s = get_strategy("rps_breakout", **PARAMS)

    enriched = s.prepare(market)

    assert "rps" not in market.columns
    assert enriched["rps"].iloc[-1] == 100.0


def test_rps_breakout_selects_top_percentile():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1, 1, 1, 1.5],
        "600002.SH": [1, 1, 1, 1.1],
        "600003.SH": [1, 1, 1, 0.9],
    })
    s = get_strategy("rps_breakout", **PARAMS)

    signals = selection.compute_signals(s, market)
    last = signals[signals["trade_date"] == "20240104"]
    hit = last[last["signal"]]["ts_code"].tolist()

    assert hit == ["600000.SH"]


def test_rps_breakout_excludes_suspended_from_ranking():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1, 1, 1, 1.5],
        "600009.SH": [1, 1, 1, 5.0],
    }, suspended={"600009.SH"})
    s = get_strategy("rps_breakout", **PARAMS)

    signals = selection.compute_signals(s, market)
    last = signals[signals["trade_date"] == "20240104"].set_index("ts_code")

    assert last.loc["600000.SH", "score"] == 100.0
    assert not last.loc["600009.SH", "signal"]


def test_rps_breakout_skips_insufficient_window():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1.0, 5.0],
    })
    s = get_strategy("rps_breakout", **PARAMS)

    enriched = s.prepare(market)
    short = enriched[enriched["ts_code"] == "600001.SH"]

    assert short["rps"].isna().all()
    signals = selection.compute_signals(s, market)
    assert not signals[signals["ts_code"] == "600001.SH"]["signal"].any()


def test_rps_breakout_no_lookahead_on_truncated_market():
    market = make_market({
        "600000.SH": [1, 1, 1.2, 2.0],
        "600001.SH": [1, 1, 1.0, 1.5],
        "600002.SH": [1, 1, 0.9, 1.1],
    })
    s = get_strategy("rps_breakout", window=1, rps_threshold=90.0,
                     high_ratio=0.9, high_min_bars=1)

    full = selection.compute_signals(s, market)
    truncated = selection.compute_signals(
        s, market[market["trade_date"] <= "20240103"])
    common = full[full["trade_date"] <= "20240103"].reset_index(drop=True)

    assert common["score"].notna().any()
    assert common["signal"].any()
    pd.testing.assert_series_equal(common["signal"], truncated["signal"])
    pd.testing.assert_series_equal(common["score"], truncated["score"])


def test_rps_breakout_defaults_and_warmup():
    s = get_strategy("rps_breakout")
    assert s.p.window == 120
    assert s.p.rps_threshold == 90.0
    assert s.p.high_ratio == 0.90
    assert s.p.high_min_bars == 60
    assert s.warmup_days == 121


def _rows(code, entries, suspended=False):
    return [{
        "ts_code": code, "trade_date": d,
        "open": c, "high": h, "low": c, "close": c, "raw_close": c,
        "vol": 100.0, "amount": 1000.0, "adj_factor": 1.0,
        "suspended": suspended, "is_st": False, "is_new": False, "board": "main",
    } for d, c, h in entries]


def test_rps_reference_is_trading_date_not_row_count():
    # B 在 20240103 停牌：窗口 3 的基准日应为 20240103，B 当日无 120 日涨幅 → 不参与排名
    market = pd.DataFrame(
        _rows("600000.SH", [("20240101", 1.0, 1.0), ("20240102", 1.0, 1.0),
                            ("20240103", 1.0, 1.0), ("20240104", 1.0, 1.0),
                            ("20240105", 1.0, 1.0), ("20240106", 2.0, 2.0)])
        + _rows("600001.SH", [("20240101", 1.0, 1.0), ("20240102", 1.0, 1.0),
                              ("20240104", 1.0, 1.0), ("20240105", 1.0, 1.0),
                              ("20240106", 5.0, 5.0)])
    )
    s = get_strategy("rps_breakout", **PARAMS)

    signals = selection.compute_signals(s, market)
    last = signals[signals["trade_date"] == "20240106"].set_index("ts_code")

    assert pd.isna(last.loc["600001.SH", "score"])
    assert not last.loc["600001.SH", "signal"]
    assert last.loc["600000.SH", "score"] == 100.0


def test_rps_high_window_uses_trading_dates():
    # C 缺 20240105：窗口 3 的高点区间应为 20240104/06 两根（除息日无行情不计入）
    market = pd.DataFrame(
        _rows("600000.SH", [("20240101", 1.0, 1.0), ("20240102", 1.0, 1.0),
                            ("20240103", 1.0, 1.0), ("20240104", 1.0, 1.0),
                            ("20240105", 1.0, 1.0), ("20240106", 1.0, 1.0)])
        + _rows("600001.SH", [("20240101", 1.0, 1.0), ("20240102", 1.0, 1.0),
                              ("20240103", 1.0, 100.0), ("20240104", 1.0, 1.0),
                              ("20240106", 10.4, 10.5)])
    )
    s = get_strategy("rps_breakout", **PARAMS)

    signals = selection.compute_signals(s, market)
    last = signals[signals["trade_date"] == "20240106"].set_index("ts_code")

    # 按行滚动会把停牌前的 100 计入窗口；按交易日窗口只取 20240104/06
    assert last.loc["600001.SH", "score"] == 100.0
    assert last.loc["600001.SH", "signal"]


def test_rps_short_panel_with_enough_warmup_matches_full_panel():
    dates = ["20240101", "20240102", "20240103", "20240104",
             "20240105", "20240106"]
    market = pd.DataFrame(
        _rows("600000.SH", [(d, c, c) for d, c in
                            zip(dates, [1, 1, 1, 1.2, 1.3, 2.0])])
        + _rows("600001.SH", [(d, c, c) for d, c in
                              zip(dates, [1, 1, 1, 1.1, 1.1, 1.5])])
        + _rows("600002.SH", [(d, c, c) for d, c in
                              zip(dates, [1, 1, 1, 0.9, 0.8, 1.1])])
    )
    s = get_strategy("rps_breakout", **PARAMS)

    full = selection.compute_signals(s, market)
    tail = selection.compute_signals(
        s, market[market["trade_date"] >= "20240103"])

    common_full = full[full["trade_date"] == "20240106"].set_index("ts_code")
    common_tail = tail[tail["trade_date"] == "20240106"].set_index("ts_code")
    pd.testing.assert_series_equal(common_full["signal"], common_tail["signal"])
    pd.testing.assert_series_equal(common_full["score"], common_tail["score"])
