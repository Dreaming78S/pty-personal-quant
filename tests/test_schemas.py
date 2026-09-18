import pandas as pd
import pytest

from quant.data import schemas


def test_all_expected_tables_exist():
    expected = {
        "stock_basic", "trade_cal", "daily", "adj_factor", "daily_basic",
        "suspend_d", "stk_limit", "index_daily", "namechange", "ingest_log",
        "stock_company", "new_share", "stk_holdertrade",
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


def test_new_reference_tables_use_no_date_column():
    for table in ("stock_company", "new_share", "stk_holdertrade"):
        assert schemas.date_column(table) is None


def test_new_table_primary_keys():
    assert "PRIMARY KEY (ts_code)" in schemas.TABLES["stock_company"].ddl
    assert "PRIMARY KEY (ts_code)" in schemas.TABLES["new_share"].ddl
    holder_ddl = schemas.TABLES["stk_holdertrade"].ddl
    assert ("PRIMARY KEY (ts_code, ann_date, holder_name, in_de, change_vol)"
            in holder_ddl)
    assert "KEY idx_ann_date (ann_date)" in holder_ddl


def test_daily_basic_limit_status_column_after_circ_mv():
    cols = schemas.columns_of("daily_basic")
    assert cols[cols.index("circ_mv") + 1] == "limit_status"
    ddl = schemas.TABLES["daily_basic"].ddl
    assert ddl.index("circ_mv") < ddl.index("limit_status") < ddl.index("updated_at")
    assert "limit_status INT NULL" in ddl


def test_stk_holdertrade_holder_name_widened_to_255():
    spec = schemas.TABLES["stk_holdertrade"]
    assert "holder_name VARCHAR(255) NOT NULL COMMENT '股东名称'" in spec.ddl
    assert spec.columns == (
        "ts_code", "ann_date", "holder_name", "holder_type", "in_de",
        "change_vol", "change_ratio", "after_share", "after_ratio",
        "avg_price", "total_share", "begin_date", "close_date",
    )
    assert ("PRIMARY KEY (ts_code, ann_date, holder_name, in_de, change_vol)"
            in spec.ddl)


def _read_df_with_holder_name_length(length, column_absent=False):
    def read_df(sql, params=None):
        if "CHARACTER_MAXIMUM_LENGTH" in sql:
            assert "TABLE_SCHEMA = DATABASE()" in sql
            assert params == ("stk_holdertrade", "holder_name")
            if column_absent:
                return pd.DataFrame()
            return pd.DataFrame({"CHARACTER_MAXIMUM_LENGTH": [length]})
        return pd.DataFrame({"COLUMN_NAME": ["limit_status"]})
    return read_df


def test_migrate_widens_narrow_holder_name(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_with_holder_name_length(128))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == ["stk_holdertrade.holder_name"]
    assert len(executed) == 1
    assert "MODIFY COLUMN `holder_name` VARCHAR(255) NOT NULL" in executed[0]


def test_migrate_skips_widening_when_already_255(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_with_holder_name_length(255))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_skips_widening_when_length_is_null(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_with_holder_name_length(None))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_skips_widening_when_column_absent(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_with_holder_name_length(None,
                                                         column_absent=True))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_skips_existing_column(monkeypatch):
    executed = []
    monkeypatch.setattr(
        schemas.db, "read_df",
        lambda sql, params=None: pd.DataFrame({"COLUMN_NAME": ["limit_status"]}))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_adds_missing_column(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        lambda sql, params=None: pd.DataFrame())
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == ["daily_basic.limit_status"]
    assert len(executed) == 1
    assert "ADD COLUMN `limit_status`" in executed[0]
    assert "AFTER `circ_mv`" in executed[0]
