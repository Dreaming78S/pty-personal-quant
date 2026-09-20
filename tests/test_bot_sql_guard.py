import pytest

from quant.bot.sql_guard import SqlRejected, validate_sql

ALLOWED = {"daily", "stock_basic", "hit_ma_volume"}


def test_validate_appends_limit():
    assert validate_sql("SELECT ts_code FROM daily",
                        allowed=ALLOWED) == "SELECT ts_code FROM daily LIMIT 200"


def test_validate_keeps_smaller_limit():
    sql = validate_sql("SELECT ts_code FROM daily LIMIT 10", allowed=ALLOWED)

    assert sql.endswith("LIMIT 10")


def test_validate_tightens_larger_limit():
    sql = validate_sql("SELECT ts_code FROM daily LIMIT 5000", allowed=ALLOWED)

    assert sql.endswith("LIMIT 200")


def test_validate_accepts_with_cte_and_join():
    sql = ("WITH t AS (SELECT ts_code FROM hit_ma_volume) "
           "SELECT b.name FROM t JOIN stock_basic b ON b.ts_code = t.ts_code")

    result = validate_sql(sql, allowed=ALLOWED)

    assert result.endswith("LIMIT 200")


def test_validate_strips_comments_and_trailing_semicolon():
    assert validate_sql("SELECT 1 FROM daily -- 说明\n",
                        allowed=ALLOWED).endswith("LIMIT 200")
    assert validate_sql("SELECT 1 FROM daily;",
                        allowed=ALLOWED).endswith("LIMIT 200")


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM daily; DROP TABLE daily",
    "UPDATE daily SET close = 1",
    "DELETE FROM daily",
    "DROP TABLE daily",
    "CREATE TABLE x (a INT)",
    "INSERT INTO daily (ts_code) VALUES ('x')",
    "SELECT * FROM daily INTO OUTFILE '/tmp/x'",
    "SELECT SLEEP(10)",
    "SELECT * FROM information_schema.tables",
    "",
])
def test_validate_rejects_dangerous_sql(sql):
    with pytest.raises(SqlRejected):
        validate_sql(sql, allowed=ALLOWED)


def test_validate_rejects_unknown_table():
    with pytest.raises(SqlRejected, match="不允许查询的表"):
        validate_sql("SELECT * FROM mysql_user", allowed=ALLOWED)


def test_validate_uses_schema_whitelist_by_default():
    with pytest.raises(SqlRejected):
        validate_sql("SELECT * FROM some_other_table")


def test_validate_rejects_comma_joined_unknown_table():
    with pytest.raises(SqlRejected, match="不允许查询的表"):
        validate_sql("SELECT * FROM daily, evil", allowed=ALLOWED)


def test_validate_rejects_comma_joined_unknown_table_with_where():
    with pytest.raises(SqlRejected, match="不允许查询的表"):
        validate_sql("SELECT * FROM daily, evil WHERE close > 1", allowed=ALLOWED)


def test_validate_rejects_comma_joined_system_schema():
    with pytest.raises(SqlRejected, match="不允许查询的表"):
        validate_sql("SELECT * FROM information_schema.tables, daily", allowed=ALLOWED)


def test_validate_accepts_comma_joined_allowed_tables_with_aliases():
    sql = validate_sql("SELECT * FROM daily d, stock_basic b", allowed=ALLOWED)

    assert sql.endswith("LIMIT 200")


def test_validate_accepts_select_list_and_string_literal_commas():
    assert validate_sql("SELECT ts_code, close FROM daily",
                        allowed=ALLOWED).endswith("LIMIT 200")
    assert validate_sql("SELECT * FROM daily WHERE name LIKE '%a,b%'",
                        allowed=ALLOWED).endswith("LIMIT 200")


def test_validate_accepts_schema_qualified_allowed_table():
    assert validate_sql("SELECT * FROM quant.daily",
                        allowed=ALLOWED).endswith("LIMIT 200")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM daily JOIN stock_basic "
    "ON daily.ts_code = stock_basic.ts_code, evil",
    "SELECT * FROM daily LEFT JOIN stock_basic AS b "
    "ON b.ts_code = daily.ts_code, evil",
    "SELECT * FROM daily PARTITION (p0), evil",
    "SELECT * FROM daily USE INDEX (i), evil",
    "SELECT * FROM daily, (SELECT 1 WHERE s='(') x, evil",
    "SELECT * FROM evil WHERE note='WITH evil AS ('",
    "SELECT * FROM information_schema.tables, daily",
    "SELECT * FROM quant.evil",
])
def test_validate_rejects_unknown_table_in_join_and_comma_lists(sql):
    with pytest.raises(SqlRejected, match="不允许查询的表"):
        validate_sql(sql, allowed=ALLOWED)


def test_validate_tightens_mysql_offset_comma_limit():
    sql = validate_sql("SELECT * FROM daily LIMIT 10, 5000", allowed=ALLOWED)

    assert sql.endswith("LIMIT 10, 200")
