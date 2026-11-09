"""limit_up_shakeout 现金收益网格搜索。

搜索维度：
1. 止盈分档：T+1 收盘对买入价涨幅 → T+2 止盈率的分段函数（左开右闭）。
   含固定阈值、全正分档、含止损分档、低段开盘卖、深跌小反弹等多族候选。
2. T+1 买入低开区间：按原始价 `(open_raw(T+1) - close_raw(T)) / close_raw(T)`
   划分，支持全区间、高开/低开分区、低开嵌套区间。
3. 是否要求 T 日最低价支撑：后复权 `low(T) >= close(T-1)`，True / False。

固定条件（与 limitup_shakeout_returns.py 默认口径一致）：
- T 日高开：`raw_open(T) / raw_pre_close(T) - 1 ∈ [0%, 11%]`
- T 日收盘涨跌幅：`raw_close(T) / raw_pre_close(T) - 1 ∈ [0%, 11%]`
- 近 N 日涨幅：默认不限
- 买入：T+1 原始开盘价（delta=0，不再搜索限价深度）

输出：
- `grid_results.csv`：全部组合（分档 × 低开区间 × 支撑条件）的汇总指标
- `tier_main_effects.csv`：按止盈分档汇总的主效应
- `entry_gap_main_effects.csv`：按低开区间汇总的主效应
- `family_main_effects.csv`：按分档族汇总的主效应
- `low_support_effects.csv`：同一（分档 × 低开区间）下，要求/不要求支撑的配对差异
- `low_support_summary.csv`：按低开区间汇总的支撑效应
- `best_by_entry_support.csv`：每个（低开区间 × 支撑条件）下的最佳分档
- `yearly_stats.csv` / `tier_breakdown.csv` / `sell_mode_breakdown.csv`
- `all_trades.csv`：Top-N 组合 + 基准组合的逐笔明细
- `run_meta.json` / `report.md`

用法：
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py --start 2024-01-01 --end latest
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py --no-report
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py --min-samples 100
    uv run python tmp_scripts/limitup_shakeout_cash_grid_search.py --top-trades 30

注意：本脚本是样本内网格搜索。组合数量很大，排名靠前的组合存在过拟合/多重检验风险；
应优先看分档族主效应、低开区间主效应和年度稳定性，而不是只看单个最优组合。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TMP_DIR))
os.chdir(ROOT)

from quant.data import db  # noqa: E402
from quant.engine.exit_tiers import (  # noqa: E402
    DEFAULT_EXIT_TIERS,
    format_tiers_summary,
    tier_labels,
    tiers_from_scalar,
    tiers_to_jsonable,
    validate_exit_tiers,
)

# 合并后的模块同时保留原 cash / filtered 两套函数名，这里统一用 cash 别名
import limitup_shakeout_returns as cash  # noqa: E402

# ==============================================================================
# 固定筛选参数（与 limitup_shakeout_returns.py 的默认口径一致）
DEFAULT_START = "20181101"
DEFAULT_END = "latest"
DEFAULT_GAP = (0.00, 0.11)
DEFAULT_CLOSE_CHG = (0.0, 0.11)
DEFAULT_N_DAYS_RISE = "unlimited"
# 原策略默认要求最低价支撑；本网格同时搜索 True/False 两个取值。
DEFAULT_REQUIRE_LOW_SUPPORT = True

# 现金口径：与 cash_trades.csv 默认运行参数保持一致
DEFAULT_POSITION = 100_000.0
DEFAULT_LOT_MODE = "floor"
DEFAULT_MAX_DELAY_DAYS = 10
DEFAULT_COMMISSION = cash.DEFAULT_COMMISSION
DEFAULT_MIN_COMMISSION = cash.DEFAULT_MIN_COMMISSION
DEFAULT_STAMP = cash.DEFAULT_STAMP
DEFAULT_TRANSFER = cash.DEFAULT_TRANSFER
DEFAULT_SLIPPAGE = cash.DEFAULT_SLIPPAGE
DEFAULT_GENERATE_REPORT = True
DEFAULT_TRADES_TOP_N = 50
MIN_SAMPLES_FOR_RANKING = 100

FAMILY_BASELINE = "现行默认"
FAMILY_FIXED = "固定阈值"
FAMILY_POSITIVE = "全正分档"
FAMILY_STOP = "含止损"
FAMILY_REBOUND = "深跌小反弹"
FAMILY_OPEN = "低段开盘卖"
FAMILY_ORDER = (
    FAMILY_BASELINE,
    FAMILY_FIXED,
    FAMILY_POSITIVE,
    FAMILY_STOP,
    FAMILY_REBOUND,
    FAMILY_OPEN,
)


# ==============================================================================
# 止盈分档设计
# 区间为左开右闭 (lo, hi]，None 表示 ±∞；rate 为 T+2 止盈率（可为负，即止损）。
# ==============================================================================


def tiers_pct(bounds: list[float], rates: list[float]) -> tuple:
    """用“百分点”写分档：bounds 为内部边界，rates 为各段止盈率。

    例：tiers_pct([0, 5, 7], [1, 2, 3, 4])
    → (-∞,0]→1%；(0,5]→2%；(5,7]→3%；(7,+∞)→4%。
    """
    bounds = [float(b) for b in bounds]
    rates = [float(r) for r in rates]
    if len(bounds) != len(rates) - 1:
        raise ValueError(
            f"分档边界数必须等于止盈率数-1：bounds={bounds}, rates={rates}"
        )
    if any(b2 <= b1 for b1, b2 in zip(bounds, bounds[1:])):
        raise ValueError(f"分档边界必须严格递增：{bounds}")
    tiers = []
    for i, rate in enumerate(rates):
        lo = None if i == 0 else bounds[i - 1] / 100.0
        hi = None if i == len(rates) - 1 else bounds[i] / 100.0
        tiers.append((lo, hi, rate / 100.0))
    return tuple(tiers)


# 固定阈值锚点：覆盖 0.25% ~ 8%，用于判断“动态分档是否优于固定阈值”
FIXED_RATES_PCT = [
    0.0,
    0.25,
    0.5,
    0.75,
    1.0,
    1.25,
    1.5,
    2.0,
    2.5,
    3.0,
    4.0,
    5.0,
    6.0,
    8.0,
]

# 全正分档候选：(名称, 内部边界(%), 各段止盈率(%))
POSITIVE_SPECS: list[tuple[str, list[float], list[float]]] = [
    # ---- 2 档：低段/高段 ----
    ("2档_0_1_2", [0], [1, 2]),
    ("2档_0_0.5_2", [0], [0.5, 2]),
    ("2档_0_1.5_2.5", [0], [1.5, 2.5]),
    ("2档_0_1_3", [0], [1, 3]),
    ("2档_0_2_3", [0], [2, 3]),
    ("2档_0_1_4", [0], [1, 4]),
    ("2档_0_0.5_1.5", [0], [0.5, 1.5]),
    ("2档_0_0_2", [0], [0, 2]),
    ("2档_3_1_2", [3], [1, 2]),
    ("2档_5_1_2", [5], [1, 2]),
    ("2档_5_1_3", [5], [1, 3]),
    ("2档_7_1_3", [7], [1, 3]),
    ("2档_7_1_4", [7], [1, 4]),
    ("2档_10_1_3", [10], [1, 3]),
    # ---- 3 档：低/中/高 ----
    ("3档_0_5_1_2_3", [0, 5], [1, 2, 3]),
    ("3档_0_5_0.5_2_3", [0, 5], [0.5, 2, 3]),
    ("3档_0_5_1_2.5_3", [0, 5], [1, 2.5, 3]),
    ("3档_0_5_1_2_4", [0, 5], [1, 2, 4]),
    ("3档_0_7_1_2_3", [0, 7], [1, 2, 3]),
    ("3档_0_7_0.5_2_3", [0, 7], [0.5, 2, 3]),
    ("3档_0_7_1_2_4", [0, 7], [1, 2, 4]),
    ("3档_0_3_1_2_3", [0, 3], [1, 2, 3]),
    ("3档_0_10_1_2_3", [0, 10], [1, 2, 3]),
    ("3档_0_5_1.5_2.5_3.5", [0, 5], [1.5, 2.5, 3.5]),
    ("3档_0_5_0.5_1.5_2.5", [0, 5], [0.5, 1.5, 2.5]),
    ("3档_0_7_0.5_1.5_2.5", [0, 7], [0.5, 1.5, 2.5]),
    ("3档_0_3_0.5_1.5_2.5", [0, 3], [0.5, 1.5, 2.5]),
    ("3档_0_5_0_2_3", [0, 5], [0, 2, 3]),
    # ---- 4 档：低/中/高/极高 ----
    ("4档_0_3_7_1_2_3_4", [0, 3, 7], [1, 2, 3, 4]),
    ("4档_0_5_7_1_2_3_4", [0, 5, 7], [1, 2, 3, 4]),
    ("4档_0_3_5_0.5_1.5_2.5_3.5", [0, 3, 5], [0.5, 1.5, 2.5, 3.5]),
    ("4档_0_5_10_1_2_3_4", [0, 5, 10], [1, 2, 3, 4]),
    ("4档_0_4_8_0.5_1.5_2.5_3.5", [0, 4, 8], [0.5, 1.5, 2.5, 3.5]),
    ("4档_0_3_10_1_2_3_4", [0, 3, 10], [1, 2, 3, 4]),
    ("4档_0_6_10_0.5_1.5_2.5_3.5", [0, 6, 10], [0.5, 1.5, 2.5, 3.5]),
    ("4档_0_5_8_1_2_3_4", [0, 5, 8], [1, 2, 3, 4]),
    # ---- 5 档：更细的高段 ----
    ("5档_0_3_5_7_1_2_3_4_5", [0, 3, 5, 7], [1, 2, 3, 4, 5]),
    ("5档_0_5_7_10_0.5_1.5_2.5_3.5_4.5", [0, 5, 7, 10], [0.5, 1.5, 2.5, 3.5, 4.5]),
]

# 含止损 / 低段特殊处理候选
STOP_SPECS: list[tuple[str, list[float], list[float], str]] = [
    # ---- 3 档：深跌止损 + 低段反弹 + 高段 ----
    ("止损3档_-3_0_-2_1_2", [-3, 0], [-2, 1, 2], FAMILY_STOP),
    ("止损3档_-5_0_-2_1_2", [-5, 0], [-2, 1, 2], FAMILY_STOP),
    ("止损3档_-5_0_-3_1_2", [-5, 0], [-3, 1, 2], FAMILY_STOP),
    ("止损3档_-7_0_-3_1_2", [-7, 0], [-3, 1, 2], FAMILY_STOP),
    ("止损3档_-5_0_-5_1_2", [-5, 0], [-5, 1, 2], FAMILY_STOP),
    ("止损3档_-3_0_-2_1_3", [-3, 0], [-2, 1, 3], FAMILY_STOP),
    # ---- 4 档：止损 + 中段/高段细分 ----
    ("止损4档_-5_0_5_-2_1_2_3", [-5, 0, 5], [-2, 1, 2, 3], FAMILY_STOP),
    ("止损4档_-5_0_7_-2_1_2_3", [-5, 0, 7], [-2, 1, 2, 3], FAMILY_STOP),
    ("止损4档_-3_0_7_-2_1_2_3", [-3, 0, 7], [-2, 1, 2, 3], FAMILY_STOP),
    ("止损4档_-7_0_7_-3_1_2_3", [-7, 0, 7], [-3, 1, 2, 3], FAMILY_STOP),
    ("止损4档_-5_0_5_-5_1_2_3", [-5, 0, 5], [-5, 1, 2, 3], FAMILY_STOP),
    ("止损4档_-5_0_7_-3_1_2_4", [-5, 0, 7], [-3, 1, 2, 4], FAMILY_STOP),
    # ---- 5 档：止损 + 低/中/高/极高 ----
    ("止损5档_-5_0_5_7_-2_1_2_3_4", [-5, 0, 5, 7], [-2, 1, 2, 3, 4], FAMILY_STOP),
    ("止损5档_-5_0_3_7_-2_1_2_3_4", [-5, 0, 3, 7], [-2, 1, 2, 3, 4], FAMILY_STOP),
    ("止损5档_-5_0_5_10_-2_1_2_3_4", [-5, 0, 5, 10], [-2, 1, 2, 3, 4], FAMILY_STOP),
    ("止损5档_-7_0_5_7_-3_1_2_3_4", [-7, 0, 5, 7], [-3, 1, 2, 3, 4], FAMILY_STOP),
    # ---- 深跌段不认赔，要求小反弹 ----
    ("深跌反弹_-5_0_5_1.5_1_2_3", [-5, 0, 5], [1.5, 1, 2, 3], FAMILY_REBOUND),
    ("深跌反弹_-7_0_7_1.5_1_2_3", [-7, 0, 7], [1.5, 1, 2, 3], FAMILY_REBOUND),
    # ---- 低段目标 -10%，近似 T+2 开盘无条件卖出 ----
    ("开盘卖_-10_0_5_-10_2_3", [0, 5], [-10, 2, 3], FAMILY_OPEN),
    ("开盘卖_-10_0_7_-10_2_3", [0, 7], [-10, 2, 3], FAMILY_OPEN),
    ("开盘卖_-10_0_5_7_-10_2_3_4", [0, 5, 7], [-10, 2, 3, 4], FAMILY_OPEN),
]

EXIT_TIERS_GRID: list[tuple[str, tuple, str]] = [
    ("现行默认4档", DEFAULT_EXIT_TIERS, FAMILY_BASELINE),
]
EXIT_TIERS_GRID += [
    (f"固定{rate:g}%", tiers_from_scalar(rate / 100.0), FAMILY_FIXED)
    for rate in FIXED_RATES_PCT
]
EXIT_TIERS_GRID += [
    (name, tiers_pct(bounds, rates), FAMILY_POSITIVE)
    for name, bounds, rates in POSITIVE_SPECS
]
EXIT_TIERS_GRID += [
    (name, tiers_pct(bounds, rates), family)
    for name, bounds, rates, family in STOP_SPECS
]

# 参考分档：用于冻结样本集合。分档只影响卖出规则，不改变筛选样本；
# 但缺失 T+1 收盘价的样本在固定阈值下可交易、在多档下不可交易，故用参考分档统一。
REFERENCE_SCHEME = "现行默认4档"
VERIFY_SCHEMES = ("固定0%", "固定8%")

# T+1 买入低开区间候选：(名称, 区间, 说明)
# 区间为闭区间，按原始价 (open_raw(T+1) - close_raw(T)) / close_raw(T) 判定。
ENTRY_GAP_GRID: list[tuple[str, tuple[float, float], str]] = [
    ("全区间", (-0.11, 0.11), "全部低开/高开样本"),
    ("高开_全部", (0.0, 0.11), "T+1 开盘不低于 T 日收盘"),
    ("高开_0~3", (0.0, 0.03), "轻微高开"),
    ("高开_3~11", (0.03, 0.11), "明显高开"),
    ("低开_全部", (-0.11, 0.0), "全部低开"),
    ("低开_深", (-0.11, -0.06), "深度低开"),
    ("低开_中", (-0.06, -0.02), "中度低开"),
    ("低开_中深", (-0.06, -0.04), "中度低开的深段（嵌套）"),
    ("低开_中浅", (-0.04, -0.02), "中度低开的浅段（嵌套）"),
    ("低开_浅", (-0.02, 0.0), "轻微低开"),
    ("低开_中+浅", (-0.06, 0.0), "中度+轻微低开（嵌套）"),
]

LOW_SUPPORT_OPTIONS: list[tuple[bool, str]] = [
    (True, "要求"),
    (False, "不要求"),
]
LOW_SUPPORT_LABELS = {value: label for value, label in LOW_SUPPORT_OPTIONS}
# ==============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "limit_up_shakeout 现金收益网格搜索："
            "止盈分档 × T+1 买入低开区间 × 是否要求最低价支撑"
        )
    )
    parser.add_argument(
        "--start", default=DEFAULT_START, help=f"命中日起始，默认 {DEFAULT_START}"
    )
    parser.add_argument(
        "--end",
        default=DEFAULT_END,
        help=f"命中日结束，默认 {DEFAULT_END}，即命中表最新日期",
    )
    parser.add_argument(
        "--position",
        type=float,
        default=DEFAULT_POSITION,
        help=f"每个样本投入资金上限，默认 {DEFAULT_POSITION:,.0f}",
    )
    parser.add_argument(
        "--lot-mode",
        choices=("nearest", "floor"),
        default=DEFAULT_LOT_MODE,
        help="手数取整：nearest=最接近预算，floor=不超过预算",
    )
    parser.add_argument(
        "--max-delay-days",
        type=int,
        default=DEFAULT_MAX_DELAY_DAYS,
        help=f"尾盘跌停后最多顺延的交易日数，默认 {DEFAULT_MAX_DELAY_DAYS}",
    )
    parser.add_argument(
        "--commission",
        type=float,
        default=DEFAULT_COMMISSION,
        help=f"佣金率，默认 {DEFAULT_COMMISSION}",
    )
    parser.add_argument(
        "--min-commission",
        type=float,
        default=DEFAULT_MIN_COMMISSION,
        help=f"最低佣金，默认 {DEFAULT_MIN_COMMISSION:,.0f} 元",
    )
    parser.add_argument(
        "--stamp",
        type=float,
        default=DEFAULT_STAMP,
        help=f"印花税率（卖出），默认 {DEFAULT_STAMP}",
    )
    parser.add_argument(
        "--transfer",
        type=float,
        default=DEFAULT_TRANSFER,
        help=f"过户费率（双边），默认 {DEFAULT_TRANSFER}",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=DEFAULT_SLIPPAGE,
        help=f"单边滑点，默认 {DEFAULT_SLIPPAGE}",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=MIN_SAMPLES_FOR_RANKING,
        help=f"排名/报告纳入的最小样本数，默认 {MIN_SAMPLES_FOR_RANKING}",
    )
    parser.add_argument(
        "--top-trades",
        type=int,
        default=DEFAULT_TRADES_TOP_N,
        help=f"all_trades.csv 写出多少个最佳组合，默认 {DEFAULT_TRADES_TOP_N}",
    )
    parser.add_argument(
        "--report",
        dest="generate_report",
        action="store_true",
        help="生成 report.md / CSV（默认开启）",
    )
    parser.add_argument(
        "--no-report",
        dest="generate_report",
        action="store_false",
        help="只打印控制台，不写报告文件",
    )
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument(
        "--output-dir", default="outputs", help="报告输出根目录，默认 outputs"
    )
    return parser.parse_args(argv)


# ==============================================================================
# 格式化与表格
# ==============================================================================


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def fmt_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.2f}"


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.{digits}f}"


def fmt_range(value: tuple[float, float]) -> str:
    return f"[{fmt_pct(value[0], signed=False)}, {fmt_pct(value[1], signed=False)}]"


def md_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_无_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(str(row.get(column, "")) for column in columns) + " |"
        )
    return "\n".join(lines)


def with_retry(fn, desc: str, attempts: int = 3, wait: float = 8.0):
    """带重试的调用包装：本地到 RDS 的链路偶发超时，避免长跑中途报废。"""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络/连接类错误统一重试
            if i == attempts:
                raise
            print(f"  {desc} 第 {i}/{attempts} 次尝试失败：{exc}；{wait:g}s 后重试 ...")
            time.sleep(wait)


# ==============================================================================
# 行情预取与逐笔分析列
# ==============================================================================


def preload_delay_cache(
    frozen_inputs: dict[tuple[str, bool], pd.DataFrame],
    calendar: list[str],
    max_delay_days: int,
) -> dict:
    """预取所有可能触发跌停顺延的 (代码, 日期) 行情行，供 calc_trades 注入。

    候选 = 卖出日（T+2）收盘跌停的样本；该标志来自价格本身、与分档无关，
    是任何组合下顺延查询的完全超集。
    """
    date_pos = {d: i for i, d in enumerate(calendar)}
    wanted: set[tuple[str, str]] = set()
    n_candidates = 0
    for cash_input in frozen_inputs.values():
        candidates = cash_input.loc[
            cash_input["卖出日是否跌停"].astype(bool), ["股票代码", "次次日日期"]
        ]
        n_candidates += len(candidates)
        for code, t2_date in zip(candidates["股票代码"], candidates["次次日日期"]):
            pos = date_pos.get(str(t2_date))
            if pos is None:
                continue
            for date in calendar[pos + 1 : pos + 1 + max_delay_days]:
                wanted.add((str(code), date))
    cache: dict = {}
    if not wanted:
        print("预取跌停顺延行情：无候选样本")
        return cache
    codes = sorted({c for c, _d in wanted})
    dates = sorted({d for _c, d in wanted})
    print(
        f"预取跌停顺延行情：{n_candidates} 个候选样本，"
        f"{len(wanted):,} 个 (代码, 日期) ..."
    )
    for i in range(0, len(codes), 500):
        chunk = codes[i : i + 500]
        code_ph = ",".join(["%s"] * len(chunk))
        date_ph = ",".join(["%s"] * len(dates))
        frame = with_retry(
            lambda: db.read_df(
                "SELECT d.ts_code, d.trade_date, d.open, d.high, d.close, "
                "d.pre_close, a.adj_factor, l.down_limit "
                "FROM daily d "
                "LEFT JOIN adj_factor a ON a.ts_code = d.ts_code "
                "  AND a.trade_date = d.trade_date "
                "LEFT JOIN stk_limit l ON l.ts_code = d.ts_code "
                "  AND l.trade_date = d.trade_date "
                f"WHERE d.ts_code IN ({code_ph}) AND d.trade_date IN ({date_ph})",
                (*chunk, *dates),
            ),
            "预取顺延行情",
        )
        if frame.empty:
            continue
        for row in frame.to_dict("records"):
            cache[(str(row["ts_code"]), str(row["trade_date"]))] = {
                "ts_code": row["ts_code"],
                "trade_date": row["trade_date"],
                **{
                    col: pd.to_numeric(row.get(col), errors="coerce")
                    for col in (
                        "open",
                        "high",
                        "close",
                        "pre_close",
                        "adj_factor",
                        "down_limit",
                    )
                },
            }
    for key in wanted:
        cache.setdefault(key, None)  # 数据库无此行也标记，避免循环阶段回查
    hit = sum(1 for v in cache.values() if v is not None)
    print(f"预取完成：数据库命中 {hit:,} / 需要 {len(wanted):,}")
    return cache


def fetch_by_code_date_pairs(
    table: str,
    columns: list[str],
    pairs,
    chunk_size: int = 5000,
) -> pd.DataFrame:
    pairs = list(dict.fromkeys((str(code), str(date)) for code, date in pairs))
    if not pairs:
        return pd.DataFrame(columns=["ts_code", "trade_date", *columns])
    frames = []
    select_cols = ", ".join(["ts_code", "trade_date", *columns])
    for i in range(0, len(pairs), chunk_size):
        chunk = pairs[i : i + chunk_size]
        tuples = " OR ".join(["(ts_code=%s AND trade_date=%s)"] * len(chunk))
        params = [item for pair in chunk for item in pair]
        frame = with_retry(
            lambda: db.read_df(
                f"SELECT {select_cols} FROM {table} WHERE {tuples}",
                params,
            ),
            f"读取 {table}",
        )
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", *columns])
    return pd.concat(frames, ignore_index=True)


def build_enrichment(
    base_samples: pd.DataFrame,
    prices: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    """为逐笔明细构建分析列，按 (命中日, 代码) 键返回。"""
    # 涨停日 K 线实体：T-1 开盘/收盘（不复权），从已加载行情取
    prev_px = prices[["ts_code", "trade_date", "open", "close"]].rename(
        columns={"trade_date": "prev_date", "open": "prev_open", "close": "prev_close"}
    )
    body = base_samples[["ts_code", "prev_date"]].merge(
        prev_px, on=["ts_code", "prev_date"], how="left"
    )
    body_pct = (body["prev_close"] - body["prev_open"]) / body["prev_open"] * 100.0

    # 量能比：T 与 T-1 的成交量
    vol_df = fetch_by_code_date_pairs(
        "daily",
        ["vol"],
        list(zip(base_samples["ts_code"], base_samples["hit_date"]))
        + list(zip(base_samples["ts_code"], base_samples["prev_date"])),
    )
    vol_t = base_samples[["ts_code", "hit_date"]].merge(
        vol_df.rename(columns={"trade_date": "hit_date", "vol": "vol_t"}),
        on=["ts_code", "hit_date"],
        how="left",
    )["vol_t"]
    vol_prev = base_samples[["ts_code", "prev_date"]].merge(
        vol_df.rename(columns={"trade_date": "prev_date", "vol": "vol_prev"}),
        on=["ts_code", "prev_date"],
        how="left",
    )["vol_prev"]
    vol_ratio = (vol_t / vol_prev).replace([np.inf, -np.inf], np.nan)

    # T+1 流通市值（万元）
    mv_df = fetch_by_code_date_pairs(
        "daily_basic",
        ["circ_mv"],
        zip(base_samples["ts_code"], base_samples["t1_date"]),
    )
    circ_mv = base_samples[["ts_code", "t1_date"]].merge(
        mv_df.rename(columns={"trade_date": "t1_date"}),
        on=["ts_code", "t1_date"],
        how="left",
    )["circ_mv"]

    # 行业快照（取命中表自身的 industry，保留命中当时的分类）
    industry_df = with_retry(
        lambda: db.read_df(
            "SELECT trade_date, ts_code, industry FROM hit_limit_up_shakeout "
            "WHERE trade_date BETWEEN %s AND %s",
            (start, end),
        ),
        "读取行业快照",
    )
    industry_df = industry_df.rename(columns={"trade_date": "hit_date"})
    industry_df["hit_date"] = industry_df["hit_date"].astype(str)
    industry_df["ts_code"] = industry_df["ts_code"].astype(str)

    enrich = pd.DataFrame(
        {
            "hit_date": base_samples["hit_date"].astype(str).to_numpy(),
            "ts_code": base_samples["ts_code"].astype(str).to_numpy(),
            "买入日高开比例(原始价,%)": (
                pd.to_numeric(base_samples["entry_gap"], errors="coerce") * 100.0
            ).to_numpy(),
            "涨停日K线实体(%)": body_pct.to_numpy(),
            "量能比(T/T-1)": vol_ratio.to_numpy(),
            "买入日流通市值(万元)": circ_mv.to_numpy(),
        }
    )
    enrich = enrich.merge(
        industry_df[["hit_date", "ts_code", "industry"]],
        on=["hit_date", "ts_code"],
        how="left",
    ).rename(columns={"industry": "行业"})
    return enrich


# ==============================================================================
# 指标与拆解
# ==============================================================================


def combo_metrics(
    combo_id: str,
    tier_name: str,
    family: str,
    tiers: tuple,
    entry_name: str,
    entry_gap: tuple[float, float],
    require_low_support: bool,
    trades: pd.DataFrame,
    args: argparse.Namespace,
) -> dict:
    realized = trades[trades["exit_status"] == "已卖出"]
    buy_fail = int(trades["exit_status"].isin(cash.BUY_FAIL_STATUSES).sum())
    stats_pnl = cash.stats(realized["net_pnl"])
    stats_ret = cash.stats(realized["net_return"])
    total_pnl = float(realized["net_pnl"].sum()) if len(realized) else 0.0
    std_ret = stats_ret.get("标准差")
    mean_ret = stats_ret.get("平均")
    se_ret = (
        float(std_ret) / np.sqrt(len(realized))
        if std_ret is not None and len(realized) > 0
        else None
    )
    t_ret = (
        float(mean_ret) / se_ret
        if mean_ret is not None and se_ret not in (None, 0)
        else None
    )
    return {
        "组合": combo_id,
        "分档": tier_name,
        "族": family,
        "tiers": tiers,
        "低开区间": entry_name,
        "entry_gap": entry_gap,
        "要求最低价支撑": bool(require_low_support),
        "样本数": len(trades),
        "实际买入": int((trades["shares"] > 0).sum()),
        "买入失败": buy_fail,
        "已卖出": len(realized),
        "延期卖出": int((trades["delay_days"] > 0).sum()),
        "超期估值": int(
            trades["exit_status"].astype(str).str.startswith("超期未卖出").sum()
        ),
        "未卖出": len(trades) - len(realized) - buy_fail,
        "净盈亏合计": total_pnl,
        "平均净盈亏": stats_pnl.get("平均"),
        "平均净收益率": mean_ret,
        "中位数": stats_ret.get("中位数"),
        "标准差": std_ret,
        "标准误": se_ret,
        "t值": t_ret,
        "胜率": stats_pnl.get("胜率"),
        "盈亏比": stats_pnl.get("盈亏比"),
        "最小收益率": stats_ret.get("最小"),
        "最大收益率": stats_ret.get("最大"),
        "收益/预算": (
            total_pnl / (len(realized) * args.position) if len(realized) else None
        ),
    }


def yearly_rows(trades: pd.DataFrame) -> list[dict]:
    realized = trades[trades["exit_status"] == "已卖出"].copy()
    if realized.empty:
        return []
    rows = []
    for year, group in realized.groupby(realized["hit_date"].str[:4], sort=True):
        rows.append(
            {
                "年份": year,
                "已卖出": len(group),
                "净盈亏合计": float(group["net_pnl"].sum()),
                "平均净收益率": float(group["net_return"].mean()),
                "胜率": float((group["net_pnl"] > 0).mean()),
            }
        )
    return rows


def tier_breakdown_rows(trades: pd.DataFrame, tiers: tuple) -> list[dict]:
    rate_map = dict(zip(tier_labels(tiers), (t[2] for t in tiers)))
    rows = []
    for label in tier_labels(tiers):
        group = trades[trades["exit_tier"] == label]
        if group.empty:
            continue
        realized = group[group["exit_status"] == "已卖出"]
        rows.append(
            {
                "止盈档位": label,
                "止盈阈值": rate_map[label],
                "样本数": len(group),
                "已卖出": len(realized),
                "平均净收益率": (
                    float(realized["net_return"].mean()) if len(realized) else None
                ),
                "胜率": (
                    float((realized["net_pnl"] > 0).mean()) if len(realized) else None
                ),
                "净盈亏合计": float(realized["net_pnl"].sum()) if len(realized) else 0.0,
            }
        )
    return rows


def sell_mode_rows(trades: pd.DataFrame) -> list[dict]:
    rows = []
    valid = trades[trades["exit_mode"].astype(str).ne("")]
    total = len(trades) or 1
    for mode, group in valid.groupby("exit_mode", sort=True):
        realized = group[group["exit_status"] == "已卖出"]
        rows.append(
            {
                "卖出方式": str(mode),
                "样本数": len(group),
                "占比": len(group) / total,
                "平均净收益率": (
                    float(realized["net_return"].mean()) if len(realized) else None
                ),
                "净盈亏合计": float(realized["net_pnl"].sum()) if len(realized) else 0.0,
            }
        )
    return rows


# ==============================================================================
# 主效应 / 配对差异
# ==============================================================================


def _mean(values) -> float | None:
    values = [v for v in values if v is not None and not pd.isna(v)]
    return float(np.mean(values)) if values else None


def _median(values) -> float | None:
    values = [v for v in values if v is not None and not pd.isna(v)]
    return float(np.median(values)) if values else None


def main_effect_rows(
    results: list[dict], key: str, min_samples: int = 0
) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in results:
        if row["样本数"] < min_samples:
            continue
        groups.setdefault(str(row[key]), []).append(row)
    rows = []
    for value, items in groups.items():
        valid_means = [
            item["平均净收益率"]
            for item in items
            if item["平均净收益率"] is not None
            and not pd.isna(item["平均净收益率"])
        ]
        wins = [item["胜率"] for item in items if item["胜率"] is not None]
        samples = [item["样本数"] for item in items if item["样本数"]]
        row = {
            key: value,
            "组合数": len(items),
            "平均净收益率": _mean(valid_means),
            "中位净收益率": _median(valid_means),
            "正收益组合占比": (
                float(np.mean([m > 0 for m in valid_means]))
                if valid_means
                else None
            ),
            "平均胜率": _mean(wins),
            "平均样本数": _mean(samples),
            "最佳组合均值": max(valid_means) if valid_means else None,
            "最差组合均值": min(valid_means) if valid_means else None,
        }
        if key == "分档":
            row["族"] = items[0].get("族", "")
        rows.append(row)
    rows.sort(
        key=lambda row: (
            row["平均净收益率"] is None,
            -(row["平均净收益率"] or 0.0),
        )
    )
    return rows


def low_support_effect_rows(
    results: list[dict], min_samples: int = 0
) -> tuple[list[dict], list[dict]]:
    """返回 (配对差异明细, 按低开区间汇总)。

    只比较“要求支撑”和“不要求支撑”都达到 min_samples 的配对，避免
    1~2 笔样本的极端组合主导支撑效应。
    """
    pairs: dict[tuple[str, str], dict[bool, dict]] = {}
    for row in results:
        key = (row["分档"], row["低开区间"])
        pairs.setdefault(key, {})[bool(row["要求最低价支撑"])] = row

    detail = []
    for (tier_name, entry_name), pair in pairs.items():
        if True not in pair or False not in pair:
            continue
        yes = pair[True]
        no = pair[False]
        if yes["平均净收益率"] is None or no["平均净收益率"] is None:
            continue
        if yes["样本数"] < min_samples or no["样本数"] < min_samples:
            continue
        detail.append(
            {
                "分档": tier_name,
                "族": yes["族"],
                "低开区间": entry_name,
                "要求均值": yes["平均净收益率"],
                "不要求均值": no["平均净收益率"],
                "差异(要求-不要求)": yes["平均净收益率"] - no["平均净收益率"],
                "要求样本数": yes["样本数"],
                "不要求样本数": no["样本数"],
                "样本减少": no["样本数"] - yes["样本数"],
            }
        )

    summary = []
    for entry_name in [name for name, _rng, _note in ENTRY_GAP_GRID]:
        rows = [row for row in detail if row["低开区间"] == entry_name]
        diffs = [row["差异(要求-不要求)"] for row in rows]
        if not rows:
            continue
        summary.append(
            {
                "低开区间": entry_name,
                "配对数": len(rows),
                "平均差异": _mean(diffs),
                "中位差异": _median(diffs),
                "要求更优占比": float(np.mean([d > 0 for d in diffs])),
                "最小差异": min(diffs),
                "最大差异": max(diffs),
                "平均样本减少": _mean([row["样本减少"] for row in rows]),
            }
        )
    summary.sort(key=lambda row: -(row["平均差异"] or 0.0))
    detail.sort(key=lambda row: -(row["差异(要求-不要求)"] or 0.0))
    return detail, summary


def best_by_entry_support(results: list[dict], min_samples: int) -> list[dict]:
    rows = []
    for entry_name, entry_gap, _note in ENTRY_GAP_GRID:
        for support, support_label in LOW_SUPPORT_OPTIONS:
            subset = [
                row
                for row in results
                if row["低开区间"] == entry_name
                and bool(row["要求最低价支撑"]) == support
                and row["平均净收益率"] is not None
                and not pd.isna(row["平均净收益率"])
            ]
            qualified = [row for row in subset if row["样本数"] >= min_samples]
            pool = qualified or subset
            if not pool:
                continue
            best = max(pool, key=lambda row: row["平均净收益率"])
            rows.append(
                {
                    "低开区间": entry_name,
                    "区间": fmt_range(entry_gap),
                    "最低价支撑": support_label,
                    "最佳分档": best["分档"],
                    "族": best["族"],
                    "平均净收益率": best["平均净收益率"],
                    "中位数": best["中位数"],
                    "t值": best.get("t值"),
                    "胜率": best["胜率"],
                    "样本数": best["样本数"],
                    "净盈亏合计": best["净盈亏合计"],
                    "是否达到最小样本": best["样本数"] >= min_samples,
                }
            )
    return rows


def family_rows(results: list[dict], min_samples: int = 0) -> list[dict]:
    return main_effect_rows(results, "族", min_samples)


# ==============================================================================
# 报告
# ==============================================================================


def build_report(
    args: argparse.Namespace,
    start: str,
    end: str,
    results: list[dict],
    ranked: list[dict],
    tier_effect: list[dict],
    entry_effect: list[dict],
    family_effect: list[dict],
    support_detail: list[dict],
    support_summary: list[dict],
    best_entry_support: list[dict],
    yearly: dict[str, list[dict]],
    tier_breakdown: dict[str, list[dict]],
    sell_mode: dict[str, list[dict]],
    sample_stats: dict[tuple[str, bool], dict],
    verify_diffs: dict[str, int],
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# limit_up_shakeout 现金收益网格搜索")
    lines.append("")
    lines.append(f"- 生成时间：{generated_at}")
    lines.append(f"- 统计区间：`{start}` ~ `{end}`")
    lines.append(
        f"- 固定筛选：T 日高开 `{fmt_range(DEFAULT_GAP)}`；"
        f"T 日收盘涨跌幅 `{fmt_range(DEFAULT_CLOSE_CHG)}`；"
        f"近 N 日涨幅 `{cash.fmt_n_days_rise(DEFAULT_N_DAYS_RISE)}`"
    )
    lines.append("- 买入：T+1 原始开盘价（`delta=0`，不再搜索限价深度）")
    lines.append(
        f"- 最低价支撑基准默认：{'要求' if DEFAULT_REQUIRE_LOW_SUPPORT else '不要求'}"
        "；网格同时搜索 True / False"
    )
    lines.append(
        f"- 网格：{len(EXIT_TIERS_GRID)} 个止盈分档 × "
        f"{len(ENTRY_GAP_GRID)} 个低开区间 × "
        f"{len(LOW_SUPPORT_OPTIONS)} 个最低价支撑条件 = "
        f"**{len(EXIT_TIERS_GRID) * len(ENTRY_GAP_GRID) * len(LOW_SUPPORT_OPTIONS):,}** 个组合"
    )
    lines.append(
        f"- 每笔预算：`{args.position:,.0f}` 元；手数模式：`{args.lot_mode}`；"
        f"佣金：`{args.commission:.5%}`（最低 {args.min_commission:,.0f} 元）；"
        f"印花税：`{args.stamp:.3%}`；过户费：`{args.transfer:.4%}`；"
        f"滑点：`{args.slippage:.2%}`"
    )
    lines.append(
        f"- 有效组合数：`{len(results):,}` / `{len(EXIT_TIERS_GRID) * len(ENTRY_GAP_GRID) * len(LOW_SUPPORT_OPTIONS):,}`"
        "（样本为空的区间会被跳过）"
    )
    lines.append(
        f"- 排名最小样本数：`{args.min_samples}`；明细组合数：`{args.top_trades}`"
    )
    lines.append(
        "- 样本冻结：同一（低开区间 × 最低价支撑）下所有分档共用参考分档 "
        f"`{REFERENCE_SCHEME}` 筛出的样本，保证离场规则对比的样本集合一致。"
    )
    lines.append("")
    lines.append("> 这是样本内网格搜索。组合数很大，单个最优组合存在过拟合风险；"
                 "请优先看分档族/低开区间的主效应、配对差异和年度稳定性。")
    lines.append("")

    # 1. 网格定义
    lines.append("## 1. 网格定义")
    lines.append("")
    lines.append("### 1.1 止盈分档")
    lines.append("")
    tier_rows = [
        {
            "名称": name,
            "族": family,
            "分档函数": format_tiers_summary(tiers),
        }
        for name, tiers, family in EXIT_TIERS_GRID
    ]
    lines.append(md_table(tier_rows, ["名称", "族", "分档函数"]))
    lines.append("")
    lines.append("### 1.2 T+1 买入低开区间")
    lines.append("")
    gap_rows = [
        {
            "名称": name,
            "区间": fmt_range(rng),
            "说明": note,
            "样本数(要求支撑)": sample_stats.get((name, True), {}).get("samples", "-"),
            "样本数(不要求支撑)": sample_stats.get((name, False), {}).get("samples", "-"),
        }
        for name, rng, note in ENTRY_GAP_GRID
    ]
    lines.append(
        md_table(
            gap_rows,
            ["名称", "区间", "说明", "样本数(要求支撑)", "样本数(不要求支撑)"],
        )
    )
    lines.append("")
    lines.append(
        "> 区间按闭区间处理（`>= lo - eps` 且 `<= hi + eps`），相邻区间在边界上可能重叠；"
        "核心非重叠分区为 `高开_0~3` / `高开_3~11` / `低开_深` / `低开_中深` / "
        "`低开_中浅` / `低开_浅`；`低开_中`、`低开_中+浅`、`高开_全部`、`低开_全部`、"
        "`全区间` 是嵌套/汇总区间，用于敏感性分析，不应视为独立假设。"
    )
    lines.append("")
    lines.append("### 1.3 最低价支撑条件")
    lines.append("")
    lines.append(
        "- `要求`：后复权 `low(T) >= close(T-1)`，与原策略默认口径一致。\n"
        "- `不要求`：去掉该条件，观察放宽样本池后收益和样本量的变化。"
    )
    lines.append("")

    # 2. 样本量
    lines.append("## 2. 样本量")
    lines.append("")
    sample_rows = []
    for entry_name, _rng, _note in ENTRY_GAP_GRID:
        yes = sample_stats.get((entry_name, True), {})
        no = sample_stats.get((entry_name, False), {})
        yes_n = yes.get("samples")
        no_n = no.get("samples")
        sample_rows.append(
            {
                "低开区间": entry_name,
                "要求支撑样本": yes_n if yes_n is not None else "-",
                "不要求支撑样本": no_n if no_n is not None else "-",
                "放宽后增加": (
                    no_n - yes_n
                    if yes_n is not None and no_n is not None
                    else "-"
                ),
                "要求支撑减少占比": (
                    f"{(no_n - yes_n) / no_n:.1%}"
                    if no_n
                    else "-"
                ),
            }
        )
    lines.append(
        md_table(
            sample_rows,
            [
                "低开区间",
                "要求支撑样本",
                "不要求支撑样本",
                "放宽后增加",
                "要求支撑减少占比",
            ],
        )
    )
    lines.append("")

    # 3. 总体排名
    lines.append("## 3. 总体排名（达到最小样本数的组合）")
    lines.append("")
    if ranked:
        top_rows = []
        for i, row in enumerate(ranked[:30], start=1):
            top_rows.append(
                {
                    "排名": i,
                    "组合": row["组合"],
                    "族": row["族"],
                    "低开区间": row["低开区间"],
                    "要求支撑": "是" if row["要求最低价支撑"] else "否",
                    "样本数": row["样本数"],
                    "平均净收益率": fmt_pct(row["平均净收益率"]),
                    "中位数": fmt_pct(row["中位数"]),
                    "t值": fmt_num(row.get("t值")),
                    "胜率": fmt_pct(row["胜率"], signed=False),
                    "净盈亏合计": fmt_money(row["净盈亏合计"]),
                    "盈亏比": fmt_num(row["盈亏比"]),
                }
            )
        lines.append(
            md_table(
                top_rows,
                [
                    "排名",
                    "组合",
                    "族",
                    "低开区间",
                    "要求支撑",
                    "样本数",
                    "平均净收益率",
                    "中位数",
                    "t值",
                    "胜率",
                    "净盈亏合计",
                    "盈亏比",
                ],
            )
        )
    else:
        lines.append("_没有组合达到最小样本数。_")
    lines.append("")
    lines.append(
        "> `t值 = 平均净收益率 / 标准误`，仅作单组合参考；未做多重检验校正，"
        "组合数很大时不要把 |t|>2 当作可靠显著性。"
    )
    lines.append("")

    # 4. 分档主效应
    lines.append("## 4. 止盈分档主效应")
    lines.append("")
    lines.append(
        "对每个分档，统计它在全部（低开区间 × 支撑条件）组合上的平均净收益率、"
        "正收益组合占比、平均胜率等；**仅纳入样本数 ≥ "
        f"{args.min_samples} 的组合**。用于判断“哪类分档结构”更稳健，"
        "而不是只看单个低开区间。"
    )
    lines.append("")
    tier_effect_rows = []
    shown_tiers = set()
    for row in tier_effect[:30] + tier_effect[-10:]:
        if row["分档"] in shown_tiers:
            continue
        shown_tiers.add(row["分档"])
        tier_effect_rows.append(
            {
                "分档": row["分档"],
                "族": row["族"],
                "组合数": row["组合数"],
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "中位净收益率": fmt_pct(row["中位净收益率"]),
                "正收益组合占比": fmt_pct(row["正收益组合占比"], signed=False),
                "平均胜率": fmt_pct(row["平均胜率"], signed=False),
                "平均样本数": fmt_num(row["平均样本数"], 0),
                "最佳组合": fmt_pct(row["最佳组合均值"]),
                "最差组合": fmt_pct(row["最差组合均值"]),
            }
        )
    lines.append(
        md_table(
            tier_effect_rows,
            [
                "分档",
                "族",
                "组合数",
                "平均净收益率",
                "中位净收益率",
                "正收益组合占比",
                "平均胜率",
                "平均样本数",
                "最佳组合",
                "最差组合",
            ],
        )
    )
    lines.append("")
    lines.append("> 表中只列前 30 + 后 10 个分档；完整结果见 `tier_main_effects.csv`。")
    lines.append("")

    # 5. 低开区间主效应
    lines.append("## 5. 低开区间主效应")
    lines.append("")
    lines.append(
        f"同样仅纳入样本数 ≥ {args.min_samples} 的组合。样本过少的区间"
        "不会进入本表，请结合第 2 节样本量阅读。"
    )
    lines.append("")
    entry_rows = []
    for row in entry_effect:
        entry_rows.append(
            {
                "低开区间": row["低开区间"],
                "组合数": row["组合数"],
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "中位净收益率": fmt_pct(row["中位净收益率"]),
                "正收益组合占比": fmt_pct(row["正收益组合占比"], signed=False),
                "平均胜率": fmt_pct(row["平均胜率"], signed=False),
                "平均样本数": fmt_num(row["平均样本数"], 0),
                "最佳组合": fmt_pct(row["最佳组合均值"]),
                "最差组合": fmt_pct(row["最差组合均值"]),
            }
        )
    lines.append(
        md_table(
            entry_rows,
            [
                "低开区间",
                "组合数",
                "平均净收益率",
                "中位净收益率",
                "正收益组合占比",
                "平均胜率",
                "平均样本数",
                "最佳组合",
                "最差组合",
            ],
        )
    )
    lines.append("")

    # 6. 最低价支撑影响
    lines.append("## 6. 最低价支撑条件的影响")
    lines.append("")
    support_rows = []
    diffs_all = [row["差异(要求-不要求)"] for row in support_detail]
    if diffs_all:
        support_rows.append(
            {
                "范围": "全部配对",
                "配对数": len(diffs_all),
                "平均差异(要求-不要求)": fmt_pct(_mean(diffs_all)),
                "中位差异": fmt_pct(_median(diffs_all)),
                "要求更优占比": fmt_pct(float(np.mean([d > 0 for d in diffs_all])), signed=False),
                "最小差异": fmt_pct(min(diffs_all)),
                "最大差异": fmt_pct(max(diffs_all)),
            }
        )
    for row in support_summary:
        support_rows.append(
            {
                "范围": row["低开区间"],
                "配对数": row["配对数"],
                "平均差异(要求-不要求)": fmt_pct(row["平均差异"]),
                "中位差异": fmt_pct(row["中位差异"]),
                "要求更优占比": fmt_pct(row["要求更优占比"], signed=False),
                "最小差异": fmt_pct(row["最小差异"]),
                "最大差异": fmt_pct(row["最大差异"]),
            }
        )
    lines.append(
        md_table(
            support_rows,
            [
                "范围",
                "配对数",
                "平均差异(要求-不要求)",
                "中位差异",
                "要求更优占比",
                "最小差异",
                "最大差异",
            ],
        )
    )
    lines.append("")
    lines.append(
        f"> 配对差异仅纳入“要求/不要求”双方样本数均 ≥ {args.min_samples} 的组合。"
        "它仍是样本内对比：要求支撑会剔除一部分样本，差异同时包含"
        "“过滤效应”和“剩余样本构成变化”，不是纯因果效应。"
    )
    lines.append("")
    if support_detail:
        lines.append("### 6.1 配对差异最大的组合（要求支撑更优）")
        lines.append("")
        detail_rows = []
        for row in support_detail[:10]:
            detail_rows.append(
                {
                    "分档": row["分档"],
                    "族": row["族"],
                    "低开区间": row["低开区间"],
                    "要求均值": fmt_pct(row["要求均值"]),
                    "不要求均值": fmt_pct(row["不要求均值"]),
                    "差异": fmt_pct(row["差异(要求-不要求)"]),
                    "样本减少": row["样本减少"],
                }
            )
        lines.append(
            md_table(
                detail_rows,
                ["分档", "族", "低开区间", "要求均值", "不要求均值", "差异", "样本减少"],
            )
        )
        lines.append("")
        lines.append("### 6.2 配对差异最小的组合（要求支撑更差）")
        lines.append("")
        detail_rows = []
        for row in support_detail[-10:]:
            detail_rows.append(
                {
                    "分档": row["分档"],
                    "族": row["族"],
                    "低开区间": row["低开区间"],
                    "要求均值": fmt_pct(row["要求均值"]),
                    "不要求均值": fmt_pct(row["不要求均值"]),
                    "差异": fmt_pct(row["差异(要求-不要求)"]),
                    "样本减少": row["样本减少"],
                }
            )
        lines.append(
            md_table(
                detail_rows,
                ["分档", "族", "低开区间", "要求均值", "不要求均值", "差异", "样本减少"],
            )
        )
        lines.append("")

    # 7. 每个低开区间×支撑下的最佳分档
    lines.append("## 7. 每个低开区间 × 支撑条件下的最佳分档")
    lines.append("")
    best_rows = []
    for row in best_entry_support:
        best_rows.append(
            {
                "低开区间": row["低开区间"],
                "区间": row["区间"],
                "要求支撑": row["最低价支撑"],
                "最佳分档": row["最佳分档"],
                "族": row["族"],
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "中位数": fmt_pct(row["中位数"]),
                "t值": fmt_num(row.get("t值")),
                "胜率": fmt_pct(row["胜率"], signed=False),
                "样本数": row["样本数"],
                "达到最小样本": "是" if row["是否达到最小样本"] else "否",
                "净盈亏合计": fmt_money(row["净盈亏合计"]),
            }
        )
    lines.append(
        md_table(
            best_rows,
            [
                "低开区间",
                "区间",
                "要求支撑",
                "最佳分档",
                "族",
                "平均净收益率",
                "中位数",
                "t值",
                "胜率",
                "样本数",
                "达到最小样本",
                "净盈亏合计",
            ],
        )
    )
    lines.append("")

    # 8. 分档族主效应
    lines.append("## 8. 分档族主效应")
    lines.append("")
    family_rows_out = []
    for row in family_effect:
        family_rows_out.append(
            {
                "族": row["族"],
                "组合数": row["组合数"],
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "中位净收益率": fmt_pct(row["中位净收益率"]),
                "正收益组合占比": fmt_pct(row["正收益组合占比"], signed=False),
                "平均胜率": fmt_pct(row["平均胜率"], signed=False),
                "最佳组合": fmt_pct(row["最佳组合均值"]),
                "最差组合": fmt_pct(row["最差组合均值"]),
            }
        )
    lines.append(
        md_table(
            family_rows_out,
            [
                "族",
                "组合数",
                "平均净收益率",
                "中位净收益率",
                "正收益组合占比",
                "平均胜率",
                "最佳组合",
                "最差组合",
            ],
        )
    )
    lines.append("")

    # 9. 年度稳定性
    lines.append("## 9. 年度稳定性（Top 5 组合）")
    lines.append("")
    if ranked:
        top5 = ranked[:5]
        years = sorted({row["年份"] for combo in top5 for row in yearly.get(combo["组合"], [])})
        year_rows = []
        for year in years:
            row = {"年份": year}
            for combo in top5:
                values = [
                    item["平均净收益率"]
                    for item in yearly.get(combo["组合"], [])
                    if item["年份"] == year
                ]
                row[combo["组合"]] = fmt_pct(values[0]) if values else "-"
            year_rows.append(row)
        lines.append(md_table(year_rows, ["年份", *[combo["组合"] for combo in top5]]))
    else:
        lines.append("_无可用组合。_")
    lines.append("")

    # 10. 卖出方式与档位拆解
    lines.append("## 10. Top 3 组合的卖出方式与档位拆解")
    lines.append("")
    for combo in ranked[:3]:
        lines.append(f"### {combo['组合']}")
        lines.append("")
        mode_rows = [
            {
                "卖出方式": row["卖出方式"],
                "样本数": row["样本数"],
                "占比": fmt_pct(row["占比"], signed=False),
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "净盈亏合计": fmt_money(row["净盈亏合计"]),
            }
            for row in sell_mode.get(combo["组合"], [])
        ]
        lines.append(
            md_table(
                mode_rows,
                ["卖出方式", "样本数", "占比", "平均净收益率", "净盈亏合计"],
            )
        )
        lines.append("")
        tier_rows = [
            {
                "止盈档位": row["止盈档位"],
                "止盈阈值": fmt_pct(row["止盈阈值"], signed=False),
                "样本数": row["样本数"],
                "平均净收益率": fmt_pct(row["平均净收益率"]),
                "胜率": fmt_pct(row["胜率"], signed=False),
                "净盈亏合计": fmt_money(row["净盈亏合计"]),
            }
            for row in tier_breakdown.get(combo["组合"], [])
        ]
        lines.append(
            md_table(
                tier_rows,
                ["止盈档位", "止盈阈值", "样本数", "平均净收益率", "胜率", "净盈亏合计"],
            )
        )
        lines.append("")

    # 11. 冻结校验
    lines.append("## 11. 冻结样本校验")
    lines.append("")
    lines.append(
        f"参考分档：`{REFERENCE_SCHEME}`；校验轮廓："
        + "、".join(f"`{name}`" for name in VERIFY_SCHEMES)
        + "。"
    )
    nonzero = {key: value for key, value in verify_diffs.items() if value}
    if nonzero:
        lines.append("")
        lines.append("以下组合的独立重筛样本集合与冻结集合存在差异：")
        lines.append("")
        lines.append(
            md_table(
                [{"组合": key, "差异条数": value} for key, value in nonzero.items()],
                ["组合", "差异条数"],
            )
        )
    else:
        lines.append("")
        lines.append("所有校验轮廓的独立重筛差异均为 0 条。")
    lines.append("")

    # 12. 结论与注意事项
    lines.append("## 12. 结论与注意事项")
    lines.append("")
    if ranked:
        best = ranked[0]
        lines.append(
            f"- 达到最小样本数 `{args.min_samples}` 的组合中，平均净收益率最高的是 "
            f"`{best['组合']}`：{fmt_pct(best['平均净收益率'])}，"
            f"中位数 {fmt_pct(best['中位数'])}，胜率 {fmt_pct(best['胜率'], signed=False)}，"
            f"样本 {best['样本数']:,}。"
        )
    if family_effect:
        top_family = family_effect[0]
        lines.append(
            f"- 分档族主效应最强的是 `{top_family['族']}`："
            f"跨组合平均净收益率 {fmt_pct(top_family['平均净收益率'])}，"
            f"正收益组合占比 {fmt_pct(top_family['正收益组合占比'], signed=False)}。"
        )
    if entry_effect:
        top_entry = entry_effect[0]
        lines.append(
            f"- 低开区间主效应最强的是 `{top_entry['低开区间']}`："
            f"跨组合平均净收益率 {fmt_pct(top_entry['平均净收益率'])}，"
            f"正收益组合占比 {fmt_pct(top_entry['正收益组合占比'], signed=False)}。"
        )
    if diffs_all:
        lines.append(
            f"- 最低价支撑的配对差异：要求 − 不要求的平均差异为 "
            f"{fmt_pct(_mean(diffs_all))}，要求更优的配对占比 "
            f"{fmt_pct(float(np.mean([d > 0 for d in diffs_all])), signed=False)}；"
            "但它会减少样本数，需同时看样本减少和稳定性。"
        )
    lines.append("")
    lines.append("**注意事项：**")
    lines.append("")
    lines.append("- 网格组合数很大，单个最优组合可能是样本内噪声；优先看主效应/族效应和年度稳定性。")
    lines.append("- 低开区间存在嵌套/重叠，配对比较和主效应会被重复样本放大。")
    lines.append("- 同一交易日可能有多个信号，持仓期重叠；合计净盈亏不是组合净值。")
    lines.append("- 买入按 T+1 开盘价，未考虑涨停买不进、跌停卖不掉之外的盘中流动性。")
    lines.append("- 结果对佣金、滑点、停牌/跌停处理敏感；实盘前应做样本外/滚动验证。")
    lines.append("")

    lines.append("## 13. 输出文件")
    lines.append("")
    lines.append("- `grid_results.csv`：全部组合汇总指标")
    lines.append("- `tier_main_effects.csv`：分档主效应")
    lines.append("- `entry_gap_main_effects.csv`：低开区间主效应")
    lines.append("- `family_main_effects.csv`：分档族主效应")
    lines.append("- `low_support_effects.csv`：要求/不要求支撑的逐对差异")
    lines.append("- `low_support_summary.csv`：按低开区间汇总的支撑效应")
    lines.append("- `best_by_entry_support.csv`：每个低开区间×支撑下的最佳分档")
    lines.append("- `yearly_stats.csv` / `tier_breakdown.csv` / `sell_mode_breakdown.csv`")
    lines.append("- `all_trades.csv`：Top-N 组合 + 基准组合逐笔明细")
    lines.append("- `run_meta.json`：网格定义、样本量与参数")
    lines.append("")
    return "\n".join(lines)


# ==============================================================================
# 主流程
# ==============================================================================


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.position <= 0:
        raise SystemExit("--position 必须 > 0")
    if args.max_delay_days < 0:
        raise SystemExit("--max-delay-days 必须 >= 0")
    if args.min_samples < 0:
        raise SystemExit("--min-samples 必须 >= 0")
    if args.top_trades < 0:
        raise SystemExit("--top-trades 必须 >= 0（0 = 不写逐笔明细）")

    scheme_tiers = {name: tiers for name, tiers, _family in EXIT_TIERS_GRID}
    if len(scheme_tiers) != len(EXIT_TIERS_GRID):
        raise SystemExit("止盈分档名称存在重复")
    for name, tiers, _family in EXIT_TIERS_GRID:
        try:
            validate_exit_tiers(tiers)
        except ValueError as exc:
            raise SystemExit(f"网格中的止盈分档 {name} 无效：{exc}") from exc
    if REFERENCE_SCHEME not in scheme_tiers:
        raise SystemExit(f"参考分档 {REFERENCE_SCHEME} 不存在")
    missing_verify = [name for name in VERIFY_SCHEMES if name not in scheme_tiers]
    if missing_verify:
        raise SystemExit(f"VERIFY_SCHEMES 引用了不存在的分档：{missing_verify}")
    for name, rng, _note in ENTRY_GAP_GRID:
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"网格中的低开区间 {name} 无效：{rng}")

    start = cash.compact_date(args.start)
    end = with_retry(lambda: cash.resolve_end_date(args.end), "解析结束日期")
    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")

    total_combos = len(EXIT_TIERS_GRID) * len(ENTRY_GAP_GRID) * len(LOW_SUPPORT_OPTIONS)
    print("=" * 96)
    print("limit_up_shakeout 现金收益网格搜索")
    print("  维度：止盈分档 × T+1 买入低开区间 × 是否要求最低价支撑")
    print(f"  统计区间：{start} ~ {end}")
    print(
        f"  固定筛选：T日高开 {fmt_range(DEFAULT_GAP)}；"
        f"T日收盘涨跌幅 {fmt_range(DEFAULT_CLOSE_CHG)}；"
        f"近N日涨幅 {cash.fmt_n_days_rise(DEFAULT_N_DAYS_RISE)}"
    )
    print(
        f"  网格：{len(EXIT_TIERS_GRID)} 个止盈分档 × "
        f"{len(ENTRY_GAP_GRID)} 个低开区间 × "
        f"{len(LOW_SUPPORT_OPTIONS)} 个支撑条件 = {total_combos:,} 个组合"
    )
    print(
        f"  每笔预算：{args.position:,.0f} 元；手数模式：{args.lot_mode}；"
        f"佣金：{args.commission:.5%}（最低 {args.min_commission:,.0f} 元）；"
        f"滑点：{args.slippage:.2%}"
    )
    print(f"  参考分档：{REFERENCE_SCHEME}；校验轮廓：{'、'.join(VERIFY_SCHEMES)}")
    print("-" * 96)

    t0 = time.time()
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = with_retry(lambda: cash.load_hits(start, end), "读取命中样本")
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条")

    calendar = with_retry(cash.load_calendar, "读取交易日历")
    hits = cash.attach_dates(hits, calendar, DEFAULT_N_DAYS_RISE)
    print("读取行情（按样本角色日期精确取数）...")
    prices = with_retry(
        lambda: cash.load_prices_by_keys(cash.price_keys_for_hits(hits)),
        "读取行情",
    )
    if prices.empty:
        raise SystemExit("没有读取到行情数据")
    print(f"行情行数：{len(prices):,}（耗时 {time.time() - t0:.1f}s）")
    print("-" * 96)

    def run_samples(
        entry_gap: tuple[float, float],
        require_low_support: bool,
        tiers: tuple,
    ) -> pd.DataFrame:
        return cash.build_samples(
            hits,
            prices,
            DEFAULT_GAP,
            DEFAULT_CLOSE_CHG,
            DEFAULT_N_DAYS_RISE,
            tiers,
            entry_gap,
            delta_pct=0.0,
            require_low_support=require_low_support,
            compute_reasons=False,
        )

    # 每个（低开区间 × 支撑条件）冻结一次样本；分档只改变卖出规则。
    frozen_input: dict[tuple[str, bool], pd.DataFrame] = {}
    frozen_keys: dict[tuple[str, bool], set] = {}
    sample_stats: dict[tuple[str, bool], dict] = {}
    base_samples: pd.DataFrame | None = None
    reference_tiers = scheme_tiers[REFERENCE_SCHEME]
    for entry_name, entry_gap, _note in ENTRY_GAP_GRID:
        for support, support_label in LOW_SUPPORT_OPTIONS:
            samples = run_samples(entry_gap, support, reference_tiers)
            included = samples[samples["included"]].copy()
            hit_samples = len(samples)
            if base_samples is None:
                base_samples = samples
            if included.empty:
                print(
                    f"  低开 {entry_name} × 支撑{support_label}："
                    "筛选后没有可计算收益的样本，跳过"
                )
                sample_stats[(entry_name, support)] = {
                    "samples": 0,
                    "hit_samples": hit_samples,
                    "excluded": hit_samples,
                }
                continue
            cash_input = cash.build_cash_input(included)
            frozen_input[(entry_name, support)] = cash_input
            frozen_keys[(entry_name, support)] = set(
                zip(
                    cash_input["命中日期"].astype(str),
                    cash_input["股票代码"].astype(str),
                )
            )
            sample_stats[(entry_name, support)] = {
                "samples": len(cash_input),
                "hit_samples": hit_samples,
                "excluded": hit_samples - len(cash_input),
            }
            print(
                f"  低开 {entry_name} × 支撑{support_label}："
                f"冻结样本 {len(cash_input):,} / {hit_samples:,} 条"
            )
    if base_samples is None or not frozen_input:
        raise SystemExit("没有可用于网格搜索的样本")

    # 校验：极端阈值轮廓独立重筛，对比冻结样本集合差异（应为 0）
    verify_diffs: dict[str, int] = {}
    for entry_name, entry_gap, _note in ENTRY_GAP_GRID:
        for support, support_label in LOW_SUPPORT_OPTIONS:
            key = (entry_name, support)
            if key not in frozen_keys:
                continue
            for verify_name in VERIFY_SCHEMES:
                vsamples = run_samples(entry_gap, support, scheme_tiers[verify_name])
                vincluded = vsamples[vsamples["included"]].copy()
                vkeys = set(
                    zip(
                        vincluded["hit_date"].astype(str),
                        vincluded["ts_code"].astype(str),
                    )
                )
                diff = len(vkeys ^ frozen_keys[key])
                verify_diffs[f"{verify_name}×{entry_name}×支撑{support_label}"] = diff
                if diff:
                    print(
                        f"  注意：{verify_name}×{entry_name}×支撑{support_label} "
                        f"独立重筛与冻结集合差异 {diff} 条"
                    )
    if all(v == 0 for v in verify_diffs.values()):
        print(f"冻结样本校验通过：{len(verify_diffs)} 个极端轮廓重筛差异均为 0 条")

    # 逐笔分析列（行业/低开/流通市值/量能/涨停日实体）
    enrich = build_enrichment(base_samples, prices, start, end)
    print(f"分析列构建完成：{len(enrich):,} 行")

    # 预取顺延行情：组合循环阶段零数据库访问
    delay_cache = preload_delay_cache(frozen_input, calendar, args.max_delay_days)
    print("-" * 96)

    cash_args_base = dict(
        position=args.position,
        lot_mode=args.lot_mode,
        max_delay_days=args.max_delay_days,
        commission=args.commission,
        min_commission=args.min_commission,
        stamp=args.stamp,
        transfer=args.transfer,
        slippage=args.slippage,
        delta=0.0,
    )
    cash_args = argparse.Namespace(**cash_args_base)

    results: list[dict] = []
    yearly: dict[str, list[dict]] = {}
    tier_breakdown: dict[str, list[dict]] = {}
    sell_mode: dict[str, list[dict]] = {}
    effective_total = len(frozen_input) * len(EXIT_TIERS_GRID)
    done = 0
    t_combo = time.time()

    for tier_name, tiers, family in EXIT_TIERS_GRID:
        for entry_name, entry_gap, _note in ENTRY_GAP_GRID:
            for support, support_label in LOW_SUPPORT_OPTIONS:
                key = (entry_name, support)
                if key not in frozen_input:
                    continue
                combo_id = f"{tier_name}×{entry_name}×支撑{support_label}"
                trades = cash.calc_trades(
                    frozen_input[key],
                    cash_args,
                    tiers,
                    calendar=calendar,
                    price_cache=delay_cache,
                )
                metrics = combo_metrics(
                    combo_id,
                    tier_name,
                    family,
                    tiers,
                    entry_name,
                    entry_gap,
                    support,
                    trades,
                    args,
                )
                results.append(metrics)
                yearly[combo_id] = yearly_rows(trades)
                tier_breakdown[combo_id] = tier_breakdown_rows(trades, tiers)
                sell_mode[combo_id] = sell_mode_rows(trades)
                done += 1
                if done % 25 == 0 or done == effective_total:
                    print(
                        f"  已完成 {done}/{effective_total} 个组合"
                        f"（{time.time() - t_combo:.0f}s）"
                    )
    print("-" * 96)

    ranked_all = sorted(
        results,
        key=lambda row: (
            row["平均净收益率"] is None,
            -(row["平均净收益率"] or 0.0),
        ),
    )
    ranked = [
        row
        for row in ranked_all
        if row["平均净收益率"] is not None and row["样本数"] >= args.min_samples
    ]
    if not ranked:
        print(f"没有组合达到最小样本数 {args.min_samples}，排名改用全部组合")
        ranked = [row for row in ranked_all if row["平均净收益率"] is not None]
    metrics_index = {row["组合"]: row for row in results}

    print(f"按平均净收益率排名（前 10 / 共 {len(ranked)} 个达标组合）：")
    for i, row in enumerate(ranked[:10], start=1):
        print(
            f"  {i}. {row['组合']}：{fmt_pct(row['平均净收益率'])}，"
            f"样本 {row['样本数']:,}，净盈亏 {fmt_money(row['净盈亏合计'])} 元，"
            f"胜率 {fmt_pct(row['胜率'], signed=False)}"
        )

    tier_effect = main_effect_rows(results, "分档", args.min_samples)
    entry_effect = main_effect_rows(results, "低开区间", args.min_samples)
    family_effect = family_rows(results, args.min_samples)
    support_detail, support_summary = low_support_effect_rows(
        results, args.min_samples
    )
    best_entry_support = best_by_entry_support(results, args.min_samples)

    if tier_effect:
        print("分档族主效应（按平均净收益率）：")
        for row in family_effect:
            print(
                f"  {row['族']}：{fmt_pct(row['平均净收益率'])}，"
                f"正收益组合占比 {fmt_pct(row['正收益组合占比'], signed=False)}"
            )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_cash_grid_search_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        # 1) 全部组合
        grid_rows = []
        for row in ranked_all:
            grid_rows.append(
                {
                    "组合": row["组合"],
                    "分档": row["分档"],
                    "族": row["族"],
                    "止盈分档": format_tiers_summary(row["tiers"]),
                    "止盈分档JSON": json.dumps(
                        tiers_to_jsonable(row["tiers"]), ensure_ascii=False
                    ),
                    "低开区间": row["低开区间"],
                    "区间下界": row["entry_gap"][0],
                    "区间上界": row["entry_gap"][1],
                    "要求最低价支撑": row["要求最低价支撑"],
                    "样本数": row["样本数"],
                    "实际买入": row["实际买入"],
                    "买入失败": row["买入失败"],
                    "已卖出": row["已卖出"],
                    "延期卖出": row["延期卖出"],
                    "超期估值": row["超期估值"],
                    "未卖出": row["未卖出"],
                    "净盈亏合计": row["净盈亏合计"],
                    "平均净盈亏": row["平均净盈亏"],
                    "平均净收益率": row["平均净收益率"],
                    "中位数": row["中位数"],
                    "标准差": row.get("标准差"),
                    "标准误": row.get("标准误"),
                    "t值": row.get("t值"),
                    "胜率": row["胜率"],
                    "盈亏比": row["盈亏比"],
                    "最小收益率": row["最小收益率"],
                    "最大收益率": row["最大收益率"],
                    "收益/预算": row["收益/预算"],
                }
            )
        pd.DataFrame(grid_rows).to_csv(
            run_dir / "grid_results.csv", index=False, encoding="utf-8-sig"
        )

        # 2) 主效应
        def write_effect(rows: list[dict], path: Path, key: str) -> None:
            out = []
            for row in rows:
                item = {
                    key: row[key],
                    "组合数": row["组合数"],
                    "平均净收益率": row["平均净收益率"],
                    "中位净收益率": row["中位净收益率"],
                    "正收益组合占比": row["正收益组合占比"],
                    "平均胜率": row["平均胜率"],
                    "平均样本数": row["平均样本数"],
                    "最佳组合均值": row["最佳组合均值"],
                    "最差组合均值": row["最差组合均值"],
                }
                if key == "分档":
                    item["族"] = row.get("族", "")
                out.append(item)
            pd.DataFrame(out).to_csv(path, index=False, encoding="utf-8-sig")

        write_effect(tier_effect, run_dir / "tier_main_effects.csv", "分档")
        write_effect(entry_effect, run_dir / "entry_gap_main_effects.csv", "低开区间")
        write_effect(family_effect, run_dir / "family_main_effects.csv", "族")

        pd.DataFrame(support_detail).to_csv(
            run_dir / "low_support_effects.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(support_summary).to_csv(
            run_dir / "low_support_summary.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(best_entry_support).to_csv(
            run_dir / "best_by_entry_support.csv", index=False, encoding="utf-8-sig"
        )

        # 3) 年度 / 档位 / 卖出方式
        yearly_rows_out = [
            {"组合": row["组合"], **item}
            for row in ranked_all
            for item in yearly.get(row["组合"], [])
        ]
        pd.DataFrame(yearly_rows_out).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )

        tier_rows_out = [
            {"组合": row["组合"], **item}
            for row in ranked_all
            for item in tier_breakdown.get(row["组合"], [])
        ]
        pd.DataFrame(tier_rows_out).to_csv(
            run_dir / "tier_breakdown.csv", index=False, encoding="utf-8-sig"
        )

        mode_rows_out = [
            {"组合": row["组合"], **item}
            for row in ranked_all
            for item in sell_mode.get(row["组合"], [])
        ]
        pd.DataFrame(mode_rows_out).to_csv(
            run_dir / "sell_mode_breakdown.csv", index=False, encoding="utf-8-sig"
        )

        # 4) all_trades.csv：Top-N + 基准组合
        top_n = args.top_trades if args.top_trades > 0 else len(ranked)
        selected_ids = [row["组合"] for row in ranked[:top_n]]
        baseline_id = f"{REFERENCE_SCHEME}×{ENTRY_GAP_GRID[0][0]}×支撑要求"
        if baseline_id in metrics_index and baseline_id not in selected_ids:
            selected_ids.append(baseline_id)
        print(
            f"写 all_trades.csv：{len(selected_ids)} 个组合"
            f"（Top{top_n} + 基准）..."
        )
        preferred = [
            "组合",
            "要求最低价支撑",
            "命中日期",
            "股票代码",
            "股票名称",
            "行业",
            "买入日高开比例(原始价,%)",
            "买入日流通市值(万元)",
            "量能比(T/T-1)",
            "涨停日K线实体(%)",
            "买入日",
            "计划卖出日",
            "实际卖出日",
            "T+1收盘对买入价涨幅",
            "动态止盈阈值",
            "止盈区间",
            "卖出方式",
        ]
        with (run_dir / "all_trades.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as fout:
            wrote_header = False
            for i, combo_id in enumerate(selected_ids, start=1):
                row = metrics_index[combo_id]
                key = (row["低开区间"], row["要求最低价支撑"])
                trades = cash.calc_trades(
                    frozen_input[key],
                    cash_args,
                    row["tiers"],
                    calendar=calendar,
                    price_cache=delay_cache,
                )
                trades = trades.merge(enrich, on=["hit_date", "ts_code"], how="left")
                trades.insert(0, "combo", combo_id)
                trades.insert(
                    1,
                    "require_low_support",
                    "要求" if row["要求最低价支撑"] else "不要求",
                )
                out = trades.rename(
                    columns={
                        "combo": "组合",
                        "require_low_support": "要求最低价支撑",
                        **cash.TRADE_CN,
                    }
                )
                order = [c for c in preferred if c in out.columns] + [
                    c for c in out.columns if c not in preferred
                ]
                out[order].to_csv(fout, header=not wrote_header, index=False)
                wrote_header = True
                print(f"  [明细 {i}/{len(selected_ids)}] {combo_id}")

        # 5) run_meta.json
        meta = {
            "generated_at": generated_at,
            "start": start,
            "end": end,
            "fixed": {
                "gap_range": list(DEFAULT_GAP),
                "close_chg_range": list(DEFAULT_CLOSE_CHG),
                "n_days_rise": DEFAULT_N_DAYS_RISE,
            },
            "grid": {
                "exit_tiers": [
                    {
                        "name": name,
                        "family": family,
                        "tiers": tiers_to_jsonable(tiers),
                    }
                    for name, tiers, family in EXIT_TIERS_GRID
                ],
                "entry_gap": [
                    {"name": name, "range": list(rng), "note": note}
                    for name, rng, note in ENTRY_GAP_GRID
                ],
                "low_support": [
                    {"value": support, "label": label}
                    for support, label in LOW_SUPPORT_OPTIONS
                ],
                "total_combos": total_combos,
            },
            "reference_scheme": REFERENCE_SCHEME,
            "verify_schemes": list(VERIFY_SCHEMES),
            "verify_diffs": verify_diffs,
            "sample_stats": {
                f"{entry_name}×支撑{LOW_SUPPORT_LABELS[support]}": stats
                for (entry_name, support), stats in sample_stats.items()
            },
            "cash": {
                "position": args.position,
                "lot_mode": args.lot_mode,
                "max_delay_days": args.max_delay_days,
                "commission": args.commission,
                "min_commission": args.min_commission,
                "stamp": args.stamp,
                "transfer": args.transfer,
                "slippage": args.slippage,
                "delta": 0.0,
            },
            "min_samples_for_ranking": args.min_samples,
            "trades_top_n": args.top_trades,
            "trades_combos": selected_ids,
            "analysis_columns": [
                "行业",
                "买入日高开比例(原始价,%)",
                "买入日流通市值(万元)",
                "量能比(T/T-1)",
                "涨停日K线实体(%)",
            ],
            "hit_samples": len(hits),
            "price_rows": len(prices),
            "results": [
                {
                    "组合": row["组合"],
                    "分档": row["分档"],
                    "族": row["族"],
                    "低开区间": row["低开区间"],
                    "要求最低价支撑": row["要求最低价支撑"],
                    "样本数": row["样本数"],
                    "净盈亏合计": row["净盈亏合计"],
                    "平均净收益率": row["平均净收益率"],
                    "t值": row.get("t值"),
                    "胜率": row["胜率"],
                }
                for row in ranked_all
            ],
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        report = build_report(
            args,
            start,
            end,
            results,
            ranked,
            tier_effect,
            entry_effect,
            family_effect,
            support_detail,
            support_summary,
            best_entry_support,
            yearly,
            tier_breakdown,
            sell_mode,
            sample_stats,
            verify_diffs,
            generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"报告已生成：{run_dir.resolve()}")

    print("=" * 96)
    print(f"完成：{len(results)} 个有效组合，耗时 {time.time() - t0:.0f}s")
    if run_dir is not None:
        print(f"报告目录：{run_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
