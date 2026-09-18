from quant.data import schemas


def test_all_expected_tables_exist():
    expected = {
        "stock_basic", "trade_cal", "daily", "adj_factor", "daily_basic",
        "suspend_d", "stk_limit", "index_daily", "namechange", "ingest_log",
    }
    assert set(schemas.TABLES) == expected


def test_daily_primary_key_and_index():
    ddl = schemas.TABLES["daily"].ddl
    assert "PRIMARY KEY (ts_code, trade_date)" in ddl
    assert "KEY idx_trade_date (trade_date)" in ddl


def test_columns_of_order():
    cols = schemas.columns_of("daily")
    assert cols[:4] == ["ts_code", "trade_date", "open", "high"]
    assert "amount" in cols


def test_date_column_flags():
    assert schemas.date_column("daily") == "trade_date"
    assert schemas.date_column("stock_basic") is None


def test_create_all_calls_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(schemas.db, "execute", lambda sql, params=None: calls.append(sql))
    names = schemas.create_all()
    assert set(names) == set(schemas.TABLES)
    assert len(calls) == len(schemas.TABLES)
    assert all("CREATE TABLE IF NOT EXISTS" in sql for sql in calls)
