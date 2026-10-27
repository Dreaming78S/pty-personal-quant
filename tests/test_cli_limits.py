from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_limit_update_calls_fill_limits(monkeypatch):
    called = {}

    def fake_fill(from_date=None, to_date=None):
        called.update(from_date=from_date, to_date=to_date)
        return {"limit_up_daily": 3, "limit_down_daily": 2}

    monkeypatch.setattr("quant.engine.limits.fill_limits", fake_fill)

    result = runner.invoke(app, ["limit", "update",
                                 "--from-date", "2024-01-02"])

    assert result.exit_code == 0
    assert called == {"from_date": "20240102", "to_date": None}
    assert "limit_up_daily" in result.output
    assert "limit_down_daily" in result.output
    assert "3" in result.output
