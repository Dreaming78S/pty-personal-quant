import pandas as pd

from quant.engine import loader
from quant.engine.loader import UniverseFilters


def _frames():
    dates = ["20240101", "20240102", "20240103"]
    daily = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "open": [10.0] * 6, "high": [11.0] * 6, "low": [9.0] * 6,
        "close": [10.0] * 6, "pre_close": [10.0] * 6,
        "vol": [100.0] * 6, "amount": [1000.0] * 6,
    })
    adj = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "adj_factor": [2.0] * 6,
    })
    basic = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "turnover_rate": [1.0] * 6, "volume_ratio": [1.0] * 6,
        "pe_ttm": [10.0] * 6, "pb": [1.0] * 6,
        "total_mv": [1000.0] * 6, "circ_mv": [800.0] * 6,
    })
    suspend = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240102"],
        "suspend_timing": ["全天"], "suspend_type": ["S"],
    })
    limits = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "up_limit": [11.0] * 6, "down_limit": [9.0] * 6,
    })
    stock_basic = pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "list_date": ["20230101", "20240103"],
    })
    namechange = pd.DataFrame({
        "ts_code": ["000001.SZ"], "name": ["*ST平安"],
        "start_date": ["20240103"], "end_date": [""],
    })
    trade_cal = pd.DataFrame({
        "exchange": ["SSE"] * 3, "cal_date": dates, "is_open": [1, 1, 1],
    })
    return daily, adj, basic, suspend, limits, stock_basic, namechange, trade_cal


def test_build_market_adjusts_and_flags():
    frames = _frames()
    filters = UniverseFilters(min_list_days=2)
    market = loader.build_market(*frames, filters)

    row = market[(market["ts_code"] == "600000.SH") & (market["trade_date"] == "20240101")].iloc[0]
    assert row["close"] == 20.0          # 后复权
    assert row["raw_close"] == 10.0
    assert row["board"] == "main"
    assert row["up_limit"] == 11.0

    st_rows = market[(market["ts_code"] == "000001.SZ") & (market["trade_date"] == "20240103")]
    assert st_rows.iloc[0]["is_st"] is True or st_rows.iloc[0]["is_st"]
    assert not st_rows.iloc[0]["suspended"]

    susp = market[(market["ts_code"] == "000001.SZ") & (market["trade_date"] == "20240102")]
    assert susp.iloc[0]["suspended"]

    new = market[(market["ts_code"] == "000001.SZ") & (market["trade_date"] == "20240103")]
    assert new.iloc[0]["is_new"]  # 上市第 1 个交易日


def test_apply_universe_filters():
    frames = _frames()
    market = loader.build_market(*frames, UniverseFilters(min_list_days=0))
    kept = loader.apply_universe(market, UniverseFilters(min_list_days=0))
    # 000001.SZ 20240102 停牌、20240103 为 ST，均被剔除；600000.SH 保留
    assert ("000001.SZ", "20240103") not in set(zip(kept["ts_code"], kept["trade_date"]))
    assert ("000001.SZ", "20240102") not in set(zip(kept["ts_code"], kept["trade_date"]))
    assert ("600000.SH", "20240101") in set(zip(kept["ts_code"], kept["trade_date"]))


def test_apply_universe_excludes_new_stock():
    frames = _frames()
    market = loader.build_market(*frames, UniverseFilters(min_list_days=2))
    kept = loader.apply_universe(market, UniverseFilters(min_list_days=2))
    # 000001.SZ 于 20240103 上市，前 2 个交易日内视为次新
    assert ("000001.SZ", "20240103") not in set(zip(kept["ts_code"], kept["trade_date"]))


def test_apply_universe_board_filter():
    frames = _frames()
    market = loader.build_market(*frames, UniverseFilters())
    kept = loader.apply_universe(market, UniverseFilters(allowed_boards=("star",)))
    assert kept.empty
