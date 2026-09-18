import pandas as pd
from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_select_command_end_to_end_with_fakes(monkeypatch):
    monkeypatch.setattr("quant.data.cache.ensure_all", lambda tables=None: None)
    monkeypatch.setattr("quant.engine.loader.resolve_trade_date", lambda date=None: "20240103")
    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda **kwargs: pd.DataFrame({"dummy": [1]}))
    monkeypatch.setattr("quant.engine.selection.run_selection",
                        lambda *a, **k: pd.DataFrame({
                            "rank": [1], "ts_code": ["600000.SH"], "name": ["浦发银行"]}))
    monkeypatch.setattr("quant.engine.report.save_selection",
                        lambda *a, **k: "outputs/select/20240103_ma_volume.csv")

    result = runner.invoke(app, ["select", "-s", "ma_volume"])

    assert result.exit_code == 0
    assert "浦发银行" in result.output
    assert "已保存" in result.output
