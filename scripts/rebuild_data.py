"""夜间全量重建：清空系统表数据并从 Tushare 重新全量入库。

典型用法（非交易时段执行，整体约 2 小时）：

    uv run python scripts/rebuild_data.py --yes

默认行为：
- 清空 schemas.TABLES 中全部系统表的数据（保留表结构），包括 ingest_log
  水位线，确保后续从零重新拉取；
- 行情/交易类数据从 2018-01-01 起（--market-start 可调），股东增减持默认
  同一起点（--holdertrade-start 可调，该接口限速 90 次/分钟）；
- 预计约 1.2 万次行情调用（150 次/分钟）+ 约 3000 个自然日的增减持调用。

安全提示：
- 不加 --yes 时会列出将被清空的表并要求输入 YES 确认；
- --all-tables 会清空当前数据库中的全部基础表（含非本系统表），请谨慎使用；
- 中断后可用 --skip-truncate 跳过清空，从水位线续跑补齐。
"""
from __future__ import annotations

import argparse
import sys
import time

from quant import maintenance
from quant.data import db, schemas
from quant.data.tushare_client import TushareClient
from quant.utils.logging import setup_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()

    tables = maintenance.tables_to_truncate(all_tables=args.all_tables)
    if not args.skip_truncate and not args.yes:
        if not confirm(tables, args.all_tables):
            print("已取消，未做任何修改。")
            return 0

    started = time.monotonic()
    schemas.create_all()
    schemas.migrate()
    if not args.skip_truncate:
        maintenance.truncate_tables(all_tables=args.all_tables)

    client = TushareClient(calls_per_minute=args.rate)
    results = maintenance.rebuild(market_start=args.market_start,
                                  holdertrade_start=args.holdertrade_start,
                                  client=client)
    print_summary(results, time.monotonic() - started)
    return 1 if any(isinstance(value, str) for value in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
