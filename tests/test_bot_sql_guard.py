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
