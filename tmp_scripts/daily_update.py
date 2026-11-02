"""交易日盘后一键更新：行情增量入库 → 命中表增量写入 → 状态校验（数据库 + 本地缓存） → 飞书推送。

用法（建议交易日 18:00 后运行，可挂到任务计划程序）：
    uv run python tmp_scripts/daily_update.py
    uv run python tmp_scripts/daily_update.py --skip-notify   # 只更新与校验，不推送

流程（前 4 步幂等、可中断重跑，重跑不会重复或损坏数据）：
1. quant data update 等价：快照表整体刷新 + 日频表按水位线增量；
2. quant hits update 等价：增量写入 hit_<策略>（内部先刷新行情类本地缓存）；
3. quant limit update 等价：增量写入主板涨停、跌停股表；
4. 校验行情/派生表水位线是否追平最新交易日，刷新过期的本地 Parquet 缓存，未通过返回非零退出码；
5. quant notify 等价：默认推送三张卡（今日最终推荐 + 趋势跌停/涨停洗盘 + 多策略共振）；
   非交易日自动跳过推送，推送失败返回非零退出码
   （数据已更新完成，可单独运行 uv run quant notify 补推）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.chdir(Path(__file__).resolve().parents[1])

from quant.data import cache, db, ingest, schemas  # noqa: E402
from quant.engine import hits, limits, notify  # noqa: E402
from quant.utils.logging import setup_logging  # noqa: E402

logger = logging.getLogger(__name__)

CHECK_TABLES = ("trade_cal", "stock_basic", "daily", "adj_factor", "daily_basic",
                "suspend_d", "stk_limit", "index_daily", "namechange")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-notify", action="store_true",
                        help="只做数据更新与校验，不推送飞书卡片")
    return parser.parse_args()


def _watermarks() -> dict[str, str]:
    rows = db.read_df("SELECT task_name, last_trade_date FROM ingest_log")
    return dict(zip(rows["task_name"].astype(str),
                    rows["last_trade_date"].fillna("-").astype(str)))


def _banner(step: str) -> None:
    print()
    print("=" * 64)
    print(step)
    print("=" * 64)


def _check_cache(marks: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """刷新过期缓存并返回（已刷新、无缓存、刷新失败）表名。"""
    refreshed: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    for name in (n for n in schemas.TABLES if n != "ingest_log"):
        path = cache.cache_path(name)
        if not path.exists():
            missing.append(name)
            continue
        cached = cache._read_meta(name).get("watermark")
        if cached == marks.get(name):
            continue
        try:
            cache.ensure_cache(name)
            refreshed.append(name)
        except Exception as exc:  # noqa: BLE001 - 单表刷新失败不阻断检查
            failed.append(f"{name}（{exc}）")
    return refreshed, missing, failed


def main(skip_notify: bool = False) -> int:
    setup_logging()
    started = time.monotonic()

    _banner("第 1 步 / 5：从 Tushare 增量更新行情数据")
    try:
        results = ingest.update_all()
    except Exception as exc:  # noqa: BLE001 - 明确提示可重跑
        logger.exception("行情更新失败")
        print(f"\n行情更新失败：{exc}")
        print("已入库部分不受影响，水位线未推进；请稍后直接重跑本脚本。")
        return 1
    for name, rows in results.items():
        print(f"  {name}: 已写入 {rows} 行")

    _banner("第 2 步 / 5：增量计算并写入各策略命中表")
    try:
        hit_rows = hits.fill_hits("all")
    except Exception as exc:  # noqa: BLE001 - 明确提示可重跑
        logger.exception("命中表更新失败")
        print(f"\n命中表更新失败：{exc}")
        print("行情已入库；请修复后重跑本脚本，命中表会从水位线续写。")
        return 1
    for name, rows in hit_rows.items():
        print(f"  hit_{name}: 已写入 {rows} 行")

    _banner("第 3 步 / 5：增量计算并写入主板涨停、跌停股表")
    try:
        limit_rows = limits.fill_limits()
    except Exception as exc:  # noqa: BLE001 - 明确提示可重跑
        logger.exception("涨跌停表更新失败")
        print(f"\n涨跌停表更新失败：{exc}")
        print("行情与命中表已更新；请修复后重跑本脚本，涨跌停表会从水位线续写。")
        return 1
    for name, rows in limit_rows.items():
        print(f"  {name}: 已写入 {rows} 行")

    _banner("第 4 步 / 5：状态校验（数据库 + 本地缓存）")
    latest = ingest.latest_trade_date()
    marks = _watermarks()
    print(f"  最新交易日（<= 今天）：{latest}")
    for name in CHECK_TABLES:
        print(f"  {name}: 水位线 {marks.get(name, '-')}")
    unfinished = []
    for table in [*schemas.HIT_TABLES, *schemas.LIMIT_TABLES]:
        mark = marks.get(table, "-")
        ok = mark == latest
        if not ok:
            unfinished.append(table)
        print(f"  {table}: 水位线 {mark} {'OK' if ok else '未追平'}")
    missing_data = [n for n in CHECK_TABLES if marks.get(n) != latest]

    refreshed, missing_cache, cache_failed = _check_cache(marks)
    mirrored = len(schemas.TABLES) - 1 - len(missing_cache)
    print(f"  本地缓存：{mirrored} 张已镜像，"
          f"{len(missing_cache)} 张缺失（后续命令会自动创建）")
    if refreshed:
        print(f"  已刷新过期缓存：{', '.join(refreshed)}")
    if missing_cache:
        print(f"  暂无缓存：{', '.join(missing_cache)}")

    elapsed = time.monotonic() - started
    if unfinished or missing_data or cache_failed:
        print()
        print(f"未完成：派生表 {unfinished or '无'}，行情表 {missing_data or '无'}，"
              f"缓存刷新失败 {cache_failed or '无'}；用时 {elapsed:.1f} 秒，请重跑本脚本。")
        return 1

    if skip_notify:
        print()
        print(f"前 4 步完成：行情、{len(schemas.HIT_TABLES)} 张命中表与 "
              f"{len(schemas.LIMIT_TABLES)} 张涨跌停表均已追平最新交易日 {latest}，"
              f"用时 {elapsed:.1f} 秒；已跳过飞书推送（--skip-notify）。")
        return 0

    today = datetime.now().strftime("%Y%m%d")
    if latest != today:
        print()
        print(f"今天（{today}）不是交易日，跳过飞书推送"
              f"（如需手动补推请运行 uv run quant notify）。用时 {elapsed:.1f} 秒。")
        return 0

    _banner("第 5 步 / 5：飞书推送当日卡片")
    try:
        statuses = notify.notify_hits()
    except Exception as exc:  # noqa: BLE001 - 数据已完成，推送可单独补跑
        logger.exception("飞书推送失败")
        print(f"\n飞书推送失败：{exc}")
        print("数据更新与校验已完成；修复后可单独运行 uv run quant notify 补推。")
        return 1
    failed = []
    for name, status in statuses.items():
        print(f"  {name}: {status}")
        if status.startswith("失败"):
            failed.append(name)

    elapsed = time.monotonic() - started
    print()
    if failed:
        print(f"推送失败：{'、'.join(failed)}；"
              f"可运行 uv run quant notify -s <策略> 补推。用时 {elapsed:.1f} 秒。")
        return 1
    print(f"全部完成：行情、{len(schemas.HIT_TABLES)} 张命中表与 "
          f"{len(schemas.LIMIT_TABLES)} 张涨跌停表均已追平最新交易日 {latest}，"
          f"已推送 {len(statuses)} 张卡片，用时 {elapsed:.1f} 秒。")
    return 0


if __name__ == "__main__":
    sys.exit(main(**vars(_parse_args())))
