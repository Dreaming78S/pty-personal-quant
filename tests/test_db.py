import pandas as pd

from quant.data import db


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
