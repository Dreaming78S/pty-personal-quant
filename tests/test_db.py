from types import SimpleNamespace

import pandas as pd

from quant.data import db


def test_get_connection_sets_timeouts(monkeypatch):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)
    monkeypatch.setattr(db, "get_settings",
                        lambda: SimpleNamespace(aliyun_rds_host="db.example.com",
                                                aliyun_rds_port=3306,
                                                aliyun_rds_user="user",
                                                aliyun_rds_passport="pw",
                                                aliyun_rds_database="quant"))

    db.get_connection()

    assert captured["connect_timeout"] == db.CONNECT_TIMEOUT_SECONDS
    assert captured["read_timeout"] == db.READ_TIMEOUT_SECONDS
    assert captured["write_timeout"] == db.WRITE_TIMEOUT_SECONDS
    assert captured["host"] == "db.example.com"


def test_build_upsert_sql():
    sql = db.build_upsert_sql("daily", ["ts_code", "trade_date", "close"])
    assert sql == (
        "INSERT INTO `daily` (`ts_code`, `trade_date`, `close`) "
        "VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE `ts_code`=VALUES(`ts_code`), "
        "`trade_date`=VALUES(`trade_date`), `close`=VALUES(`close`)"
    )


def test_upsert_df_converts_nan_to_none(monkeypatch):
    captured = {}

    def fake_upsert_rows(table, columns, rows):
        captured["table"] = table
        captured["columns"] = columns
        captured["rows"] = list(rows)
        return len(captured["rows"])

    monkeypatch.setattr(db, "upsert_rows", fake_upsert_rows)
    df = pd.DataFrame({"ts_code": ["000001.SZ", "600000.SH"],
                       "trade_date": ["20240102", "20240102"],
                       "close": [10.5, float("nan")]})

    n = db.upsert_df("daily", df)

    assert n == 2
    assert captured["columns"] == ["ts_code", "trade_date", "close"]
    assert captured["rows"][1][2] is None


def test_upsert_df_respects_column_order(monkeypatch):
    captured = {}
    monkeypatch.setattr(db, "upsert_rows",
                        lambda table, columns, rows: captured.update(columns=columns) or 1)
    df = pd.DataFrame({"b": [1], "a": [2]})
    db.upsert_df("t", df, columns=["a", "b"])
    assert captured["columns"] == ["a", "b"]
