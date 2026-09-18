import pandas as pd
import pytest

from quant.data.tushare_client import RateLimiter, TushareClient


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


STOCK_BASIC_FIELDS = (
    "ts_code,symbol,name,area,industry,market,exchange,"
    "list_status,list_date,delist_date,is_hs,cnspell"
)


def test_fetch_stock_basic_requests_all_statuses_and_dedupes():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            if kwargs["list_status"] == "L":
                return pd.DataFrame({"ts_code": ["000001.SZ", "600001.SH"],
                                     "list_status": ["L", "L"],
                                     "delist_date": [None, None]})
            if kwargs["list_status"] == "D":
                return pd.DataFrame({"ts_code": ["000001.SZ"],
                                     "list_status": ["D"],
                                     "delist_date": ["20200101"]})
            return pd.DataFrame({"ts_code": ["600002.SH"],
                                 "list_status": ["P"],
                                 "delist_date": [None]})

    client = TushareClient(token="t", pro=FakePro())
    df = client.fetch_stock_basic()

    common = {"exchange": "", "fields": STOCK_BASIC_FIELDS}
    assert calls == [
        ("stock_basic", {**common, "list_status": "L"}),
        ("stock_basic", {**common, "list_status": "D"}),
        ("stock_basic", {**common, "list_status": "P"}),
    ]
    assert list(df["ts_code"]) == ["600001.SH", "000001.SZ", "600002.SH"]
    assert list(df.index) == [0, 1, 2]
    deduped = df[df["ts_code"] == "000001.SZ"].iloc[0]
    assert deduped["list_status"] == "D"
    assert deduped["delist_date"] == "20200101"


def test_fetch_helpers_pass_kwargs():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    client.fetch_daily("20240102")
    client.fetch_index_daily("000300.SH", "20240102")
    client.fetch_trade_cal("20240101", "20240131")

    assert calls[0] == ("daily", {"trade_date": "20240102"})
    assert calls[1] == ("index_daily", {"ts_code": "000300.SH", "trade_date": "20240102"})
    assert calls[2] == ("trade_cal", {"exchange": "SSE", "start_date": "20240101", "end_date": "20240131"})
