import pandas as pd
import pytest

from quant.engine import metrics


def test_max_drawdown_hand_computed():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0])
    assert metrics.max_drawdown(equity) == pytest.approx(0.25)


def test_annualized_return_doubling_in_one_year():
    equity = pd.Series([100.0] * 253)
    equity.iloc[-1] = 200.0
    assert metrics.annualized_return(equity) == pytest.approx(1.0, abs=1e-6)


def test_sharpe_zero_volatility_returns_zero():
    returns = pd.Series([0.001] * 10)
    assert metrics.sharpe_ratio(returns) == 0.0


def test_trade_stats_win_rate():
    trades = pd.DataFrame([
        {"trade_date": "20240101", "ts_code": "A", "side": "buy",
         "price": 10.0, "shares": 100, "amount": 1000.0, "fee": 0.0},
        {"trade_date": "20240102", "ts_code": "A", "side": "sell",
         "price": 11.0, "shares": 100, "amount": 1100.0, "fee": 0.0},
    ])
    stats = metrics.trade_stats(trades)
    assert stats["胜率"] == pytest.approx(1.0)
    assert stats["交易对数"] == 1


def test_compute_metrics_keys_and_benchmark():
    equity = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104", "20240105"],
        "equity": [100.0, 101.0, 102.0, 103.0],
        "cash": [0.0, 0.0, 0.0, 0.0],
        "benchmark": [100.0, 100.5, 101.0, 101.5],
    })
    result = metrics.compute_metrics(equity)
    for key in ["累计收益", "年化收益", "最大回撤", "夏普比率", "卡玛比率",
                "月胜率", "基准收益", "超额收益", "超额最大回撤"]:
        assert key in result
    assert result["累计收益"] == pytest.approx(0.03)
    assert result["超额收益"] > 0


def test_trade_stats_prorates_buy_fee_across_partial_lots():
    trades = pd.DataFrame([
        {"trade_date": "20240101", "ts_code": "A", "side": "buy",
         "price": 10.0, "shares": 100, "amount": 1000.0, "fee": 5.0},
        {"trade_date": "20240102", "ts_code": "A", "side": "sell",
         "price": 10.0, "shares": 50, "amount": 500.0, "fee": 0.0},
        {"trade_date": "20240103", "ts_code": "A", "side": "sell",
         "price": 10.1, "shares": 50, "amount": 505.0, "fee": 0.0},
    ])
    stats = metrics.trade_stats(trades)
    assert stats["交易对数"] == 2
    assert stats["胜率"] == pytest.approx(0.5)


def test_trade_stats_zero_share_buy_lot_is_skipped():
    trades = pd.DataFrame([
        {"trade_date": "20240101", "ts_code": "A", "side": "buy",
         "price": 10.0, "shares": 0, "amount": 0.0, "fee": 0.0},
        {"trade_date": "20240102", "ts_code": "A", "side": "buy",
         "price": 10.0, "shares": 100, "amount": 1000.0, "fee": 0.0},
        {"trade_date": "20240103", "ts_code": "A", "side": "sell",
         "price": 11.0, "shares": 100, "amount": 1100.0, "fee": 0.0},
    ])
    stats = metrics.trade_stats(trades)
    assert stats["胜率"] == pytest.approx(1.0)
    assert stats["交易对数"] == 1


def test_compute_metrics_with_trades_includes_stats_and_turnover():
    equity = pd.DataFrame({
        "trade_date": ["20240102", "20240103"],
        "equity": [100.0, 110.0],
        "cash": [0.0, 0.0],
    })
    trades = pd.DataFrame([
        {"trade_date": "20240102", "ts_code": "A", "side": "buy",
         "price": 10.0, "shares": 100, "amount": 1000.0, "fee": 0.0},
        {"trade_date": "20240103", "ts_code": "A", "side": "sell",
         "price": 11.0, "shares": 100, "amount": 1100.0, "fee": 0.0},
    ])
    result = metrics.compute_metrics(equity, trades=trades)
    assert result["胜率"] == pytest.approx(1.0)
    assert result["交易对数"] == 1
    assert result["换手率"] == pytest.approx(2100.0 / 105.0)


def test_compute_metrics_without_benchmark_omits_benchmark_keys():
    equity = pd.DataFrame({
        "trade_date": ["20240102", "20240103"],
        "equity": [100.0, 101.0],
        "cash": [0.0, 0.0],
    })
    result = metrics.compute_metrics(equity)
    assert "基准收益" not in result
    assert "超额收益" not in result
    assert "超额最大回撤" not in result


def test_compute_metrics_benchmark_alignment_with_custom_index():
    equity = pd.DataFrame(
        {
            "trade_date": ["20240102", "20240103", "20240104"],
            "equity": [100.0, 105.0, 110.0],
            "cash": [0.0, 0.0, 0.0],
            "benchmark": [100.0, 90.0, 95.0],
        },
        index=[10, 20, 30],
    )
    result = metrics.compute_metrics(equity)
    assert result["基准收益"] == pytest.approx(-0.05)
    assert result["超额收益"] == pytest.approx(1.10 / 0.95 - 1.0)
    assert result["超额最大回撤"] == pytest.approx(1.0 / 133.0)
