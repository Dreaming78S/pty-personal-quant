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
