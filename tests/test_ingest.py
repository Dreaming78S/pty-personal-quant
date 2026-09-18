import pandas as pd
import pytest

from quant.data import ingest, schemas
from quant.data.tushare_client import (
    DAILY_FIELDS,
    HOLDERTRADE_FIELDS,
    INDEX_DAILY_FIELDS,
)


class FakeClient:
    def __init__(self, fail_on_date=None, stock_basic_df=None,
                 holdertrade_df=None, stock_company_df=None, new_share_df=None):
        self.calls = []
        self.fail_on_date = fail_on_date
        self.stock_basic_df = stock_basic_df
        self.holdertrade_df = holdertrade_df
        self.stock_company_df = stock_company_df
        self.new_share_df = new_share_df
        self.stock_basic_calls = 0
        self.stock_company_calls = 0
        self.new_share_calls = 0

    def _frame(self, trade_date="20240102"):
        return pd.DataFrame({"ts_code": ["000001.SZ"],
                             "trade_date": [trade_date],
                             "close": [10.0]})

    def _holdertrade_frame(self):
        return pd.DataFrame({"ts_code": ["000001.SZ"],
                             "ann_date": ["20240102"],
                             "holder_name": ["甲"],
                             "in_de": ["DE"],
                             "change_vol": [10.0]})

    def call(self, api, **kwargs):
        self.calls.append((api, kwargs))
        if self.fail_on_date and kwargs.get("trade_date") == self.fail_on_date:
            raise RuntimeError("mock failure")
        if api == "stk_holdertrade":
            if self.holdertrade_df is not None:
                return self.holdertrade_df.copy()
            return self._holdertrade_frame()
        return self._frame(kwargs.get("trade_date", "20240102"))

    def fetch_stock_basic(self):
        self.stock_basic_calls += 1
        if self.stock_basic_df is not None:
            return self.stock_basic_df
        return self._frame()

    def fetch_stock_company(self):
        self.stock_company_calls += 1
        if self.stock_company_df is not None:
            return self.stock_company_df
        return self._frame()

    def fetch_new_share(self):
        self.new_share_calls += 1
        if self.new_share_df is not None:
            return self.new_share_df
        return self._frame()


def _patch_db(monkeypatch):
    state = {"upserts": [], "watermarks": []}
    monkeypatch.setattr(ingest.db, "upsert_df",
                        lambda table, df: state["upserts"].append(df) or len(df))
    monkeypatch.setattr(ingest, "set_watermark",
                        lambda table, d: state["watermarks"].append((table, d)))
    return state


def test_prepare_keeps_only_schema_columns():
    df = pd.DataFrame({"ts_code": ["a"], "close": [1.0], "extra": ["x"]})
    out = ingest._prepare(df, "adj_factor")
    assert list(out.columns) == ["ts_code"]


def test_update_skips_dates_at_or_before_watermark(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240103")
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240103", "20240104", "20240105"])
    client = FakeClient()

    n = ingest.update("daily", to_date="20240105", client=client)

    assert [kw["trade_date"] for _, kw in client.calls] == ["20240104", "20240105"]
    assert state["watermarks"][-1] == ("daily", "20240105")
    assert n == 2


def test_update_respects_from_date_override(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240103")
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240102", "20240103"])
    client = FakeClient()

    ingest.update("daily", from_date="20240102", to_date="20240103", client=client)

    assert [kw["trade_date"] for _, kw in client.calls] == ["20240102", "20240103"]


def test_watermark_never_regresses_on_manual_backfill(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240130")
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240102", "20240103", "20240110"])
    client = FakeClient()

    ingest.update("daily", from_date="20240102", to_date="20240110", client=client)

    assert [kw["trade_date"] for _, kw in client.calls] == [
        "20240102", "20240103", "20240110"]
    assert state["watermarks"] == [("daily", "20240130")]


def test_watermark_not_advanced_for_failed_batch(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "BATCH_DATES", 2)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: None)
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240101", "20240102", "20240103", "20240104"])
    client = FakeClient(fail_on_date="20240103")

    with pytest.raises(RuntimeError):
        ingest.update("daily", to_date="20240104", client=client)

    assert state["watermarks"] == [("daily", "20240102")]


def test_by_date_fetch_passes_fields(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: None)
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240102"])
    client = FakeClient()

    ingest.update("daily", to_date="20240102", client=client)

    assert client.calls == [("daily", {"trade_date": "20240102",
                                       "fields": DAILY_FIELDS})]


def test_index_daily_fetch_passes_fields(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: None)
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240102"])
    client = FakeClient()

    ingest.update("index_daily", to_date="20240102", client=client)

    assert [kw["fields"] for _, kw in client.calls] == [INDEX_DAILY_FIELDS]
    assert [kw["ts_code"] for _, kw in client.calls] == ["000300.SH"]


def test_by_calendar_day_passes_ann_date_and_advances_watermark(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240101")
    captured = {}

    def fake_calendar(start, end):
        captured["range"] = (start, end)
        return ["20240102", "20240103", "20240104"]

    monkeypatch.setattr(ingest, "calendar_days_between", fake_calendar)
    client = FakeClient()

    n = ingest.update("stk_holdertrade", to_date="20240104", client=client)

    assert captured["range"] == ("20240101", "20240104")
    assert [kw["ann_date"] for _, kw in client.calls] == [
        "20240102", "20240103", "20240104"]
    assert [kw["fields"] for _, kw in client.calls] == [HOLDERTRADE_FIELDS] * 3
    assert state["watermarks"][-1] == ("stk_holdertrade", "20240104")
    assert n == 3


def test_by_calendar_day_default_start_is_spec_default(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: None)
    captured = {}

    def fake_calendar(start, end):
        captured["start"] = start
        return []

    monkeypatch.setattr(ingest, "calendar_days_between", fake_calendar)
    client = FakeClient()

    ingest.update("stk_holdertrade", client=client)

    assert captured["start"] == "20150101"
    assert client.calls == []


def test_by_calendar_day_drops_rows_without_change_vol(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: None)
    monkeypatch.setattr(ingest, "calendar_days_between",
                        lambda start, end: ["20240102"])
    client = FakeClient(holdertrade_df=pd.DataFrame({
        "ts_code": ["000001.SZ", "000002.SZ"],
        "ann_date": ["20240102", "20240102"],
        "holder_name": ["甲", "乙"],
        "in_de": ["DE", "IN"],
        "change_vol": [None, 5.0],
    }))

    ingest.update("stk_holdertrade", to_date="20240102", client=client)

    assert len(state["upserts"]) == 1
    upserted = state["upserts"][0]
    assert list(upserted["ts_code"]) == ["000002.SZ"]
    assert list(upserted["change_vol"]) == [5.0]


def test_full_refresh_upserts_and_sets_watermark(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    client = FakeClient()

    ingest.update("stock_basic", client=client)

    assert state["watermarks"] == [("stock_basic", "20240105")]
    assert len(state["upserts"]) == 1
    assert client.stock_basic_calls == 1
    assert client.calls == []


def test_stock_basic_full_refresh_keeps_delisted_rows(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    combined = pd.DataFrame({
        "ts_code": ["000001.SZ", "600001.SH"],
        "symbol": ["000001", "600001"],
        "name": ["平安银行", "退市示例"],
        "area": ["深圳", "上海"],
        "industry": ["银行", "综合"],
        "market": ["主板", "主板"],
        "exchange": ["SZSE", "SSE"],
        "list_status": ["L", "D"],
        "list_date": ["19910403", "19990101"],
        "delist_date": [None, "20200514"],
        "is_hs": ["S", "N"],
        "cnspell": ["PAYH", "TSSL"],
    })
    client = FakeClient(stock_basic_df=combined)

    n = ingest.update("stock_basic", client=client)

    assert n == 2
    prepared = state["upserts"][0]
    assert list(prepared.columns) == schemas.columns_of("stock_basic")
    delisted = prepared[prepared["ts_code"] == "600001.SH"].iloc[0]
    assert delisted["list_status"] == "D"
    assert delisted["delist_date"] == "20200514"


def test_stock_company_full_refresh_uses_dedicated_fetcher(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    client = FakeClient()

    n = ingest.update("stock_company", client=client)

    assert client.stock_company_calls == 1
    assert client.calls == []
    assert state["watermarks"] == [("stock_company", "20240105")]
    assert n == 1


def test_new_share_full_refresh_uses_dedicated_fetcher(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    client = FakeClient()

    n = ingest.update("new_share", client=client)

    assert client.new_share_calls == 1
    assert client.calls == []
    assert state["watermarks"] == [("new_share", "20240105")]
    assert n == 1


def test_update_all_refreshes_company_and_ipo_snapshots(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240105")
    monkeypatch.setattr(ingest, "trade_dates_between", lambda start, end: [])
    monkeypatch.setattr(ingest, "calendar_days_between", lambda start, end: [])
    client = FakeClient()

    results = ingest.update_all(client=client)

    assert "stock_company" in ingest.FULL_REFRESH_ORDER
    assert "new_share" in ingest.FULL_REFRESH_ORDER
    assert list(results)[:len(ingest.FULL_REFRESH_ORDER)] == list(
        ingest.FULL_REFRESH_ORDER)
    assert list(results).index("stock_company") < list(results).index("daily")
    assert client.stock_company_calls == 1
    assert client.new_share_calls == 1
