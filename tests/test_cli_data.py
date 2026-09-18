from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_help_lists_data_commands():
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "update" in result.output
    assert "status" in result.output


def test_data_update_calls_ingest(monkeypatch):
    called = {}

    def fake_update_all(from_date=None, to_date=None, client=None):
        called["from_date"] = from_date
        called["to_date"] = to_date
        return {"daily": 10}

    monkeypatch.setattr("quant.data.ingest.update_all", fake_update_all)
    result = runner.invoke(app, ["data", "update", "--from-date", "2024-01-02"])
    assert result.exit_code == 0
    assert called["from_date"] == "20240102"
    assert "daily" in result.output


def test_data_status_prints_table_rows(monkeypatch):
    monkeypatch.setattr("quant.data.db.scalar",
                        lambda sql, params=None: 123)
    result = runner.invoke(app, ["data", "status"])
    assert result.exit_code == 0
    assert "daily" in result.output
