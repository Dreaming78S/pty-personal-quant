from __future__ import annotations

import argparse
import logging
import time

from quant.data import db, ingest, schemas
from quant.data.tushare_client import TushareClient
from quant.utils.dates import to_yyyymmdd
from quant.utils.logging import setup_logging

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


def parse_rebuild_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="夜间全量重建：清空系统表数据并从 Tushare 重新入库")
    parser.add_argument("--market-start", default="2018-01-01",
                        help="行情/交易类数据起始日期，默认 2018-01-01")
    parser.add_argument("--holdertrade-start", default=None,
                        help="股东增减持起始日期，默认与 --market-start 相同")
    parser.add_argument("--all-tables", action="store_true",
                        help="清空当前数据库的全部表（含非本系统表，危险）")
    parser.add_argument("--skip-truncate", action="store_true",
                        help="不清空数据，直接从现有水位线续跑/补齐")
    parser.add_argument("--yes", action="store_true",
                        help="跳过交互确认（适合无人值守的夜间任务）")
    parser.add_argument("--rate", type=int, default=150,
                        help="Tushare 普通接口调用速率上限（次/分钟，默认 150）")
    return parser.parse_args(argv)


def confirm(tables: list[str], all_tables: bool) -> bool:
    print("即将清空以下表的数据（表结构保留）：")
    for name in tables:
        print(f"  - {name}")
    if all_tables:
        print("警告：--all-tables 将清空当前数据库中的全部表，"
              "包括非本系统的表，且无法恢复！")
    answer = input("确认继续请输入 YES（其他任意输入取消）：")
    return answer.strip() == "YES"


def print_summary(results: dict[str, int | str], elapsed: float) -> None:
    print("\n各表重建结果：")
    for name, value in results.items():
        if isinstance(value, str):
            print(f"  {name}: {value}")
        else:
            print(f"  {name}: 写入 {value} 行")
    print(f"\n总耗时：{elapsed:.1f} 秒")
    print("\n各表当前行数：")
    for name in schemas.TABLES:
        try:
            count = db.scalar(f"SELECT COUNT(*) FROM `{name}`")
        except Exception as exc:  # noqa: BLE001 - 行数查询失败不影响退出码
            count = f"查询失败：{exc}"
        print(f"  {name}: {count}")


def run_rebuild_cli(argv: list[str] | None = None) -> int:
    """夜间全量重建 CLI；返回进程退出码（0 成功，1 有表失败，2 参数错误）。"""
    args = parse_rebuild_args(argv)
    setup_logging()

    try:
        market_start = to_yyyymmdd(args.market_start)
        holdertrade_start = (to_yyyymmdd(args.holdertrade_start)
                             if args.holdertrade_start else None)
    except ValueError as exc:
        print(f"参数错误：{exc}")
        return 2
    if args.rate < 1:
        print("参数错误：--rate 必须为不小于 1 的整数")
        return 2

    client = TushareClient(calls_per_minute=args.rate)

    names: list[str] = []
    if not args.skip_truncate:
        names = tables_to_truncate(all_tables=args.all_tables)
        if not args.yes and not confirm(names, args.all_tables):
            print("已取消，未做任何修改。")
            return 0

    started = time.monotonic()
    schemas.create_all()
    schemas.migrate()
    if not args.skip_truncate:
        truncate_tables(names)

    results = rebuild(market_start=market_start,
                      holdertrade_start=holdertrade_start,
                      client=client,
                      resume=args.skip_truncate)
    print_summary(results, time.monotonic() - started)
    return 1 if any(isinstance(value, str) for value in results.values()) else 0
