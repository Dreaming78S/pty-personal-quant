import pandas as pd
import pytest

from quant.data import schemas
from quant.engine import limits

DATES = ["20240102", "20240103", "20240104", "20240105"]
RANK_OF = {d: i for i, d in enumerate(DATES)}


def _bar(code, day, *, up=0.0, down=0.0, one_word=False, **flags):
    raw_close = 11.0 if up else (9.0 if down else 10.0)
    if one_word:
        raw_open = raw_high = raw_low = raw_close
    elif up:
        raw_open, raw_high, raw_low = 10.5, 11.0, 10.2
    elif down:
        raw_open, raw_high, raw_low = 9.5, 9.8, 9.0
    else:
        raw_open, raw_high, raw_low = 10.0, 10.2, 9.8
    return {
        "ts_code": code,
        "trade_date": day,
        "open": raw_open,
        "high": raw_high,
        "low": raw_low,
        "close": raw_close,
        "raw_open": raw_open,
        "raw_high": raw_high,
        "raw_low": raw_low,
        "raw_close": raw_close,
        "raw_pre_close": 10.0,
        "pct_chg": 10.0 if up else (-10.0 if down else 0.0),
        "vol": 100.0,
        "amount": 1000.0,
        "adj_factor": 1.0,
        "up_limit": 11.0,
        "down_limit": 9.0,
        "turnover_rate": 1.0,
        "volume_ratio": 1.0,
        "pe_ttm": 10.0,
        "pb": 1.0,
        "total_mv": 20000.0,
        "circ_mv": 10000.0,
        "board": "main",
        "is_st": False,
        "is_new": False,
        "suspended": False,
        **flags,
    }


def make_market():
    rows = []
    for day in DATES:
        rows.extend([
            _bar("600000.SH", day,
                 up=1 if day in ("20240102", "20240103") else 0,
                 one_word=day == "20240102"),
            _bar("600001.SH", day, up=1 if day == "20240104" else 0),
            _bar("000001.SZ", day, down=1 if day in ("20240102", "20240103") else 0,
                 one_word=day == "20240102"),
            _bar("600002.SH", day, up=1 if day == "20240102" else 0,
                 is_st=True),
            _bar("600003.SH", day, up=1 if day == "20240102" else 0,
                 is_new=day == "20240102"),
            _bar("600004.SH", day, up=1 if day == "20240102" else 0,
                 suspended=day == "20240102"),
            _bar("300001.SZ", day, up=1 if day == "20240102" else 0,
                 board="gem"),
            _bar("900901.SH", day, up=1 if day == "20240102" else 0),
            _bar("600005.SH", day,
                 up=1 if day in ("20240102", "20240104", "20240105") else 0),
        ])
    return pd.DataFrame(rows)


def patch_names(monkeypatch):
    codes = ["600000.SH", "600001.SH", "000001.SZ", "600005.SH"]
    monkeypatch.setattr(limits.loader, "load_stock_names",
                        lambda: pd.DataFrame({
                            "ts_code": codes,
                            "name": ["浦发银行", "邯郸钢铁", "平安银行", "测试股"]}))
    monkeypatch.setattr(limits.loader, "load_stock_industries",
                        lambda: pd.DataFrame({
                            "ts_code": codes,
                            "industry": ["银行", "钢铁", "银行", "测试"]}))


def test_build_limit_frames_filters_board_st_new_and_suspended(monkeypatch):
    patch_names(monkeypatch)

    frames = limits.build_limit_frames(make_market(), RANK_OF)

    up = frames["limit_up_daily"]
    assert set(zip(up["ts_code"], up["trade_date"])) == {
        ("600000.SH", "20240102"), ("600000.SH", "20240103"),
        ("600001.SH", "20240104"),
        ("600005.SH", "20240102"), ("600005.SH", "20240104"),
        ("600005.SH", "20240105"),
    }
    down = frames["limit_down_daily"]
    assert set(zip(down["ts_code"], down["trade_date"])) == {
        ("000001.SZ", "20240102"), ("000001.SZ", "20240103"),
    }


def test_build_limit_frames_sets_limit_price_one_word_and_streak(monkeypatch):
    patch_names(monkeypatch)

    frames = limits.build_limit_frames(make_market(), RANK_OF)
    up = frames["limit_up_daily"].set_index(["ts_code", "trade_date"])
    down = frames["limit_down_daily"].set_index(["ts_code", "trade_date"])

    assert (up["limit_price"] == 11.0).all()
    assert (down["limit_price"] == 9.0).all()
    assert up.loc[("600000.SH", "20240102"), "is_one_word"] == 1
    assert up.loc[("600000.SH", "20240103"), "is_one_word"] == 0
    assert down.loc[("000001.SZ", "20240102"), "is_one_word"] == 1
    assert down.loc[("000001.SZ", "20240103"), "is_one_word"] == 0

    assert up.loc[("600000.SH", "20240102"), "streak"] == 1
    assert up.loc[("600000.SH", "20240103"), "streak"] == 2
    assert up.loc[("600005.SH", "20240102"), "streak"] == 1
    assert up.loc[("600005.SH", "20240104"), "streak"] == 1
    assert up.loc[("600005.SH", "20240105"), "streak"] == 2
    assert down.loc[("000001.SZ", "20240103"), "streak"] == 2


def test_build_limit_frames_merges_name_and_industry(monkeypatch):
    patch_names(monkeypatch)

    up = limits.build_limit_frames(make_market(), RANK_OF)["limit_up_daily"]
    row = up[(up["ts_code"] == "600000.SH") &
             (up["trade_date"] == "20240102")].iloc[0]

    assert row["name"] == "浦发银行"
    assert row["industry"] == "银行"
    assert list(up.columns) == list(schemas.LIMIT_COLUMNS)


def test_build_limit_rows_rejects_unknown_direction(monkeypatch):
    patch_names(monkeypatch)
    with pytest.raises(ValueError, match="sideways"):
        limits.build_limit_rows(make_market(), "sideways", RANK_OF)


def _patch_fill(monkeypatch, watermark=None, dates=("20240102", "20240103"),
                latest="20240105"):
    captured = {"upserts": [], "watermarks": [], "loads": [], "deletes": []}
    market = make_market()
    patch_names(monkeypatch)

    monkeypatch.setattr(limits.loader, "load_market_data",
                        lambda *a, **k: captured["loads"].append((a, k)) or market)
    monkeypatch.setattr(limits.ingest, "trade_dates_between",
                        lambda start, end: list(dates))
    monkeypatch.setattr(limits.ingest, "get_watermark", lambda task: watermark)
    monkeypatch.setattr(limits.ingest, "set_watermark",
                        lambda task, d: captured["watermarks"].append((task, d)))
    monkeypatch.setattr(limits.db, "execute",
                        lambda sql, params=None: captured["deletes"].append((sql, params)))

    def fake_upsert(table, df, columns=None):
        captured["upserts"].append((table, df.copy(), columns))
        return len(df)

    monkeypatch.setattr(limits.db, "upsert_df", fake_upsert)
    monkeypatch.setattr(limits.loader, "resolve_trade_date", lambda date=None: latest)
    monkeypatch.setattr(limits.loader, "history_warmup_days", lambda start: 0)
    monkeypatch.setattr(limits.loader, "open_trade_dates", lambda: DATES)
    return captured


def test_fill_limits_writes_both_tables_and_watermarks(monkeypatch):
    captured = _patch_fill(monkeypatch)

    result = limits.fill_limits(from_date="20240102", to_date="20240103")

    assert result == {"limit_up_daily": 3, "limit_down_daily": 2}
    assert [table for table, _, _ in captured["upserts"]] == [
        "limit_up_daily", "limit_down_daily"]
    for table, df, columns in captured["upserts"]:
        assert columns == list(schemas.LIMIT_COLUMNS)
        assert set(df["trade_date"]) <= {"20240102", "20240103"}
        assert len(df) == result[table]
    assert captured["watermarks"] == [
        ("limit_up_daily", "20240103"),
        ("limit_down_daily", "20240103"),
    ]
    assert len(captured["deletes"]) == 2
    assert captured["deletes"][0][1] == ("20240102", "20240103")


def test_fill_limits_defaults_to_20180101_without_watermark(monkeypatch):
    calls = {}
    _patch_fill(monkeypatch)
    monkeypatch.setattr(limits.ingest, "trade_dates_between",
                        lambda start, end: calls.update(start=start, end=end)
                        or ["20240102"])

    limits.fill_limits()

    assert calls == {"start": limits.DEFAULT_START, "end": "20240105"}


def test_fill_limits_resumes_from_watermark(monkeypatch):
    calls = {}
    _patch_fill(monkeypatch, watermark="20240103")
    monkeypatch.setattr(limits.ingest, "trade_dates_between",
                        lambda start, end: calls.update(start=start, end=end)
                        or ["20240103"])

    limits.fill_limits()

    assert calls["start"] == "20240103"


def test_fill_limits_advances_watermark_without_rows(monkeypatch):
    captured = _patch_fill(monkeypatch, dates=("20240108",))
    monkeypatch.setattr(limits.ingest, "trade_dates_between",
                        lambda start, end: ["20240108"])
    monkeypatch.setattr(limits.loader, "resolve_trade_date",
                        lambda date=None: "20240108")

    result = limits.fill_limits(from_date="20240108")

    assert result == {"limit_up_daily": 0, "limit_down_daily": 0}
    assert captured["upserts"] == []
    assert captured["watermarks"] == [
        ("limit_up_daily", "20240108"),
        ("limit_down_daily", "20240108"),
    ]


def test_fill_limits_keeps_watermark_monotonic(monkeypatch):
    captured = _patch_fill(monkeypatch, watermark="20240104")
    monkeypatch.setattr(limits.ingest, "trade_dates_between",
                        lambda start, end: ["20240102"])

    limits.fill_limits(from_date="20240102", to_date="20240102")

    assert captured["watermarks"] == [
        ("limit_up_daily", "20240104"),
        ("limit_down_daily", "20240104"),
    ]


def test_fill_limits_loads_full_history_and_ensures_cache(monkeypatch):
    captured = _patch_fill(monkeypatch)

    limits.fill_limits(from_date="20240102", to_date="20240103")

    assert len(captured["loads"]) == 1
    args, kwargs = captured["loads"][0]
    assert args == ("20240102", "20240103")
    assert kwargs["warmup_days"] == 0
    assert kwargs["ensure"] is True


def test_fill_limits_incremental_run_does_not_delete(monkeypatch):
    captured = _patch_fill(monkeypatch, watermark="20240103")

    limits.fill_limits()

    assert captured["deletes"] == []
