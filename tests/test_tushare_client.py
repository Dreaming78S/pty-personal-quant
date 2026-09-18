import datetime

import pandas as pd
import pytest

from quant.data import schemas
from quant.data.tushare_client import (
    ADJ_FACTOR_FIELDS,
    DAILY_BASIC_FIELDS,
    DAILY_FIELDS,
    HOLDERTRADE_FIELDS,
    INDEX_DAILY_FIELDS,
    NAMECHANGE_FIELDS,
    NEW_SHARE_FIELDS,
    STK_LIMIT_FIELDS,
    STOCK_BASIC_FIELDS,
    STOCK_COMPANY_FIELDS,
    SUSPEND_FIELDS,
    TRADE_CAL_FIELDS,
    RateLimiter,
    TushareClient,
)


def test_rate_limiter_first_call_no_sleep_then_waits():
    clock_time = {"t": 0.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock_time["t"] += seconds

    rl = RateLimiter(120, sleep=fake_sleep, clock=lambda: clock_time["t"])
    rl.acquire()
    assert sleeps == []
    rl.acquire()
    assert sleeps == pytest.approx([0.5], abs=1e-6)


def test_call_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("quant.data.tushare_client.time.sleep", lambda s: None)

    class FakePro:
        def __init__(self):
            self.calls = 0

        def query(self, api, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("限流")
            return pd.DataFrame({"x": [1]})

    pro = FakePro()
    client = TushareClient(token="t", pro=pro)
    df = client.call("daily", trade_date="20240102")
    assert len(df) == 1
    assert pro.calls == 3


def test_call_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr("quant.data.tushare_client.time.sleep", lambda s: None)

    class FailPro:
        def query(self, api, **kwargs):
            raise RuntimeError("boom")

    client = TushareClient(token="t", pro=FailPro(), max_retries=2)
    with pytest.raises(RuntimeError, match="daily"):
        client.call("daily", trade_date="20240102")


def test_fetch_stock_basic_requests_exchange_status_matrix_and_dedupes():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            key = (kwargs["exchange"], kwargs["list_status"])
            if key == ("SSE", "L"):
                return pd.DataFrame({"ts_code": ["600001.SH", "600002.SH"],
                                     "list_status": ["L", "L"],
                                     "delist_date": [None, None]})
            if key == ("SSE", "D"):
                return pd.DataFrame({"ts_code": ["600002.SH"],
                                     "list_status": ["D"],
                                     "delist_date": ["20200101"]})
            if key == ("SZSE", "L"):
                return pd.DataFrame({"ts_code": ["000001.SZ"],
                                     "list_status": ["L"],
                                     "delist_date": [None]})
            if key == ("BSE", "P"):
                return pd.DataFrame({"ts_code": ["600003.SH"],
                                     "list_status": ["P"],
                                     "delist_date": [None]})
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    df = client.fetch_stock_basic()

    assert calls == [
        ("stock_basic", {"exchange": exchange, "list_status": status,
                         "fields": STOCK_BASIC_FIELDS})
        for exchange in ("SSE", "SZSE", "BSE")
        for status in ("L", "D", "P")
    ]
    assert list(df["ts_code"]) == ["600001.SH", "600002.SH", "000001.SZ",
                                   "600003.SH"]
    assert list(df.index) == [0, 1, 2, 3]
    deduped = df[df["ts_code"] == "600002.SH"].iloc[0]
    assert deduped["list_status"] == "D"
    assert deduped["delist_date"] == "20200101"


def test_fetch_stock_basic_raises_when_all_frames_empty():
    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    with pytest.raises(RuntimeError, match="stock_basic"):
        client.fetch_stock_basic()


def test_fetch_helpers_pass_kwargs_and_fields():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    client.fetch_daily("20240102")
    client.fetch_adj_factor("20240102")
    client.fetch_daily_basic("20240102")
    client.fetch_suspend_d("20240102")
    client.fetch_stk_limit("20240102")
    client.fetch_index_daily("000300.SH", "20240102")
    client.fetch_trade_cal("20240101", "20240131")

    assert calls[0] == ("daily", {"trade_date": "20240102", "fields": DAILY_FIELDS})
    assert calls[1] == ("adj_factor",
                        {"trade_date": "20240102", "fields": ADJ_FACTOR_FIELDS})
    assert calls[2] == ("daily_basic",
                        {"trade_date": "20240102", "fields": DAILY_BASIC_FIELDS})
    assert calls[3] == ("suspend_d",
                        {"trade_date": "20240102", "fields": SUSPEND_FIELDS})
    assert calls[4] == ("stk_limit",
                        {"trade_date": "20240102", "fields": STK_LIMIT_FIELDS})
    assert calls[5] == ("index_daily", {"ts_code": "000300.SH",
                                       "trade_date": "20240102",
                                       "fields": INDEX_DAILY_FIELDS})
    assert calls[6] == ("trade_cal", {"exchange": "SSE", "start_date": "20240101",
                                      "end_date": "20240131",
                                      "fields": TRADE_CAL_FIELDS})


def test_fetch_stock_company_queries_three_exchanges_and_dedupes():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            if kwargs["exchange"] == "SSE":
                return pd.DataFrame({"ts_code": ["600000.SH", "600001.SH"],
                                     "exchange": ["SSE", "SSE"]})
            if kwargs["exchange"] == "SZSE":
                return pd.DataFrame({"ts_code": ["000001.SZ"],
                                     "exchange": ["SZSE"]})
            return pd.DataFrame({"ts_code": ["600000.SH"], "exchange": ["BSE"]})

    client = TushareClient(token="t", pro=FakePro())
    df = client.fetch_stock_company()

    assert calls == [
        ("stock_company", {"exchange": "SSE", "fields": STOCK_COMPANY_FIELDS}),
        ("stock_company", {"exchange": "SZSE", "fields": STOCK_COMPANY_FIELDS}),
        ("stock_company", {"exchange": "BSE", "fields": STOCK_COMPANY_FIELDS}),
    ]
    assert list(df["ts_code"]) == ["600001.SH", "000001.SZ", "600000.SH"]
    assert list(df.index) == [0, 1, 2]
    deduped = df[df["ts_code"] == "600000.SH"].iloc[0]
    assert deduped["exchange"] == "BSE"


def test_fetch_new_share_loops_years_and_dedupes():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            year = kwargs["start_date"][:4]
            return pd.DataFrame({"ts_code": ["300001.SZ"],
                                 "ipo_date": [f"{year}0101"]})

    client = TushareClient(token="t", pro=FakePro())
    df = client.fetch_new_share()

    assert len(calls) >= 30
    assert {api for api, _ in calls} == {"new_share"}
    years = []
    for _, kwargs in calls:
        start, end = kwargs["start_date"], kwargs["end_date"]
        assert start.endswith("0101") and end.endswith("1231")
        assert start[:4] == end[:4]
        assert kwargs["fields"] == NEW_SHARE_FIELDS
        years.append(int(start[:4]))
    assert years[0] == 1990
    assert years == list(range(1990, datetime.date.today().year + 1))
    assert list(df["ts_code"]) == ["300001.SZ"]
    assert list(df.index) == [0]


def test_fetch_namechange_loops_years_and_dedupes_on_ts_code_start_date():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            year = kwargs.get("start_date", "")[:4]
            if year == "1991":
                return pd.DataFrame({"ts_code": ["000001.SZ"],
                                     "name": ["深发展A"],
                                     "start_date": ["19910403"]})
            if year == "1992":
                return pd.DataFrame({"ts_code": ["000001.SZ"],
                                     "name": ["深发展"],
                                     "start_date": ["19910403"]})
            if year == "1993":
                return pd.DataFrame({"ts_code": ["000001.SZ"],
                                     "name": ["平安银行"],
                                     "start_date": ["19930701"]})
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    df = client.fetch_namechange()

    assert len(calls) >= 30
    assert {api for api, _ in calls} == {"namechange"}
    years = []
    for _, kwargs in calls:
        start, end = kwargs["start_date"], kwargs["end_date"]
        assert start.endswith("0101") and end.endswith("1231")
        assert start[:4] == end[:4]
        assert kwargs["fields"] == NAMECHANGE_FIELDS
        years.append(int(start[:4]))
    assert years[0] == 1990
    assert years == list(range(1990, datetime.date.today().year + 1))
    # 同一 (ts_code, start_date) 保留后出现的年份（keep="last"），
    # 不同 start_date 的历史曾用名全部保留
    assert list(df["start_date"]) == ["19910403", "19930701"]
    assert list(df["ts_code"]) == ["000001.SZ", "000001.SZ"]
    overridden = df[df["start_date"] == "19910403"].iloc[0]
    assert overridden["name"] == "深发展"


FIELDS_BY_TABLE = {
    "daily": DAILY_FIELDS,
    "adj_factor": ADJ_FACTOR_FIELDS,
    "daily_basic": DAILY_BASIC_FIELDS,
    "suspend_d": SUSPEND_FIELDS,
    "stk_limit": STK_LIMIT_FIELDS,
    "index_daily": INDEX_DAILY_FIELDS,
    "trade_cal": TRADE_CAL_FIELDS,
    "namechange": NAMECHANGE_FIELDS,
    "stock_company": STOCK_COMPANY_FIELDS,
    "new_share": NEW_SHARE_FIELDS,
    "stk_holdertrade": HOLDERTRADE_FIELDS,
}


@pytest.mark.parametrize("table", sorted(FIELDS_BY_TABLE))
def test_field_constants_match_schema_columns(table):
    assert FIELDS_BY_TABLE[table].split(",") == schemas.columns_of(table)


def test_stock_basic_fields_match_schema_plus_cnspell():
    fields = set(STOCK_BASIC_FIELDS.split(","))
    assert fields == set(schemas.columns_of("stock_basic")) | {"cnspell"}


def test_fetch_stock_company_raises_when_an_exchange_frame_is_empty():
    class FakePro:
        def query(self, api, **kwargs):
            if kwargs["exchange"] == "SZSE":
                return pd.DataFrame()
            return pd.DataFrame({"ts_code": ["600000.SH"],
                                 "exchange": [kwargs["exchange"]]})

    client = TushareClient(token="t", pro=FakePro())
    with pytest.raises(RuntimeError, match="SZSE"):
        client.fetch_stock_company()


def test_fetch_new_share_raises_when_all_years_are_empty():
    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    with pytest.raises(RuntimeError, match="new_share"):
        client.fetch_new_share()


def test_fetch_namechange_raises_when_all_years_are_empty():
    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    with pytest.raises(RuntimeError, match="namechange"):
        client.fetch_namechange()


def test_call_uses_90_per_minute_limiter_for_stk_holdertrade():
    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    client.call("stk_holdertrade", trade_date="20240102")

    assert client._limiter_for("stk_holdertrade")._interval == pytest.approx(
        60 / 90, abs=1e-6)
    assert client._limiter_for("daily")._interval == pytest.approx(
        60 / 150, abs=1e-6)


def test_call_uses_45_per_minute_limiter_for_stock_basic():
    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    client.call("stock_basic", exchange="SSE", list_status="L",
                fields=STOCK_BASIC_FIELDS)

    assert client._limiter_for("stock_basic")._interval == pytest.approx(
        60 / 45, abs=1e-6)
    assert client._limiter_for("daily")._interval == pytest.approx(
        60 / 150, abs=1e-6)


def test_call_acquires_the_limiter_for_its_api(monkeypatch):
    acquired = []
    monkeypatch.setattr(RateLimiter, "acquire",
                        lambda self: acquired.append(self))

    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    client.call("stk_holdertrade", trade_date="20240102")

    assert acquired == [client._limiter_for("stk_holdertrade")]


def test_api_rate_limits_override_only_named_api():
    class FakePro:
        def query(self, api, **kwargs):
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro(),
                           api_rate_limits={"daily": 30})
    client.call("daily", trade_date="20240102")

    assert client._limiter_for("daily")._interval == pytest.approx(
        60 / 30, abs=1e-6)
    assert client._limiter_for("daily_basic")._interval == pytest.approx(
        60 / 150, abs=1e-6)
    assert client._limiter_for("stk_holdertrade")._interval == pytest.approx(
        60 / 90, abs=1e-6)
