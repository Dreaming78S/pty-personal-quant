import pandas as pd
from typer.testing import CliRunner

from quant.cli import app
from quant.engine.backtest import BacktestResult, BacktestConfig
from quant.engine.rules import FeeConfig

runner = CliRunner()


def test_backtest_command_with_fakes(monkeypatch):
    equity = pd.DataFrame({"trade_date": ["20240102"], "equity": [100.0],
                           "cash": [0.0], "benchmark": [100.0]})

    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda *a, **k: pd.DataFrame({"dummy": [1]}))
    monkeypatch.setattr("quant.engine.loader.load_benchmark",
                        lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr("quant.engine.backtest.run_backtest",
                        lambda *a, **k: BacktestResult(
                            equity=equity, trades=pd.DataFrame(),
                            config=BacktestConfig(start="20240102", end="20240102"),
                            strategy_name="ma_volume"))
    monkeypatch.setattr("quant.engine.report.save_backtest",
                        lambda *a, **k: "outputs/backtest/fake")

    result = runner.invoke(app, ["backtest", "-s", "ma_volume",
                                 "--start", "2024-01-02", "--end", "2024-01-03"])

    assert result.exit_code == 0
    assert "报告目录" in result.output


def test_backtest_command_hints_when_benchmark_cache_missing(monkeypatch):
    equity = pd.DataFrame({"trade_date": ["20240102"], "equity": [100.0],
                           "cash": [0.0], "benchmark": [100.0]})

    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda *a, **k: pd.DataFrame({"dummy": [1]}))

    def missing_benchmark(*args, **kwargs):
        raise FileNotFoundError("缓存缺失：data_cache/index_daily.parquet")

    monkeypatch.setattr("quant.engine.loader.load_benchmark", missing_benchmark)
    monkeypatch.setattr("quant.engine.backtest.run_backtest",
                        lambda *a, **k: BacktestResult(
                            equity=equity, trades=pd.DataFrame(),
                            config=BacktestConfig(start="20240102", end="20240102"),
                            strategy_name="ma_volume"))
    monkeypatch.setattr("quant.engine.report.save_backtest",
                        lambda *a, **k: "outputs/backtest/fake")

    result = runner.invoke(app, ["backtest", "-s", "ma_volume",
                                 "--start", "2024-01-02", "--end", "2024-01-03"])

    assert result.exit_code == 0
    assert "跳过基准对比" in result.output


def test_backtest_command_builds_fee_config_from_yaml(monkeypatch):
    equity = pd.DataFrame({"trade_date": ["20240102"], "equity": [100.0],
                           "cash": [0.0], "benchmark": [100.0]})
    captured = {}

    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda *a, **k: pd.DataFrame({"dummy": [1]}))
    monkeypatch.setattr("quant.engine.loader.load_benchmark",
                        lambda *a, **k: pd.Series(dtype=float))

    def fake_run(strategy, config, **kwargs):
        captured["config"] = config
        return BacktestResult(equity=equity, trades=pd.DataFrame(),
                              config=config, strategy_name="ma_volume")

    monkeypatch.setattr("quant.engine.backtest.run_backtest", fake_run)
    monkeypatch.setattr("quant.engine.report.save_backtest",
                        lambda *a, **k: "outputs/backtest/fake")

    result = runner.invoke(app, ["backtest", "-s", "ma_volume",
                                 "--start", "2024-01-02", "--end", "2024-01-03"])

    assert result.exit_code == 0
    assert isinstance(captured["config"].fees, FeeConfig)
