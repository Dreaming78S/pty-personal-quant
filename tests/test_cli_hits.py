from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_hits_update_calls_fill_hits(monkeypatch):
    called = {}

    def fake_fill(strategy="all", from_date=None, to_date=None):
        called.update(strategy=strategy, from_date=from_date, to_date=to_date)
        return {"ma_volume": 3}

    monkeypatch.setattr("quant.engine.hits.fill_hits", fake_fill)

    result = runner.invoke(app, ["hits", "update", "-s", "ma_volume",
                                 "--from-date", "2024-01-01"])

    assert result.exit_code == 0
    assert called == {"strategy": "ma_volume", "from_date": "20240101",
                      "to_date": None}
    assert "hit_ma_volume" in result.output
    assert "3" in result.output
