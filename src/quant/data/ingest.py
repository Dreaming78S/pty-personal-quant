from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from quant.data import db, schemas
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
    TushareClient,
)
from quant.utils.dates import calendar_days_between

BENCHMARK_INDEXES = ("000300.SH",)
BATCH_DATES = 20

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiSpec:
    table: str
    api: str
    mode: str  # "by_date" | "by_calendar_day" | "full"
    index_codes: tuple[str, ...] = ()
    fields: str = ""
    default_start: str = "19900101"


SPECS: dict[str, ApiSpec] = {
    "daily": ApiSpec("daily", "daily", "by_date", fields=DAILY_FIELDS),
    "adj_factor": ApiSpec("adj_factor", "adj_factor", "by_date",
                          fields=ADJ_FACTOR_FIELDS),
    "daily_basic": ApiSpec("daily_basic", "daily_basic", "by_date",
                           fields=DAILY_BASIC_FIELDS),
    "suspend_d": ApiSpec("suspend_d", "suspend_d", "by_date",
                         fields=SUSPEND_FIELDS),
    "stk_limit": ApiSpec("stk_limit", "stk_limit", "by_date",
                         fields=STK_LIMIT_FIELDS),
    "index_daily": ApiSpec("index_daily", "index_daily", "by_date",
                           index_codes=BENCHMARK_INDEXES,
                           fields=INDEX_DAILY_FIELDS),
    "stock_basic": ApiSpec("stock_basic", "stock_basic", "full",
                           fields=STOCK_BASIC_FIELDS),
    "trade_cal": ApiSpec("trade_cal", "trade_cal", "full",
                         fields=TRADE_CAL_FIELDS),
    "namechange": ApiSpec("namechange", "namechange", "full",
                          fields=NAMECHANGE_FIELDS),
    "stock_company": ApiSpec("stock_company", "stock_company", "full",
                             fields=STOCK_COMPANY_FIELDS),
    "new_share": ApiSpec("new_share", "new_share", "full",
                         fields=NEW_SHARE_FIELDS),
    "stk_holdertrade": ApiSpec("stk_holdertrade", "stk_holdertrade",
                               "by_calendar_day", fields=HOLDERTRADE_FIELDS,
                               default_start="20150101"),
}

FULL_REFRESH_ORDER = ("trade_cal", "stock_basic", "namechange",
                      "stock_company", "new_share")


def get_watermark(table: str) -> str | None:
    return db.scalar(
        "SELECT last_trade_date FROM ingest_log WHERE task_name=%s", (table,)
    )


def set_watermark(table: str, trade_date: str) -> None:
    db.execute(
        "INSERT INTO ingest_log (task_name, last_trade_date) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE last_trade_date=VALUES(last_trade_date)",
        (table, trade_date),
    )


def trade_dates_between(start: str, end: str) -> list[str]:
    df = db.read_df(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 "
        "AND cal_date>=%s AND cal_date<=%s ORDER BY cal_date",
        (start, end),
    )
    return df["cal_date"].tolist()


def latest_trade_date() -> str | None:
    today = date.today().strftime("%Y%m%d")
    return db.scalar(
        "SELECT MAX(cal_date) FROM trade_cal WHERE exchange='SSE' "
        "AND is_open=1 AND cal_date<=%s",
        (today,),
    )


def _prepare(df: pd.DataFrame, table: str) -> pd.DataFrame:
    cols = [c for c in schemas.columns_of(table) if c in df.columns]
    return df[cols].copy()


def _fetch_by_date(client: TushareClient, spec: ApiSpec, trade_date: str) -> pd.DataFrame:
    if spec.index_codes:
        frames = [client.call(spec.api, ts_code=code, trade_date=trade_date,
                              fields=spec.fields)
                  for code in spec.index_codes]
        return pd.concat(frames, ignore_index=True)
    return client.call(spec.api, trade_date=trade_date, fields=spec.fields)


def _fetch_by_calendar_day(client: TushareClient, spec: ApiSpec,
                           day: str) -> pd.DataFrame:
    return client.call(spec.api, ann_date=day, fields=spec.fields)


def update(table: str, from_date: str | None = None, to_date: str | None = None,
           client: TushareClient | None = None) -> int:
    """更新单张表；日期表按水位线增量，full 表整体 upsert。幂等、可续跑。"""
    if table not in SPECS:
        raise KeyError(f"未知数据表：{table}")
    spec = SPECS[table]
    client = client or TushareClient()

    if spec.mode == "full":
        if table == "trade_cal":
            df = client.call(spec.api, exchange="SSE",
                             start_date="19900101", end_date="20301231")
        elif table == "stock_basic":
            df = client.fetch_stock_basic()
        elif table == "stock_company":
            df = client.fetch_stock_company()
        elif table == "new_share":
            df = client.fetch_new_share()
        else:
            df = client.call(spec.api, fields=spec.fields)
        prepared = _prepare(df, table)
        n = db.upsert_df(table, prepared)
        set_watermark(table, to_date or latest_trade_date() or "")
        return n

    existing = get_watermark(table) or ""
    start = from_date or existing or spec.default_start
    if spec.mode == "by_calendar_day":
        end = to_date or date.today().strftime("%Y%m%d")
        dates = calendar_days_between(start, end)
    else:
        end = to_date or latest_trade_date()
        if end is None:
            return 0
        dates = trade_dates_between(start, end)
    if existing and not from_date:
        dates = [d for d in dates if d > existing]

    total = 0
    watermark = existing
    for i in range(0, len(dates), BATCH_DATES):
        batch = dates[i:i + BATCH_DATES]
        if spec.mode == "by_calendar_day":
            logger.info("%s: %s ~ %s", table, batch[0], batch[-1])
            frames = []
            for d in batch:
                df = _prepare(_fetch_by_calendar_day(client, spec, d), table)
                df = df.dropna(subset=["change_vol"])
                if not df.empty:
                    frames.append(df)
        else:
            frames = [_prepare(_fetch_by_date(client, spec, d), table) for d in batch]
            frames = [f for f in frames if not f.empty]
        if frames:
            total += db.upsert_df(table, pd.concat(frames, ignore_index=True))
        # 手工回补旧数据不能把水位线往回拨，否则下一轮会重复拉取
        watermark = max(watermark, batch[-1])
        set_watermark(table, watermark)
    return total


def update_all(from_date: str | None = None, to_date: str | None = None,
               client: TushareClient | None = None) -> dict[str, int]:
    """先刷参考表/快照表（trade_cal/stock_basic/namechange/stock_company/new_share），再增量日期表。"""
    client = client or TushareClient()
    results: dict[str, int] = {}
    for table in FULL_REFRESH_ORDER:
        results[table] = update(table, client=client)
    for table in SPECS:
        if table not in FULL_REFRESH_ORDER:
            results[table] = update(table, from_date=from_date, to_date=to_date,
                                    client=client)
    return results
