from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_help_lists_data_commands():
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "update" in result.output
    assert "status" in result.output


def test_data_init_db_runs_create_all_then_hits_then_migrate(monkeypatch):
    calls = []

    monkeypatch.setattr("quant.data.schemas.create_all",
                        lambda: calls.append("create_all") or ["daily"])
    monkeypatch.setattr("quant.data.schemas.create_hit_tables",
                        lambda: calls.append("create_hit_tables") or ["hit_ma_volume"])
    monkeypatch.setattr("quant.data.schemas.migrate",
                        lambda: calls.append("migrate") or ["daily_basic.limit_status"])
    monkeypatch.setattr("quant.data.schemas.migrate_hit_columns",
                        lambda: calls.append("migrate_hit_columns")
                        or ["hit_ma_volume.industry"])

    result = runner.invoke(app, ["data", "init-db"])

    assert result.exit_code == 0
    assert calls == ["create_all", "create_hit_tables", "migrate",
                     "migrate_hit_columns"]
    assert "daily_basic.limit_status" in result.output
    assert "hit_ma_volume.industry" in result.output


def test_data_init_db_quiet_when_no_migrations(monkeypatch):
    monkeypatch.setattr("quant.data.schemas.create_all", lambda: ["daily"])
    monkeypatch.setattr("quant.data.schemas.create_hit_tables",
                        lambda: ["hit_ma_volume"])
    monkeypatch.setattr("quant.data.schemas.migrate", lambda: [])
    monkeypatch.setattr("quant.data.schemas.migrate_hit_columns", lambda: [])

    result = runner.invoke(app, ["data", "init-db"])

    assert result.exit_code == 0
    assert "已创建/确认 2 张表" in result.output
    assert "已补齐" not in result.output


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


def test_data_status_reports_missing_table(monkeypatch):
    def fake_scalar(sql, params=None):
        if sql.strip() == "SELECT 1":
            return 1
        raise RuntimeError("表不存在")

    monkeypatch.setattr("quant.data.db.scalar", fake_scalar)
    result = runner.invoke(app, ["data", "status"])
    assert result.exit_code == 0
    assert "表不存在" in result.output


def test_data_status_reports_db_down(monkeypatch):
    def fake_scalar(sql, params=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("quant.data.db.scalar", fake_scalar)
    result = runner.invoke(app, ["data", "status"])
    assert result.exit_code == 1
    assert "数据库连接失败" in result.output
