import pandas as pd
import pytest

from quant import maintenance
from quant.data import ingest, schemas


def test_tables_to_truncate_default_matches_schema_tables():
    names = maintenance.tables_to_truncate()

    assert names == list(schemas.TABLES)
    assert "ingest_log" in names


def test_tables_to_truncate_all_tables_reads_information_schema(monkeypatch):
    captured = {}

    def fake_read_df(sql, params=None):
        captured["sql"] = sql
        return pd.DataFrame({"TABLE_NAME": ["daily", "legacy_notes", "trade_cal"]})

    monkeypatch.setattr(maintenance.db, "read_df", fake_read_df)

    names = maintenance.tables_to_truncate(all_tables=True)

    assert "information_schema.TABLES" in captured["sql"]
    assert "TABLE_SCHEMA = DATABASE()" in captured["sql"]
    assert "TABLE_TYPE='BASE TABLE'" in captured["sql"]
    assert "ORDER BY TABLE_NAME" in captured["sql"]
    assert names == ["daily", "legacy_notes", "trade_cal"]


def test_truncate_tables_executes_one_truncate_per_table(monkeypatch):
    executed = []
    monkeypatch.setattr(maintenance.db, "execute",
                        lambda sql: executed.append(sql))

    names = maintenance.truncate_tables()

    assert names == list(schemas.TABLES)
    assert executed == [f"TRUNCATE TABLE `{name}`" for name in schemas.TABLES]


def test_truncate_tables_with_explicit_names(monkeypatch):
    executed = []
    monkeypatch.setattr(maintenance.db, "execute",
                        lambda sql: executed.append(sql))
    monkeypatch.setattr(maintenance.db, "read_df",
                        lambda sql, params=None: pytest.fail(
                            "显式指定表清单时不应查询 information_schema"))

    names = maintenance.truncate_tables(["daily", "ingest_log"])

    assert names == ["daily", "ingest_log"]
    assert executed == ["TRUNCATE TABLE `daily`", "TRUNCATE TABLE `ingest_log`"]


def test_truncate_tables_all_tables_uses_information_schema(monkeypatch):
    executed = []
    monkeypatch.setattr(maintenance.db, "execute",
                        lambda sql: executed.append(sql))
    monkeypatch.setattr(maintenance.db, "read_df",
                        lambda sql, params=None: pd.DataFrame(
                            {"TABLE_NAME": ["legacy_notes", "trade_cal"]}))

    names = maintenance.truncate_tables(all_tables=True)

    assert names == ["legacy_notes", "trade_cal"]
    assert executed == ["TRUNCATE TABLE `legacy_notes`",
                        "TRUNCATE TABLE `trade_cal`"]


def _patch_update(monkeypatch, failing=()):
    calls = []

    def fake_update(table, from_date=None, to_date=None, client=None):
        calls.append((table, from_date))
        if table in failing:
            raise RuntimeError(f"{table} boom")
        return 1

    monkeypatch.setattr(ingest, "update", fake_update)
    return calls


def test_rebuild_passes_expected_from_dates(monkeypatch):
    calls = _patch_update(monkeypatch)

    results = maintenance.rebuild(market_start="20180101",
                                  holdertrade_start="20150101")

    dates = dict(calls)
    assert set(dates) == set(ingest.SPECS)
    for table in ingest.FULL_REFRESH_ORDER:
        assert dates[table] is None
    for table in ingest.SPECS:
        if table in ingest.FULL_REFRESH_ORDER:
            continue
        expected = "20150101" if table == "stk_holdertrade" else "20180101"
        assert dates[table] == expected
    assert all(rows == 1 for rows in results.values())


def test_rebuild_holdertrade_start_defaults_to_market_start(monkeypatch):
    calls = _patch_update(monkeypatch)

    maintenance.rebuild(market_start="20180101")

    assert dict(calls)["stk_holdertrade"] == "20180101"


def test_rebuild_resume_uses_watermarks_for_date_tables(monkeypatch):
    calls = _patch_update(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark",
                        lambda table: "20240105" if table == "daily" else None)

    maintenance.rebuild(market_start="20180101", resume=True)

    dates = dict(calls)
    assert dates["daily"] is None
    assert dates["adj_factor"] == "20180101"
    assert dates["stk_holdertrade"] == "20180101"
    for table in ingest.FULL_REFRESH_ORDER:
        assert dates[table] is None


def test_rebuild_without_resume_ignores_watermarks(monkeypatch):
    calls = _patch_update(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240105")

    maintenance.rebuild(market_start="20180101")

    assert dict(calls)["daily"] == "20180101"


def test_rebuild_normalizes_iso_dates(monkeypatch):
    calls = _patch_update(monkeypatch)

    maintenance.rebuild(market_start="2018-01-01", holdertrade_start="2015-01-01")

    dates = dict(calls)
    assert dates["daily"] == "20180101"
    assert dates["stk_holdertrade"] == "20150101"


def test_rebuild_starts_with_full_refresh_order(monkeypatch):
    calls = _patch_update(monkeypatch)

    maintenance.rebuild()

    order = [table for table, _ in calls]
    assert order[:len(ingest.FULL_REFRESH_ORDER)] == list(
        ingest.FULL_REFRESH_ORDER)
    assert len(order) == len(ingest.SPECS)
    assert dict(calls)["daily"] == "20180101"


def test_rebuild_continues_on_error_and_records_failure(monkeypatch):
    calls = _patch_update(monkeypatch, failing=("adj_factor",))

    results = maintenance.rebuild(market_start="20180101")

    assert results["adj_factor"] == "失败: adj_factor boom"
    assert results["daily"] == 1
    assert len(calls) == len(ingest.SPECS)


def test_rebuild_reraises_when_continue_on_error_false(monkeypatch):
    calls = _patch_update(monkeypatch, failing=("daily",))

    with pytest.raises(RuntimeError, match="daily boom"):
        maintenance.rebuild(market_start="20180101", continue_on_error=False)

    assert calls[-1][0] == "daily"
    assert len(calls) == len(ingest.FULL_REFRESH_ORDER) + 1
