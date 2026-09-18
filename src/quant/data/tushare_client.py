from __future__ import annotations

import datetime
import time
from typing import Any, Callable

import pandas as pd

from quant.config import get_settings

STOCK_BASIC_FIELDS = ("ts_code,symbol,name,area,industry,market,exchange,"
                      "list_status,list_date,delist_date,is_hs,cnspell")
TRADE_CAL_FIELDS = "exchange,cal_date,is_open,pretrade_date"
DAILY_FIELDS = ("ts_code,trade_date,open,high,low,close,pre_close,"
                "change,pct_chg,vol,amount")
ADJ_FACTOR_FIELDS = "ts_code,trade_date,adj_factor"
DAILY_BASIC_FIELDS = ("ts_code,trade_date,turnover_rate,turnover_rate_f,"
                      "volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,"
                      "total_share,float_share,free_share,total_mv,circ_mv,"
                      "limit_status")
SUSPEND_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
STK_LIMIT_FIELDS = "ts_code,trade_date,up_limit,down_limit"
INDEX_DAILY_FIELDS = ("ts_code,trade_date,open,high,low,close,pre_close,"
                      "change,pct_chg,vol,amount")
NAMECHANGE_FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
STOCK_COMPANY_FIELDS = ("ts_code,com_name,com_id,exchange,chairman,manager,"
                        "secretary,reg_capital,setup_date,province,city,"
                        "introduction,website,email,office,employees,"
                        "main_business,business_scope")
NEW_SHARE_FIELDS = ("ts_code,sub_code,name,ipo_date,issue_date,amount,"
                    "market_amount,price,pe,limit_amount,funds,ballot")
HOLDERTRADE_FIELDS = ("ts_code,ann_date,holder_name,holder_type,in_de,"
                      "change_vol,change_ratio,after_share,after_ratio,"
                      "avg_price,total_share,begin_date,close_date")

API_RATE_LIMITS: dict[str, int] = {
    "stk_holdertrade": 90,  # 接口上限 100/分钟，留余量
}


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
                 max_retries: int = 3, pro: Any | None = None,
                 api_rate_limits: dict[str, int] | None = None):
        self._token = token or get_settings().tushare_token
        self._api_rate_limits = {**API_RATE_LIMITS, **(api_rate_limits or {})}
        self._limiters: dict[str, RateLimiter] = {}
        self._default_limiter = RateLimiter(calls_per_minute)
        self._max_retries = max_retries
        self._pro = pro

    def _limiter_for(self, api_name: str) -> RateLimiter:
        per_minute = self._api_rate_limits.get(api_name)
        if per_minute is None:
            return self._default_limiter
        limiter = self._limiters.get(api_name)
        if limiter is None:
            limiter = RateLimiter(per_minute)
            self._limiters[api_name] = limiter
        return limiter

    @property
    def pro(self):
        if self._pro is None:
            import tushare as ts

            self._pro = ts.pro_api(self._token)
        return self._pro

    def call(self, api_name: str, **kwargs) -> pd.DataFrame:
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            self._limiter_for(api_name).acquire()
            try:
                return self.pro.query(api_name, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 需要重试所有接口异常
                last_err = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Tushare 接口 {api_name} 调用失败: {last_err}")

    def fetch_daily(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily", trade_date=trade_date, fields=DAILY_FIELDS)

    def fetch_adj_factor(self, trade_date: str) -> pd.DataFrame:
        return self.call("adj_factor", trade_date=trade_date,
                         fields=ADJ_FACTOR_FIELDS)

    def fetch_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily_basic", trade_date=trade_date,
                         fields=DAILY_BASIC_FIELDS)

    def fetch_suspend_d(self, trade_date: str) -> pd.DataFrame:
        return self.call("suspend_d", trade_date=trade_date,
                         fields=SUSPEND_FIELDS)

    def fetch_stk_limit(self, trade_date: str) -> pd.DataFrame:
        return self.call("stk_limit", trade_date=trade_date,
                         fields=STK_LIMIT_FIELDS)

    def fetch_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        return self.call("index_daily", ts_code=ts_code, trade_date=trade_date,
                         fields=INDEX_DAILY_FIELDS)

    def fetch_stock_basic(self) -> pd.DataFrame:
        # list_status="" 等价默认 L，且默认字段不含 list_status/delist_date，
        # 需按状态分别请求并显式指定 fields，否则退市股缺失、字段全为空
        frames = [
            self.call("stock_basic", exchange="", list_status=status,
                      fields=STOCK_BASIC_FIELDS)
            for status in ("L", "D", "P")
        ]
        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset="ts_code", keep="last")
        return df.reset_index(drop=True)

    def fetch_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self.call("trade_cal", exchange="SSE",
                         start_date=start_date, end_date=end_date,
                         fields=TRADE_CAL_FIELDS)

    def fetch_namechange(self) -> pd.DataFrame:
        return self.call("namechange", fields=NAMECHANGE_FIELDS)

    def fetch_stock_company(self) -> pd.DataFrame:
        frames = []
        for exchange in ("SSE", "SZSE", "BSE"):
            frame = self.call("stock_company", exchange=exchange,
                              fields=STOCK_COMPANY_FIELDS)
            if frame.empty:
                raise RuntimeError(
                    f"stock_company {exchange} 返回空数据，疑似限流或接口异常；"
                    "已中止，请稍后重跑")
            frames.append(frame)
        df = pd.concat(frames, ignore_index=True)
        if not df.empty:
            df = df.drop_duplicates(subset="ts_code", keep="last")
        return df.reset_index(drop=True)

    def fetch_new_share(self) -> pd.DataFrame:
        frames = [
            self.call("new_share", start_date=f"{year}0101",
                      end_date=f"{year}1231", fields=NEW_SHARE_FIELDS)
            for year in range(1990, datetime.date.today().year + 1)
        ]
        df = pd.concat(frames, ignore_index=True)
        if df.empty:
            raise RuntimeError(
                "new_share 全部年份返回空数据，疑似限流或接口异常；"
                "已中止，请稍后重跑")
        df = df.drop_duplicates(subset="ts_code", keep="last")
        return df.reset_index(drop=True)
