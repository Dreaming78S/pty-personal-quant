import pandas as pd
import pytest

from quant.data import ingest


class FakeClient:
    def __init__(self, fail_on_date=None):
        self.calls = []
        self.fail_on_date = fail_on_date

    def call(self, api, **kwargs):
        self.calls.append((api, kwargs))
        if self.fail_on_date and kwargs.get("trade_date") == self.fail_on_date:
            raise RuntimeError("mock failure")
        return pd.DataFrame({"ts_code": ["000001.SZ"],
                             "trade_date": [kwargs.get("trade_date", "20240102")],
                             "close": [10.0]})


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


def test_full_refresh_upserts_and_sets_watermark(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    client = FakeClient()

    ingest.update("stock_basic", client=client)

    assert state["watermarks"] == [("stock_basic", "20240105")]
    assert len(state["upserts"]) == 1
