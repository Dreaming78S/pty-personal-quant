"""夜间全量重建：清空系统表数据并从 Tushare 重新全量入库。

典型用法（非交易时段执行，整体约 2 小时）：

    uv run python tmp_scripts/rebuild_data.py --yes

默认行为：
- 清空 schemas.TABLES 中全部系统表的数据（保留表结构），包括 ingest_log
  水位线，确保后续从零重新拉取；
- 行情/交易类数据从 2018-01-01 起（--market-start 可调），股东增减持默认
  同一起点（--holdertrade-start 可调，该接口限速 90 次/分钟）；
- 预计约 1.2 万次行情调用（150 次/分钟）+ 约 3000 个自然日的增减持调用。

安全提示：
- 不加 --yes 时会列出将被清空的表并要求输入 YES 确认；
- --all-tables 会清空当前数据库中的全部基础表（含非本系统表），请谨慎使用；
- 日期参数、--rate 与 Tushare 客户端均在清空之前校验/构造，参数写错不会清库；
- 中断后可用 --skip-truncate 跳过清空，行情/增减持表按 ingest_log 水位线
  断点续跑（没有水位线的表从起始日期拉取），快照表仍整体刷新。

实现位于 ``quant.maintenance``；本脚本只保留薄入口，便于本地定时任务调用。
"""
from __future__ import annotations

import sys

from quant import maintenance

# 兼容旧的脚本调用点
parse_args = maintenance.parse_rebuild_args
confirm = maintenance.confirm
print_summary = maintenance.print_summary


def main(argv: list[str] | None = None) -> int:
    return maintenance.run_rebuild_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
