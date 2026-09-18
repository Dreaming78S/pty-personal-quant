import pytest

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


ALL_TABLE_NAMES = list(schemas.TABLES)
PER_DATE_TABLE_NAMES = [name for name, spec in schemas.TABLES.items()
                        if spec.date_column == "trade_date"]
DECIMAL_TABLE_NAMES = ["daily", "adj_factor", "daily_basic", "stk_limit",
                       "index_daily"]


@pytest.mark.parametrize("table", ALL_TABLE_NAMES)
def test_table_engine_and_charset(table):
    ddl = schemas.TABLES[table].ddl
    assert "ENGINE=InnoDB" in ddl
    assert "DEFAULT CHARSET=utf8mb4" in ddl


@pytest.mark.parametrize("table", ALL_TABLE_NAMES)
def test_table_has_primary_key(table):
    assert "PRIMARY KEY (" in schemas.TABLES[table].ddl


@pytest.mark.parametrize("table", PER_DATE_TABLE_NAMES)
def test_per_date_table_indexes_trade_date(table):
    assert "KEY idx_trade_date (trade_date)" in schemas.TABLES[table].ddl


@pytest.mark.parametrize("table", DECIMAL_TABLE_NAMES)
def test_numeric_market_columns_use_decimal(table):
    assert "DECIMAL" in schemas.TABLES[table].ddl


@pytest.mark.parametrize("table", ["daily", "index_daily"])
def test_change_column_is_backticked(table):
    assert "`change`" in schemas.TABLES[table].ddl
