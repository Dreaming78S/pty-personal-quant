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


def test_suspend_timing_widened_to_255():
    assert ("suspend_timing VARCHAR(255) NOT NULL DEFAULT '' "
            "COMMENT '日内停牌时段'"
            in schemas.TABLES["suspend_d"].ddl)


def test_suspend_d_uses_composite_primary_key():
    ddl = schemas.TABLES["suspend_d"].ddl
    assert ("PRIMARY KEY (ts_code, trade_date, suspend_type, suspend_timing)"
            in ddl)
    assert ("suspend_type VARCHAR(8) NOT NULL DEFAULT '' COMMENT 'S停牌 R复牌'"
            in ddl)
    assert "KEY idx_trade_date (trade_date)" in ddl


SUSPEND_PK_CHECK = (
    "SELECT COUNT(*) FROM information_schema.STATISTICS "
    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='suspend_d' "
    "AND INDEX_NAME='PRIMARY' AND COLUMN_NAME='suspend_timing'"
)

SUSPEND_PK_STATEMENTS = [
    "UPDATE `suspend_d` SET `suspend_type`='' WHERE `suspend_type` IS NULL",
    "UPDATE `suspend_d` SET `suspend_timing`='' WHERE `suspend_timing` IS NULL",
    "ALTER TABLE `suspend_d` MODIFY COLUMN `suspend_type` VARCHAR(8) "
    "NOT NULL DEFAULT '' COMMENT 'S停牌 R复牌'",
    "ALTER TABLE `suspend_d` MODIFY COLUMN `suspend_timing` VARCHAR(255) "
    "NOT NULL DEFAULT '' COMMENT '日内停牌时段'",
    "ALTER TABLE `suspend_d` DROP PRIMARY KEY, ADD PRIMARY KEY "
    "(`ts_code`,`trade_date`,`suspend_type`,`suspend_timing`)",
]


def test_conditional_migrations_define_suspend_pk():
    assert schemas.CONDITIONAL_MIGRATIONS == [
        ("suspend_d_pk", SUSPEND_PK_CHECK, SUSPEND_PK_STATEMENTS),
    ]


def _read_df_for_suspend_pk(count, table_exists=True):
    def read_df(sql, params=None):
        if "information_schema.TABLES" in sql:
            if table_exists:
                return pd.DataFrame({"TABLE_NAME": [params[0]]})
            return pd.DataFrame()
        if "information_schema.STATISTICS" in sql:
            return pd.DataFrame({"COUNT(*)": [count]})
        if "CHARACTER_MAXIMUM_LENGTH" in sql:
            return pd.DataFrame({"CHARACTER_MAXIMUM_LENGTH": [255]})
        return pd.DataFrame({"COLUMN_NAME": ["limit_status"]})
    return read_df


def test_migrate_applies_suspend_pk_in_order(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df", _read_df_for_suspend_pk(0))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == ["suspend_d.pk"]
    assert executed == SUSPEND_PK_STATEMENTS


def test_migrate_skips_suspend_pk_when_already_applied(monkeypatch):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df", _read_df_for_suspend_pk(1))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_skips_suspend_pk_when_table_absent(monkeypatch):
    executed = []
    queried = []

    def read_df(sql, params=None):
        queried.append(sql)
        if "information_schema.TABLES" in sql:
            return pd.DataFrame()
        return pd.DataFrame({"COUNT(*)": [0]})

    monkeypatch.setattr(schemas.db, "read_df", read_df)
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []
    assert all("information_schema.TABLES" in sql for sql in queried)


def test_namechange_change_reason_widened_to_255():
    assert ("change_reason VARCHAR(255) COMMENT '变更原因'"
            in schemas.TABLES["namechange"].ddl)


WIDENING_CASES = [
    pytest.param(
        "stk_holdertrade", "holder_name", 255,
        "ALTER TABLE `stk_holdertrade` MODIFY COLUMN `holder_name` "
        "VARCHAR(255) NOT NULL COMMENT '股东名称'",
        id="holder_name",
    ),
    pytest.param(
        "suspend_d", "suspend_timing", 255,
        "ALTER TABLE `suspend_d` MODIFY COLUMN `suspend_timing` "
        "VARCHAR(255) COMMENT '日内停牌时段'",
        id="suspend_timing",
    ),
    pytest.param(
        "namechange", "change_reason", 255,
        "ALTER TABLE `namechange` MODIFY COLUMN `change_reason` "
        "VARCHAR(255) COMMENT '变更原因'",
        id="change_reason",
    ),
]


def test_widenings_cover_all_expected_columns():
    assert ([(table, column, min_length)
             for table, column, min_length, _ in schemas.WIDENINGS]
            == [("stk_holdertrade", "holder_name", 255),
                ("suspend_d", "suspend_timing", 255),
                ("namechange", "change_reason", 255)])


def _read_df_for_widening(table, column, length, column_absent=False):
    def read_df(sql, params=None):
        if "information_schema.STATISTICS" in sql:
            return pd.DataFrame({"COUNT(*)": [1]})
        if "CHARACTER_MAXIMUM_LENGTH" in sql:
            assert "TABLE_SCHEMA = DATABASE()" in sql
            if params == (table, column):
                if column_absent:
                    return pd.DataFrame()
                return pd.DataFrame({"CHARACTER_MAXIMUM_LENGTH": [length]})
            return pd.DataFrame({"CHARACTER_MAXIMUM_LENGTH": [255]})
        return pd.DataFrame({"COLUMN_NAME": ["limit_status"]})
    return read_df


@pytest.mark.parametrize("table,column,min_length,expected_sql", WIDENING_CASES)
def test_migrate_widens_narrow_column(monkeypatch, table, column, min_length,
                                     expected_sql):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_for_widening(table, column, min_length - 1))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == [f"{table}.{column}"]
    assert executed == [expected_sql]


@pytest.mark.parametrize("table,column,min_length,expected_sql", WIDENING_CASES)
def test_migrate_skips_widening_when_already_widest(monkeypatch, table, column,
                                                    min_length, expected_sql):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_for_widening(table, column, min_length))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


@pytest.mark.parametrize("table,column,min_length,expected_sql", WIDENING_CASES)
def test_migrate_skips_widening_when_length_is_null(monkeypatch, table, column,
                                                    min_length, expected_sql):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_for_widening(table, column, None))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


@pytest.mark.parametrize("table,column,min_length,expected_sql", WIDENING_CASES)
def test_migrate_skips_widening_when_column_absent(monkeypatch, table, column,
                                                   min_length, expected_sql):
    executed = []
    monkeypatch.setattr(schemas.db, "read_df",
                        _read_df_for_widening(table, column, None,
                                              column_absent=True))
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_skips_tables_that_do_not_exist(monkeypatch):
    executed = []
    queried = []

    def read_df(sql, params=None):
        queried.append(sql)
        return pd.DataFrame()

    monkeypatch.setattr(schemas.db, "read_df", read_df)
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []
    assert all("information_schema.TABLES" in sql for sql in queried)


def _read_df_with_suspend_pk_applied(sql, params=None):
    if "information_schema.STATISTICS" in sql:
        return pd.DataFrame({"COUNT(*)": [1]})
    return None


def test_migrate_skips_existing_column(monkeypatch):
    executed = []

    def read_df(sql, params=None):
        applied = _read_df_with_suspend_pk_applied(sql, params)
        if applied is not None:
            return applied
        return pd.DataFrame({"COLUMN_NAME": ["limit_status"]})

    monkeypatch.setattr(schemas.db, "read_df", read_df)
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == []
    assert executed == []


def test_migrate_adds_missing_column(monkeypatch):
    executed = []

    def read_df(sql, params=None):
        if "information_schema.TABLES" in sql:
            return pd.DataFrame({"TABLE_NAME": [params[0]]})
        applied = _read_df_with_suspend_pk_applied(sql, params)
        if applied is not None:
            return applied
        return pd.DataFrame()

    monkeypatch.setattr(schemas.db, "read_df", read_df)
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == ["daily_basic.limit_status"]
    assert len(executed) == 1
    assert "ADD COLUMN `limit_status`" in executed[0]
    assert "AFTER `circ_mv`" in executed[0]


def test_hit_tables_cover_six_strategies():
    assert schemas.HIT_STRATEGIES == ("ma_volume", "turtle_trade",
                                      "high_tight_flag", "limit_up_shakeout",
                                      "uptrend_limit_down", "rps_breakout")
    assert set(schemas.HIT_TABLES) == {
        "hit_ma_volume", "hit_turtle_trade", "hit_high_tight_flag",
        "hit_limit_up_shakeout", "hit_uptrend_limit_down", "hit_rps_breakout",
    }


def test_hit_tables_not_in_source_tables():
    assert set(schemas.TABLES) & set(schemas.HIT_TABLES) == set()


def test_hit_table_columns_and_ddl_shape():
    spec = schemas.HIT_TABLES["hit_ma_volume"]
    assert spec.columns == schemas.HIT_COLUMNS
    assert schemas.hit_table_name("ma_volume") == "hit_ma_volume"
    assert "PRIMARY KEY (ts_code, trade_date)" in spec.ddl
    assert "`rank` INT" in spec.ddl
    assert "KEY idx_trade_date (trade_date)" in spec.ddl
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" in spec.ddl
    assert "历史命中" in spec.ddl


def test_create_hit_tables_executes_all_ddls(monkeypatch):
    calls = []
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: calls.append(sql))
    names = schemas.create_hit_tables()
    assert set(names) == set(schemas.HIT_TABLES)
    assert len(calls) == len(schemas.HIT_TABLES)
    assert all("CREATE TABLE IF NOT EXISTS" in sql for sql in calls)


def test_migrate_applies_add_then_widen_in_order(monkeypatch):
    executed = []

    def read_df(sql, params=None):
        if "information_schema.TABLES" in sql:
            return pd.DataFrame({"TABLE_NAME": [params[0]]})
        if "information_schema.STATISTICS" in sql:
            return pd.DataFrame({"COUNT(*)": [1]})
        if "CHARACTER_MAXIMUM_LENGTH" in sql:
            if params == ("stk_holdertrade", "holder_name"):
                return pd.DataFrame({"CHARACTER_MAXIMUM_LENGTH": [128]})
            return pd.DataFrame({"CHARACTER_MAXIMUM_LENGTH": [255]})
        return pd.DataFrame()

    monkeypatch.setattr(schemas.db, "read_df", read_df)
    monkeypatch.setattr(schemas.db, "execute",
                        lambda sql, params=None: executed.append(sql))

    assert schemas.migrate() == ["daily_basic.limit_status",
                                 "stk_holdertrade.holder_name"]
    assert len(executed) == 2
    assert "ADD COLUMN `limit_status`" in executed[0]
    assert "MODIFY COLUMN `holder_name` VARCHAR(255) NOT NULL" in executed[1]
