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


def _patch_select(monkeypatch, captured):
    monkeypatch.setattr("quant.data.cache.ensure_all", lambda tables=None: None)
    monkeypatch.setattr("quant.engine.loader.resolve_trade_date",
                        lambda date=None: "20240103")
    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda **kwargs: pd.DataFrame({"dummy": [1]}))

    def fake_run_selection(strategy, trade_date, top_n=20, market=None,
                           filters=None, rank_by="amount"):
        captured["filters"] = filters
        return pd.DataFrame({"rank": [1], "ts_code": ["600000.SH"],
                             "name": ["浦发银行"]})

    monkeypatch.setattr("quant.engine.selection.run_selection", fake_run_selection)
    monkeypatch.setattr("quant.engine.report.save_selection",
                        lambda *a, **k: "outputs/select/x.csv")


def test_select_defaults_to_main_board(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume"])
    assert result.exit_code == 0
    assert captured["filters"].allowed_boards == ("main",)
    assert captured["filters"].exclude_st is True
    assert captured["filters"].min_list_days == 60


def test_select_boards_all_disables_board_filter(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume", "--boards", "all"])
    assert result.exit_code == 0
    assert captured["filters"].allowed_boards is None


def test_select_boards_accepts_comma_list(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume",
                                 "--boards", "main,gem"])
    assert result.exit_code == 0
    assert captured["filters"].allowed_boards == ("main", "gem")


def test_select_boards_rejects_unknown_token(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume",
                                 "--boards", "mian"])
    assert result.exit_code != 0
    assert "未知板块" in result.output
    assert "mian" in result.output
    assert captured.get("filters") is None


def test_select_boards_rejects_all_mixed_with_names(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume",
                                 "--boards", "all,main"])
    assert result.exit_code != 0
    assert "不能" in result.output
    assert captured.get("filters") is None
