from __future__ import annotations

import logging
import time

from quant.data import db, ingest, schemas
from quant.data.tushare_client import TushareClient
from quant.utils.dates import to_yyyymmdd

logger = logging.getLogger(__name__)


def tables_to_truncate(all_tables: bool = False) -> list[str]:
    """返回将要清空的表名。all_tables=True 时返回当前库全部基础表（含非本系统表）。"""
    if all_tables:
        df = db.read_df(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE='BASE TABLE' "
            "ORDER BY TABLE_NAME"
        )
        return df["TABLE_NAME"].tolist()
    return list(schemas.TABLES)


def truncate_tables(names: list[str] | None = None,
                    all_tables: bool = False) -> list[str]:
    """清空指定表的数据（保留表结构）；names 缺省时按 all_tables 规则选取。

    返回实际清空的表名列表，便于调用方确认清空范围。
    """
    targets = (list(names) if names is not None
               else tables_to_truncate(all_tables))
    for name in targets:
        logger.info("清空表 %s ...", name)
        db.execute(f"TRUNCATE TABLE `{name}`")
    logger.info("已清空 %d 张表：%s", len(targets), ", ".join(targets))
    return targets


def _rebuild_table(table: str, from_date: str | None, client: TushareClient | None,
                   continue_on_error: bool) -> int | str:
    logger.info("开始重建 %s（from_date=%s）", table, from_date or "全量快照")
    started = time.monotonic()
    try:
        if from_date is None:
            rows = ingest.update(table, client=client)
        else:
            rows = ingest.update(table, from_date=from_date, client=client)
    except Exception as exc:  # noqa: BLE001 - 单表失败不阻断整体重建
        if not continue_on_error:
            raise
        logger.exception("重建 %s 失败：%s", table, exc)
        value: int | str = f"失败: {exc}"
    else:
        value = rows
    logger.info("结束重建 %s，耗时 %.1f 秒", table, time.monotonic() - started)
    return value


def rebuild(market_start: str = "20180101", holdertrade_start: str | None = None,
            client: TushareClient | None = None,
            continue_on_error: bool = True,
            resume: bool = False) -> dict[str, int | str]:
    """全量重建：先刷快照表，再按日期区间重拉行情与股东增减持。

    resume=True 时（配合 --skip-truncate 使用）行情/增减持表按 ingest_log
    水位线断点续跑：有水位线的表传 from_date=None 交给 ingest.update 从水位线
    继续；没有水位线的表从 market_start/holdertrade_start 拉取；快照表始终
    整体刷新。每张表独立计时并记录日志；continue_on_error=True 时单表异常记为
    ``失败: ...`` 并继续，False 时直接抛出。
    """
    market_start = to_yyyymmdd(market_start)
    holdertrade_start = (to_yyyymmdd(holdertrade_start) if holdertrade_start
                         else market_start)
    results: dict[str, int | str] = {}
    for table in ingest.FULL_REFRESH_ORDER:
        results[table] = _rebuild_table(table, None, client, continue_on_error)
    for table in ingest.SPECS:
        if table in ingest.FULL_REFRESH_ORDER:
            continue
        start = (holdertrade_start if table == "stk_holdertrade"
                 else market_start)
        from_date = None if resume and ingest.get_watermark(table) else start
        results[table] = _rebuild_table(table, from_date, client,
                                        continue_on_error)
    return results
