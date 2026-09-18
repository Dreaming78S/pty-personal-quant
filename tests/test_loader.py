import datetime
from decimal import Decimal

import pandas as pd

from quant.engine import loader
from quant.engine.loader import UniverseFilters

MARKET_NUMERIC_COLS = (
    "open", "high", "low", "close",
    "raw_open", "raw_high", "raw_low", "raw_close", "raw_pre_close",
    "vol", "amount", "adj_factor", "up_limit", "down_limit",
    "turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv",
)


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


def _to_decimal(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    df = df.copy()
    for col in cols:
        df[col] = df[col].map(lambda value: Decimal(str(value)))
    return df


def _decimal_frames():
    daily, adj, basic, suspend, limits, stock_basic, namechange, trade_cal = _frames()
    daily = _to_decimal(daily, ("open", "high", "low", "close", "pre_close",
                                "vol", "amount"))
    adj = _to_decimal(adj, ("adj_factor",))
    basic = _to_decimal(basic, ("turnover_rate", "volume_ratio", "pe_ttm",
                                "pb", "total_mv", "circ_mv"))
    limits = _to_decimal(limits, ("up_limit", "down_limit"))
    return daily, adj, basic, suspend, limits, stock_basic, namechange, trade_cal


def test_build_market_casts_decimal_columns_to_float64():
    market = loader.build_market(*_decimal_frames(), UniverseFilters(min_list_days=0))

    for col in MARKET_NUMERIC_COLS:
        assert market[col].dtype == "float64", col

    row = market[(market["ts_code"] == "600000.SH") &
                 (market["trade_date"] == "20240101")].iloc[0]
    assert row["close"] == 20.0       # 后复权
    assert row["raw_close"] == 10.0
    assert row["adj_factor"] == 2.0
    assert row["up_limit"] == 11.0


def test_build_market_handles_missing_adj_factor_for_decimal_input():
    frames = list(_decimal_frames())
    adj = frames[1]
    frames[1] = adj[~((adj["ts_code"] == "600000.SH") &
                      (adj["trade_date"] == "20240102"))]

    market = loader.build_market(*frames, UniverseFilters(min_list_days=0))

    row = market[(market["ts_code"] == "600000.SH") &
                 (market["trade_date"] == "20240102")].iloc[0]
    assert market["adj_factor"].dtype == "float64"
    assert pd.isna(row["adj_factor"])
    assert row["close"] == 10.0   # 缺失因子按 1.0 处理，不抛 Decimal 混算错误


def test_backtest_executes_on_decimal_built_market():
    from quant.engine import backtest as bt
    from quant.engine.rules import FeeConfig
    from quant.strategies.base import EmptyParams, Strategy

    class AlwaysBuy(Strategy):
        Params = EmptyParams

        def generate_signals(self, bars):
            return pd.Series(True, index=bars.index)

    market = loader.build_market(*_decimal_frames(), UniverseFilters(min_list_days=0))
    config = bt.BacktestConfig(
        start="20240101", end="20240103", initial_cash=100_000.0,
        rebalance="daily", top_n=1, min_list_days=0,
        fees=FeeConfig(commission_rate=0.0, min_commission=0.0,
                       stamp_tax_rate=0.0, transfer_fee_rate=0.0,
                       slippage=0.0))
    result = bt.run_backtest(AlwaysBuy(), config, market=market)

    assert not result.trades.empty
    assert (result.equity["cash"] >= 0).all()


def test_resolve_trade_date_caps_at_today(monkeypatch):
    today = datetime.date.today().strftime("%Y%m%d")
    cal = pd.DataFrame({
        "exchange": ["SSE"] * 3,
        "cal_date": ["20200102", "20200103", "20991231"],
        "is_open": [1, 1, 1],
    })
    monkeypatch.setattr(loader.cache, "load_table", lambda *a, **k: cal)

    assert loader.resolve_trade_date() == "20200103"
    assert loader.resolve_trade_date("20200102") == "20200102"

    cal_with_today = pd.DataFrame({
        "exchange": ["SSE"] * 4,
        "cal_date": ["20200102", "20200103", today, "20991231"],
        "is_open": [1, 1, 1, 1],
    })
    monkeypatch.setattr(loader.cache, "load_table", lambda *a, **k: cal_with_today)
    assert loader.resolve_trade_date() == today


def test_load_market_data_ensures_index_daily(monkeypatch):
    ensured = {}

    def fake_ensure_all(tables=None):
        ensured["tables"] = tables

    cal = pd.DataFrame({"exchange": ["SSE"], "cal_date": ["20240101"],
                        "is_open": [1]})
    empty = pd.DataFrame()

    def fake_load(table, **kwargs):
        return cal if table == "trade_cal" else empty

    monkeypatch.setattr(loader.cache, "ensure_all", fake_ensure_all)
    monkeypatch.setattr(loader.cache, "load_table", fake_load)

    result = loader.load_market_data("20240101", "20240102", ensure=True)

    assert "index_daily" in ensured["tables"]
    assert result.empty
