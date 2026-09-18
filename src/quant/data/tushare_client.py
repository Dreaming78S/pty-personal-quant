from __future__ import annotations

import time
from typing import Any, Callable

import pandas as pd

from quant.config import get_settings


class RateLimiter:
    """简单间隔限流：保证两次调用之间至少间隔 60/per_minute 秒。"""

    def __init__(self, per_minute: int, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._interval = 60.0 / per_minute
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def acquire(self) -> None:
        now = self._clock()
        if self._last is not None:
            wait = self._last + self._interval - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last = now


class TushareClient:
    """Tushare 封装：限流、指数退避重试、常用接口快捷方法。"""

    def __init__(self, token: str | None = None, calls_per_minute: int = 150,
                 max_retries: int = 3, pro: Any | None = None):
        self._token = token or get_settings().tushare_token
        self._limiter = RateLimiter(calls_per_minute)
        self._max_retries = max_retries
        self._pro = pro

    @property
    def pro(self):
        if self._pro is None:
            import tushare as ts

            self._pro = ts.pro_api(self._token)
        return self._pro

    def call(self, api_name: str, **kwargs) -> pd.DataFrame:
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            self._limiter.acquire()
            try:
                return self.pro.query(api_name, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 需要重试所有接口异常
                last_err = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Tushare 接口 {api_name} 调用失败: {last_err}")

    def fetch_daily(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily", trade_date=trade_date)

    def fetch_adj_factor(self, trade_date: str) -> pd.DataFrame:
        return self.call("adj_factor", trade_date=trade_date)

    def fetch_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily_basic", trade_date=trade_date)

    def fetch_suspend_d(self, trade_date: str) -> pd.DataFrame:
        return self.call("suspend_d", trade_date=trade_date)

    def fetch_stk_limit(self, trade_date: str) -> pd.DataFrame:
        return self.call("stk_limit", trade_date=trade_date)

    def fetch_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        return self.call("index_daily", ts_code=ts_code, trade_date=trade_date)

    def fetch_stock_basic(self) -> pd.DataFrame:
        # 空字符串返回 L/D/P 全部状态，退市股也要有 list_date 供次新过滤使用
        return self.call("stock_basic", exchange="", list_status="")

    def fetch_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self.call("trade_cal", exchange="SSE",
                         start_date=start_date, end_date=end_date)

    def fetch_namechange(self) -> pd.DataFrame:
        return self.call("namechange")
