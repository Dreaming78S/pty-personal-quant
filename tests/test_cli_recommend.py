from typer.testing import CliRunner

from quant.cli import app
from quant.engine import recommend

runner = CliRunner()


def _result(primary=(), secondary=(), gate_open=True, close=11.0, ma=10.05):
    return recommend.RecommendResult(
        date="20260918", gate_open=gate_open,
        index_close=close, index_ma=ma,
        primary=tuple(primary), secondary=tuple(secondary),
        index_statuses=(
            recommend.IndexStatus(ts_code="000300.SH", name="沪深300",
                                  close=close, ma=ma, above=gate_open),
        ))


def test_recommend_prints_picks_and_gate(monkeypatch):
    primary = (
        recommend.Pick(ts_code="000002.SZ", name="股B", industry="行业B",
                       raw_close=21.34, amount=300000.0, tier="重点",
                       co_count=3,
                       hits=(("RPS突破", 1), ("海龟交易", 3))),
    )
    monkeypatch.setattr(
        "quant.engine.recommend.build_recommendations",
        lambda date, config=None, ignore_gate=True: _result(primary))

    result = runner.invoke(app, ["recommend", "-d", "20260918"])

    assert result.exit_code == 0
    assert "环境闸门" in result.output
    assert "沪深300" in result.output
    assert "重点" in result.output
    assert "RPS突破(1)" in result.output


def test_recommend_handles_stale_data(monkeypatch):
    def fail(date, config=None, ignore_gate=True):
        raise ValueError("index_daily 未更新到 20260918")

    monkeypatch.setattr("quant.engine.recommend.build_recommendations", fail)

    result = runner.invoke(app, ["recommend", "-d", "20260918"])

    assert result.exit_code == 1
    assert "index_daily" in result.output
