from types import SimpleNamespace

import pandas as pd
import pytest

from quant.bot import runner


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    settings = SimpleNamespace(
        aliyun_rds_host="db.example",
        aliyun_rds_port=3306,
        aliyun_rds_user="user",
        aliyun_rds_passport="secret",
        aliyun_rds_database="quant",
    )
    monkeypatch.setattr(runner, "get_settings", lambda: settings)
    return settings


class FakeCursor:
    def __init__(self, rows):
        self.statements = []
        self._rows = rows

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)
        self.rolled_back = False
        self.closed = False
        self.connect_kwargs = {}

    def cursor(self, *args, **kwargs):
        return self.cursor_obj

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_run_readonly_uses_read_only_transaction(monkeypatch):
    fake = FakeConnection([{"ts_code": "600360.SH"}])
    monkeypatch.setattr(
        runner.pymysql, "connect",
        lambda **kwargs: (fake.connect_kwargs.update(kwargs), fake)[1])

    frame = runner.run_readonly("SELECT ts_code FROM daily LIMIT 1")

    assert fake.cursor_obj.statements == [
        "START TRANSACTION READ ONLY", "SELECT ts_code FROM daily LIMIT 1"]
    assert list(frame.columns) == ["ts_code"]
    assert fake.connect_kwargs["read_timeout"] == 15
    assert fake.connect_kwargs["connect_timeout"] == 15
    assert fake.rolled_back is True
    assert fake.closed is True


def test_run_readonly_returns_empty_frame(monkeypatch):
    fake = FakeConnection([])
    monkeypatch.setattr(runner.pymysql, "connect", lambda **kwargs: fake)

    frame = runner.run_readonly("SELECT ts_code FROM daily LIMIT 1")

    assert frame.empty


def test_run_readonly_closes_connection_on_error(monkeypatch):
    class BoomCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith("SELECT"):
                raise RuntimeError("boom")

    fake = FakeConnection([])
    fake.cursor_obj = BoomCursor([])
    monkeypatch.setattr(runner.pymysql, "connect", lambda **kwargs: fake)

    with pytest.raises(RuntimeError, match="boom"):
        runner.run_readonly("SELECT 1")

    assert fake.closed is True


def test_run_readonly_closes_connection_when_rollback_fails(monkeypatch):
    fake = FakeConnection([{"ts_code": "600360.SH"}])

    def boom():
        raise RuntimeError("rollback boom")

    fake.rollback = boom
    monkeypatch.setattr(runner.pymysql, "connect", lambda **kwargs: fake)

    frame = runner.run_readonly("SELECT ts_code FROM daily LIMIT 1")

    assert list(frame.columns) == ["ts_code"]
    assert fake.closed is True


def test_run_readonly_keeps_query_error_when_rollback_fails(monkeypatch):
    class BoomCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith("SELECT"):
                raise RuntimeError("query boom")

    fake = FakeConnection([])
    fake.cursor_obj = BoomCursor([])

    def boom():
        raise RuntimeError("rollback boom")

    fake.rollback = boom
    monkeypatch.setattr(runner.pymysql, "connect", lambda **kwargs: fake)

    with pytest.raises(RuntimeError, match="query boom"):
        runner.run_readonly("SELECT 1")

    assert fake.closed is True
