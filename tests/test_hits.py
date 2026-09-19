import json

import pandas as pd
from pydantic import BaseModel

from quant.data import schemas
from quant.engine import hits
from quant.strategies.base import Strategy, register_strategy


class DummyParams(BaseModel):
    threshold: float = 1.0


@register_strategy("hits_dummy")
class DummyHitsStrategy(Strategy):
    Params = DummyParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = 2

    def generate_signals(self, bars):
        return bars["trade_date"].isin(["20240103", "20240104"])


@register_strategy("hits_dummy2")
class DummyHitsStrategy2(DummyHitsStrategy):
    def generate_signals(self, bars):
        return bars["trade_date"] == "20240103"


def make_market():
    rows = []
    for code, name, board, amount in (
        ("600000.SH", "浦发银行", "main", 200.0),
        ("600001.SH", "邯郸钢铁", "main", 100.0),
        ("300001.SZ", "特锐德", "gem", 999.0),
        ("600002.SH", "退市测试", "main", 888.0),
    ):
        for i, d in enumerate(["20240101", "20240102", "20240103", "20240104",
                               "20240105"]):
            rows.append({
                "ts_code": code, "trade_date": d, "name": name,
                "close": 10.0 + i, "raw_close": 9.0 + i,
                "open": 10.0, "high": 11.0, "low": 9.0, "vol": 100.0,
                "amount": amount, "adj_factor": 1.0,
                "suspended": False, "is_st": code == "600002.SH",
                "is_new": False, "board": board,
            })
    return pd.DataFrame(rows)


def patch_deps(monkeypatch, watermark=None, dates=("20240103", "20240104"),
               latest="20240105"):
    captured = {"upserts": [], "watermarks": [], "loads": [], "deletes": []}

    monkeypatch.setattr(hits.loader, "load_market_data",
                        lambda *a, **k: captured["loads"].append((a, k)) or make_market())
    monkeypatch.setattr(hits.ingest, "trade_dates_between",
                        lambda start, end: list(dates))
    monkeypatch.setattr(hits.ingest, "get_watermark", lambda task: watermark)
    monkeypatch.setattr(hits.ingest, "set_watermark",
                        lambda task, d: captured["watermarks"].append((task, d)))
    monkeypatch.setattr(hits.db, "execute",
                        lambda sql, params=None: captured["deletes"].append((sql, params)))

    def fake_upsert(table, df, columns=None):
        captured["upserts"].append((table, df.copy(), columns))
        return len(df)

    monkeypatch.setattr(hits.db, "upsert_df", fake_upsert)
    monkeypatch.setattr(hits.loader, "load_stock_names",
                        lambda: pd.DataFrame({
                            "ts_code": ["600000.SH", "600001.SH", "300001.SZ",
                                        "600002.SH"],
                            "name": ["浦发银行", "邯郸钢铁", "特锐德", "退市测试"]}))
    monkeypatch.setattr(hits.loader, "load_stock_industries",
                        lambda: pd.DataFrame({
                            "ts_code": ["600000.SH", "600001.SH", "300001.SZ",
                                        "600002.SH"],
                            "industry": ["银行", "钢铁", "电气", "地产"]}))
    monkeypatch.setattr(hits.loader, "resolve_trade_date", lambda date=None: latest)
    return captured


def test_fill_hits_writes_ranked_eligible_rows(monkeypatch):
    captured = patch_deps(monkeypatch)

    result = hits.fill_hits("hits_dummy", from_date="20240103",
                            to_date="20240104")

    assert result == {"hits_dummy": 4}
    table, df, columns = captured["upserts"][0]
    assert table == "hit_hits_dummy"
    assert columns == list(schemas.HIT_COLUMNS)
    assert set(df["ts_code"]) == {"600000.SH", "600001.SH"}
    assert list(df["trade_date"].unique()) == ["20240103", "20240104"]
    first = df[df["trade_date"] == "20240103"].sort_values("rank")
    assert list(first["ts_code"]) == ["600000.SH", "600001.SH"]
    assert list(first["rank"]) == [1, 2]
    assert json.loads(df.iloc[0]["params"]) == {"threshold": 1.0}
    assert captured["watermarks"] == [("hit_hits_dummy", "20240104")]


def test_fill_hits_defaults_to_20240101_without_watermark(monkeypatch):
    calls = {}
    patch_deps(monkeypatch)
    monkeypatch.setattr(hits.ingest, "trade_dates_between",
                        lambda start, end: calls.update(start=start, end=end)
                        or ["20240103"])

    hits.fill_hits("hits_dummy")

    assert calls == {"start": "20240101", "end": "20240105"}


def test_fill_hits_resumes_from_watermark(monkeypatch):
    calls = {}
    patch_deps(monkeypatch, watermark="20240103")
    monkeypatch.setattr(hits.ingest, "trade_dates_between",
                        lambda start, end: calls.update(start=start, end=end)
                        or ["20240103"])

    hits.fill_hits("hits_dummy")

    assert calls["start"] == "20240103"


def test_fill_hits_advances_watermark_even_without_hits(monkeypatch):
    captured = patch_deps(monkeypatch)
    monkeypatch.setattr(hits.ingest, "trade_dates_between",
                        lambda start, end: ["20240105"])

    result = hits.fill_hits("hits_dummy", from_date="20240105")

    assert result == {"hits_dummy": 0}
    assert captured["upserts"] == []
    assert captured["watermarks"] == [("hit_hits_dummy", "20240105")]


def test_fill_hits_keeps_watermark_monotonic(monkeypatch):
    captured = patch_deps(monkeypatch, watermark="20240104")

    hits.fill_hits("hits_dummy", from_date="20240103", to_date="20240103")

    assert captured["watermarks"] == [("hit_hits_dummy", "20240104")]


def test_fill_hits_loads_market_once_for_multiple_strategies(monkeypatch):
    captured = patch_deps(monkeypatch)

    result = hits.fill_hits("hits_dummy,hits_dummy2", from_date="20240103",
                            to_date="20240104")

    assert set(result) == {"hits_dummy", "hits_dummy2"}
    assert len(captured["loads"]) == 1


def test_fill_hits_history_columns(monkeypatch):
    captured = patch_deps(monkeypatch)

    hits.fill_hits("hits_dummy", from_date="20240103", to_date="20240104")

    _, df, _ = captured["upserts"][0]
    first = df[(df["ts_code"] == "600000.SH") & (df["trade_date"] == "20240103")].iloc[0]
    assert first["industry"] == "银行"
    assert first["prev_hit"] == 0
    assert first["hit_3d"] == 0 and first["hit_5d"] == 0 and first["hit_10d"] == 0
    assert first["streak"] == 1

    second = df[(df["ts_code"] == "600000.SH") & (df["trade_date"] == "20240104")].iloc[0]
    assert second["prev_hit"] == 1
    assert second["hit_3d"] == 1 and second["hit_5d"] == 1 and second["hit_10d"] == 1
    assert second["streak"] == 2


def test_fill_hits_extends_panel_for_history(monkeypatch):
    captured = patch_deps(monkeypatch)

    hits.fill_hits("hits_dummy", from_date="20240103", to_date="20240104")

    _, kwargs = captured["loads"][0]
    assert kwargs["warmup_days"] == 2 + hits.HISTORY_DAYS


def test_fill_hits_replaces_explicit_window(monkeypatch):
    captured = patch_deps(monkeypatch)

    hits.fill_hits("hits_dummy", from_date="20240103", to_date="20240104")

    assert len(captured["deletes"]) == 1
    sql, params = captured["deletes"][0]
    assert "DELETE FROM `hit_hits_dummy`" in sql
    assert params == ("20240103", "20240104")


def test_fill_hits_incremental_run_does_not_delete(monkeypatch):
    captured = patch_deps(monkeypatch, watermark="20240104")

    hits.fill_hits("hits_dummy")

    assert captured["deletes"] == []


def test_fill_hits_rejects_unknown_strategy(monkeypatch):
    patch_deps(monkeypatch)
    try:
        hits.fill_hits("nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("未注册策略应报错")
