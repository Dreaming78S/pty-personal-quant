from pathlib import Path

import pandas as pd

from quant.engine import report
from quant.engine.backtest import BacktestConfig, BacktestResult
from quant.engine.rules import FeeConfig


def _result():
    equity = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104"],
        "equity": [100.0, 101.0, 102.0],
        "cash": [0.0, 0.0, 0.0],
        "benchmark": [100.0, 100.5, 101.0],
    })
    trades = pd.DataFrame({
        "trade_date": ["20240102"], "ts_code": ["600000.SH"], "side": ["buy"],
        "price": [10.0], "shares": [100], "amount": [1000.0], "fee": [5.0],
    })
    config = BacktestConfig(start="20240102", end="20240104", fees=FeeConfig())
    return BacktestResult(equity=equity, trades=trades, config=config,
                          strategy_name="ma_volume")


def test_save_backtest_writes_all_artifacts(tmp_path):
    folder = report.save_backtest(_result(), {"累计收益": 0.02}, out_root=tmp_path)

    assert (folder / "equity.csv").exists()
    assert (folder / "trades.csv").exists()
    assert (folder / "metrics.json").exists()
    assert (folder / "equity.png").exists()
    assert (folder / "equity.png").stat().st_size > 0
    assert folder.name.startswith("ma_volume_")
