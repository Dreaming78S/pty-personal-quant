import pandas as pd
import pytest
from pydantic import BaseModel

from quant.engine import backtest, rules
from quant.engine.backtest import BacktestConfig
from quant.strategies.base import EmptyParams, Strategy

ZERO_FEES = rules.FeeConfig(commission_rate=0.0, min_commission=0.0,
                            stamp_tax_rate=0.0, transfer_fee_rate=0.0,
                            slippage=0.0)


class AlwaysStrategy(Strategy):
    Params = EmptyParams

    def generate_signals(self, bars):
        return pd.Series(True, index=bars.index)


class FirstDayStrategy(Strategy):
    Params = EmptyParams

    def generate_signals(self, bars):
        return bars["trade_date"] == bars.iloc[0]["trade_date"]


class SingleCodeStrategy(Strategy):
    Params = EmptyParams

    def generate_signals(self, bars):
        return bars["ts_code"] == "000001.SZ"


def make_market(codes=("600000.SH",), dates=("20240101", "20240102", "20240103",
                                              "20240104", "20240105", "20240108"),
                price=10.0, up_limit=11.0, down_limit=9.0,
                is_st=False, is_new=False):
    rows = []
    for code in codes:
        for d in dates:
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": price, "high": price, "low": price, "close": price,
                "raw_open": price, "raw_close": price,
                "vol": 100.0, "amount": 10000.0, "adj_factor": 1.0,
                "turnover_rate": 1.0, "volume_ratio": 1.0, "pe_ttm": 10.0,
                "pb": 1.0, "total_mv": 100.0, "circ_mv": 80.0,
                "up_limit": up_limit, "down_limit": down_limit,
                "suspended": False, "is_st": is_st, "is_new": is_new,
                "board": "main",
            })
    return pd.DataFrame(rows)


def base_config(**overrides):
    cfg = dict(start="20240101", end="20240108", initial_cash=100_000.0,
               rebalance="daily", top_n=1, fees=ZERO_FEES)
    cfg.update(overrides)
    return BacktestConfig(**cfg)


def test_rebalance_dates_weekly_and_monthly():
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240201", "20240202"]
    assert backtest.rebalance_dates(dates, "weekly") == ["20240101", "20240108", "20240201"]
    assert backtest.rebalance_dates(dates, "monthly") == ["20240101", "20240201"]
    assert backtest.rebalance_dates(dates, "daily") == dates


def test_buy_at_next_open_with_correct_shares():
    market = make_market()
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)

    first_trade = result.trades.iloc[0]
    assert first_trade["trade_date"] == "20240102"
    assert first_trade["side"] == "buy"
    assert first_trade["shares"] == 10000
    assert result.trades["trade_date"].min() != "20240101"

    assert result.equity.iloc[0]["equity"] == pytest.approx(100_000.0)
    assert result.equity.iloc[-1]["equity"] == pytest.approx(100_000.0)


def test_limit_up_blocks_buy_until_tradable():
    market = make_market()
    market.loc[market["trade_date"] == "20240102", "up_limit"] = 10.0  # 次日开盘涨停
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)
    assert result.trades.iloc[0]["trade_date"] == "20240103"


def test_limit_down_postpones_sell():
    market = make_market()
    market.loc[market["trade_date"] == "20240103", "down_limit"] = 10.0  # 卖出日跌停
    result = backtest.run_backtest(FirstDayStrategy(), base_config(), market=market)
    sells = result.trades[result.trades["side"] == "sell"]
    assert len(sells) == 1
    assert sells.iloc[0]["trade_date"] == "20240104"


def test_universe_excludes_st_and_new():
    market = make_market(is_st=True)
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)
    assert result.trades.empty

    market2 = make_market(is_new=True)
    result2 = backtest.run_backtest(
        AlwaysStrategy(), base_config(min_list_days=60), market=market2)
    assert result2.trades.empty


def test_execution_next_close_buys_at_execution_close():
    market = make_market(up_limit=13.0, down_limit=8.0)
    market["close"] = 12.0
    market["raw_close"] = 12.0
    result = backtest.run_backtest(
        AlwaysStrategy(), base_config(execution="next_close"), market=market)
    first_trade = result.trades.iloc[0]
    assert first_trade["trade_date"] == "20240102"
    assert first_trade["price"] == pytest.approx(12.0)
    assert first_trade["shares"] == 8300


def test_next_close_limit_check_uses_raw_close():
    market = make_market(up_limit=13.0, down_limit=8.0)
    market["close"] = 12.0
    market["raw_close"] = 12.0
    market.loc[market["trade_date"] == "20240102", "up_limit"] = 11.0  # 开盘 10 未涨停、收盘 12 涨停
    result = backtest.run_backtest(
        AlwaysStrategy(), base_config(execution="next_close"), market=market)
    assert result.trades.iloc[0]["trade_date"] == "20240103"


def test_unknown_execution_rejected():
    market = make_market()
    with pytest.raises(ValueError, match="不支持的执行方式"):
        backtest.run_backtest(AlwaysStrategy(),
                              base_config(execution="same_close"),
                              market=market)


def test_sell_retried_on_non_rebalance_date():
    market = make_market(dates=("20240101", "20240102", "20240103", "20240104",
                                "20240105", "20240108", "20240109", "20240110"))
    market.loc[market["trade_date"] == "20240109", "down_limit"] = 10.0
    result = backtest.run_backtest(
        FirstDayStrategy(), base_config(rebalance="weekly", end="20240110"),
        market=market)
    sells = result.trades[result.trades["side"] == "sell"]
    assert len(sells) == 1
    assert sells.iloc[0]["trade_date"] == "20240110"
    assert "20240110" not in backtest.rebalance_dates(
        market["trade_date"].unique().tolist(), "weekly")


def test_benchmark_normalization_and_absence():
    market = make_market()
    bench = pd.Series([3000.0, 3300.0], index=["20240101", "20240102"])
    result = backtest.run_backtest(AlwaysStrategy(), base_config(),
                                   market=market, benchmark=bench)
    bench_col = result.equity["benchmark"]
    assert bench_col.iloc[0] == pytest.approx(100_000.0)
    assert bench_col.iloc[1] == pytest.approx(110_000.0)
    assert bench_col.iloc[-1] == pytest.approx(110_000.0)

    absent = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)
    assert absent.equity["benchmark"].isna().all()

    no_overlap = pd.Series([1000.0, 1010.0], index=["20230101", "20230102"])
    stale = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market,
                                  benchmark=no_overlap)
    assert stale.equity["benchmark"].isna().all()


def test_cash_never_negative_when_min_commission_binds():
    market = make_market()
    result = backtest.run_backtest(
        AlwaysStrategy(), base_config(initial_cash=1005.0, fees=rules.FeeConfig()),
        market=market)
    assert (result.equity["cash"] >= 0).all()


def test_cash_floor_keeps_affordable_trade():
    market = make_market()
    result = backtest.run_backtest(
        AlwaysStrategy(), base_config(initial_cash=1006.5, fees=rules.FeeConfig()),
        market=market)
    assert not result.trades.empty
    assert result.trades.iloc[0]["side"] == "buy"
    assert result.trades.iloc[0]["shares"] == 100
    assert (result.equity["cash"] >= 0).all()


def test_delisted_position_force_settled_at_last_close():
    dates = ("20240101", "20240102", "20240103", "20240104", "20240105")
    market = make_market(codes=("600000.SH", "000001.SZ"), dates=dates)
    market = market[~((market["ts_code"] == "000001.SZ") &
                      (market["trade_date"] > "20240103"))].reset_index(drop=True)
    market.loc[(market["ts_code"] == "000001.SZ") &
               (market["trade_date"] == "20240103"), "close"] = 12.0

    result = backtest.run_backtest(
        SingleCodeStrategy(), base_config(end="20240105"), market=market)

    assert result.trades["side"].tolist() == ["buy", "sell"]
    sell = result.trades.iloc[-1]
    assert sell["trade_date"] == "20240104"
    assert sell["price"] == pytest.approx(12.0)   # 最后一根K线的收盘价（ffill）
    assert sell["shares"] == 10000
    final = result.equity.iloc[-1]
    assert final["equity"] == pytest.approx(final["cash"])  # 不再持有退市股


def test_equity_excludes_warmup_dates():
    market = make_market(dates=("20231229", "20240101", "20240102", "20240103",
                                "20240104", "20240105", "20240108"))
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)
    assert result.equity["trade_date"].min() == "20240101"
    assert result.equity["trade_date"].tolist() == [
        "20240101", "20240102", "20240103", "20240104", "20240105", "20240108"]
    assert result.trades["trade_date"].min() >= "20240101"
