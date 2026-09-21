import json
from decimal import Decimal

import pandas as pd
import pytest

from quant.data import cache


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_CACHE_DIR", str(tmp_path))
    return tmp_path


def _fake_db(monkeypatch, frames):
    calls = {"n": 0}

    def fake_read_df(sql, params=None):
        calls["n"] += 1
        return frames(sql, params)

    monkeypatch.setattr(cache.db, "read_df", fake_read_df)
    return calls


def test_ensure_cache_creates_parquet_and_meta(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "close": [10.0]}))

    path = cache.ensure_cache("daily")

    assert path.exists()
    df = pd.read_parquet(path)
    assert len(df) == 1
    meta = json.loads((cache.cache_dir() / "daily.meta.json").read_text(encoding="utf-8"))
    assert meta["watermark"] == "20240102"


def test_ensure_cache_noop_when_watermark_unchanged(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    calls = _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "close": [10.0]}))
    cache.ensure_cache("daily")
    cache.ensure_cache("daily")
    assert calls["n"] == 1


def test_ensure_cache_appends_delta(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "close": [10.0]}))
    cache.ensure_cache("daily")

    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240103")
    seen = {}

    def delta(sql, params):
        seen["params"] = params
        return pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240103"], "close": [11.0]})

    monkeypatch.setattr(cache.db, "read_df", delta)
    cache.ensure_cache("daily")

    df = pd.read_parquet(cache.cache_path("daily"))
    assert list(df["trade_date"]) == ["20240102", "20240103"]
    assert seen["params"] == ("20240102",)


def test_ensure_cache_replaces_non_date_table_snapshot(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240105")
    first = pd.DataFrame({
        "ts_code": ["000001.SZ"],
        "symbol": ["000001"],
        "name": ["平安银行"],
    })
    _fake_db(monkeypatch, lambda sql, params: first)
    cache.ensure_cache("stock_basic")

    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240106")
    second = pd.DataFrame({
        "ts_code": ["000001.SZ", "600001.SH"],
        "symbol": ["000001", "600001"],
        "name": ["平安银行", "退市示例"],
    })
    monkeypatch.setattr(cache.db, "read_df", lambda sql, params=None: second)
    cache.ensure_cache("stock_basic")

    df = pd.read_parquet(cache.cache_path("stock_basic"))
    assert list(df["ts_code"]) == ["000001.SZ", "600001.SH"]


def test_load_table_filters_range_and_codes(monkeypatch):
    df = pd.DataFrame({
        "ts_code": ["A", "A", "B"],
        "trade_date": ["20240101", "20240103", "20240102"],
        "close": [1.0, 2.0, 3.0],
    })
    cache.cache_dir().mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache.cache_path("daily"), index=False)

    out = cache.load_table("daily", start="20240102", end="20240102", ts_codes=["B"])
    assert list(out["close"]) == [3.0]


def test_load_table_missing_cache_raises(tmp_cache):
    with pytest.raises(FileNotFoundError, match="缓存缺失"):
        cache.load_table("daily")


def test_ensure_cache_handles_empty_mysql(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: None)
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame())

    cache.ensure_cache("daily")

    out = cache.load_table("daily", columns=["ts_code", "trade_date"])
    assert out.empty
    assert list(out.columns) == ["ts_code", "trade_date"]


def test_ensure_cache_converts_decimal_delta_to_float(monkeypatch):
    """既有镜像为 float64、MySQL 增量以 Decimal 返回时不能写坏缓存。"""
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"],
         "open": [11.7]}))
    cache.ensure_cache("daily")
    assert pd.read_parquet(cache.cache_path("daily"))["open"].dtype == "float64"

    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240103")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240103"],
         "open": [Decimal("12.5000")]}))

    path = cache.ensure_cache("daily")

    df = pd.read_parquet(path)
    assert list(df["open"]) == [11.7, 12.5]
    assert df["open"].dtype == "float64"
    assert list(df["trade_date"]) == ["20240102", "20240103"]


def test_ensure_cache_converts_decimal_snapshot_to_float(monkeypatch):
    """非日期表整表快照里的 Decimal 也统一成 float64，与日期表镜像一致。"""
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame({
        "ts_code": ["000001.SZ"], "sub_code": ["000001"], "name": ["示例"],
        "amount": [Decimal("1234.5000")], "pe": [Decimal("23.450000")]}))

    path = cache.ensure_cache("new_share")

    df = pd.read_parquet(path)
    assert df["amount"].dtype == "float64"
    assert df["pe"].dtype == "float64"
    assert list(df["amount"]) == [1234.5]
    assert list(df["name"]) == ["示例"]
