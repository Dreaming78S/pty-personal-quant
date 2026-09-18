import json

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
