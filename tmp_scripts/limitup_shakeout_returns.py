"""计算 limit_up_shakeout 二次筛选样本的现金盈亏。

筛选条件（T 为命中日，闭区间）：
1. T 日高开：raw_open(T) / raw_pre_close(T) - 1 ∈ DEFAULT_GAP
2. T 日收盘涨跌幅：raw_close(T) / raw_pre_close(T) - 1 ∈ DEFAULT_CLOSE_CHG
3. 近 N 个交易日涨幅（含 T 日）：close(T) / close(T-N) - 1（后复权）满足
   DEFAULT_N_DAYS_RISE。默认 "unlimited" 表示不限，不要求 T-N 行情。
   DEFAULT_N_DAYS_RISE 的写法：
   - "unlimited"：不做限制；
   - (5, -0.1, 0.1)：近 5 日涨幅在 [-10%, +10%]；
   - (5, 0.1, None)：近 5 日涨幅 >= +10%（None 表示该侧不设限）。
4. T+1 买入低开：raw_open(T+1) / raw_close(T) - 1 ∈ DEFAULT_ENTRY_GAP
   （原始价口径，与软件显示价格一致；DEFAULT_ENTRY_GAP 支持多区间并集）
5. 信号日最低价不破前一交易日收盘（后复权 low(T) >= close(T-1)），默认要求，
   可用 --no-require-low-support 关闭。

买入规则（--delta，单位百分点，默认 0）：
    a = open(T+1) / close(T) - 1             # 不复权高开比例，a<0 即低开
    买入基准价 = (1 + a - delta/100) × close(T)
              = open(T+1) - (delta/100) × close(T)
- delta=0：按 T+1 开盘价买入（原规则）；
- delta>0：视为开盘下方 delta% 的限价单，T+1 最低价 <= 基准价才成交
  （按基准价成交），日内未触及则买入失败、该样本不交易；
- 实际买入价 = 基准价 × (1 + 滑点)。

卖出规则（动态止盈，阈值来自 quant.engine.exit_tiers.DEFAULT_EXIT_TIERS，
可用 --exit-tiers / --open-exit 覆盖）：
- 分档涨幅 = close(T+1) / 买入基准价 - 1（delta=0 时即原日内涨幅 close/open-1）；
- 持有窗口为 T+2 .. T+N（N 默认 2，可用 --sell-n 调整）：
  - 某日开盘价高于 买入基准价×(1+阈值)：按该日开盘价卖出；
  - 否则该日最高价触及目标价：按目标价卖出；
  - 一直未触发：T+N 收盘卖出；若收盘跌停，视为卖不出，顺延到下一交易日开盘，
    仍跌停继续顺延，最多 --max-delay-days 个交易日；
- 超期仍无法卖出时按最后可用收盘价做市值估值，并在报告中单独标注。

每笔按预算取最大整数手（100 股/手），扣除佣金（默认 0.015%，最低 5 元）、
印花税（卖出 0.05%）、过户费（双边 0.001%）和滑点（默认 0）。

用法：
    uv run python tmp_scripts/limitup_shakeout_returns.py
    uv run python tmp_scripts/limitup_shakeout_returns.py --report
    uv run python tmp_scripts/limitup_shakeout_returns.py --start 2024-01-01 --end latest --report
    uv run python tmp_scripts/limitup_shakeout_returns.py --gap 0.05 0.11 --close-chg 0.02 0.11 --n-days-rise 5 -0.1 0.2 --entry-gap -0.11 0 --open-exit 0.02
    uv run python tmp_scripts/limitup_shakeout_returns.py --exit-tiers "[[null,-0.05,-0.02],[-0.05,0,0.01],[0,0.05,0.02],[0.05,0.07,0.03],[0.07,null,0.04]]"
    uv run python tmp_scripts/limitup_shakeout_returns.py --delta 1.5 --report   # 开盘下方 1.5% 限价买入
    uv run python tmp_scripts/limitup_shakeout_returns.py --sell-n 3 --report  # 最多持有到 T+3
    uv run python tmp_scripts/limitup_shakeout_returns.py --no-require-low-support --report

样本来自 hit_limit_up_shakeout（策略名 limit_up_shakeout）。若策略逻辑或参数有调整，先执行：
    uv run quant hits update -s limit_up_shakeout --from-date 2018-01-01

性能：行情按 (ts_code, trade_date) 主键精确取数（不再整段拉取数百万行日线）；
calc_trades 使用 build_samples 预先扫描好的卖出计划，
只有“T+N 收盘卖出且跌停”的少数样本逐笔查缓存。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
    apply_exit_tier_codes,
    apply_exit_thresholds,
    format_tiers_summary,
    parse_exit_tiers,
    tier_labels,
    tiers_from_scalar,
    tiers_to_jsonable,
)

# ==============================================================================
# 统一默认参数
DEFAULT_START = "20251101"
DEFAULT_END = "latest"

DEFAULT_GAP = (0.00, 0.11)
DEFAULT_CLOSE_CHG = (0.0, 0.11)

# 近 N 日涨幅条件：
#   "unlimited"     → 不做限制（不要求 T-N 行情）
#   (days, lo, hi)  → 近 days 日涨幅 ∈ [lo, hi]；lo/hi 为 None 表示该侧不设限
# 例：(5, -0.1, 0.1) / (5, 0.1, None)
DEFAULT_N_DAYS_RISE = "unlimited"

# T+1 买入低开区间，可表示多个闭区间的并集：
#   ((-0.06, -0.01),)                       → 仅 [-6%, -1%]
#   ((-0.06, -0.01), (-0.09, -0.08))        → [-6%, -1%] ∪ [-9%, -8%]
# CLI 也可直接写：--entry-gap -0.06 -0.01 -0.09 -0.08
DEFAULT_ENTRY_GAP: tuple[tuple[float, float], ...] = ((-0.06, -0.01), (-0.09, -0.08))

# T+1 限价买入下探深度 delta（百分点）：0 = 开盘价买入
DEFAULT_DELTA = 0
DEFAULT_REQUIRE_LOW_SUPPORT = True

# 最长持有到 T+N（交易日）：T+1 买入，T+2 起盘中触及止盈目标即卖出，
# 一直未触发则 T+N 收盘卖出。N=2 等价于原来的 T+2 固定卖出。
DEFAULT_SELL_N = 2


DEFAULT_GENERATE_REPORT = False


# 现金口径
DEFAULT_POSITION = 100_000.0
DEFAULT_LOT_MODE = "floor"
DEFAULT_MAX_DELAY_DAYS = 10
DEFAULT_COMMISSION = 0.00015
DEFAULT_MIN_COMMISSION = 5.0
DEFAULT_STAMP = 0.0005
DEFAULT_TRANSFER = 0.00001
DEFAULT_SLIPPAGE = 0.0


# ==============================================================================

PRICE_CHUNK = 500
# 按 (ts_code, trade_date) 精确取行情时每批的键数；主键 row constructor IN 查询
PRICE_KEY_CHUNK = 5000
GAP_EPS = 1e-9
LIMIT_EPS = 1e-6

TRADE_CN = {
    "hit_date": "命中日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "buy_date": "买入日",
    "plan_sell_date": "计划卖出日",
    "sell_date": "实际卖出日",
    "exit_offset": "卖出偏移(交易日)",
    "t1_intraday_ret": "T+1收盘对买入价涨幅",
    "exit_threshold": "动态止盈阈值",
    "exit_tier": "止盈区间",
    "exit_mode": "卖出方式",
    "delay_days": "延期天数",
    "lot_mode": "手数取整方式",
    "lots": "手数",
    "shares": "股数",
    "buy_base_raw": "买入基准价(不复权)",
    "buy_price_raw": "买入价(不复权,含滑点)",
    "sell_price_raw": "卖出价(不复权,含滑点)",
    "buy_amount": "买入金额",
    "buy_fee": "买入费用",
    "sell_amount": "卖出金额",
    "sell_fee": "卖出费用",
    "net_pnl": "净盈亏",
    "gross_return": "毛收益率",
    "net_return": "净收益率",
    "exit_status": "卖出状态",
    "is_delayed": "是否延期卖出",
    "close_limit_down": "T+N收盘是否跌停",
    "error": "备注",
}

# cash_trades.csv 的分析字段（来自 build_samples 的样本上下文，不进 calc_trades）
CONTEXT_CN = {
    "industry": "公司所属行业",
    # T 日盘后（选股时可知）
    "t_circ_mv_yi": "T日收盘后流通市值(亿元)",
    "amount_ratio_t_prev": "T成交额/T-1成交额",
    "t_open_vs_prev_close_adj": "T日开盘相对T-1收盘(后复权)",
    "t_close_vs_prev_close_adj": "T日收盘相对T-1收盘(后复权)",
    "t_low_vs_prev_close_adj": "T日最低相对T-1收盘(后复权)",
    # T+1 开盘（买入时可知）；与 --entry-gap / DEFAULT_ENTRY_GAP 同口径
    "entry_gap_close_raw": "T+1日高开比例(原始价)",
    # T+1 收盘（买入后可知）
    "amount_ratio_t1_t": "T+1成交额/T成交额",
    "t1_kline": "T+1收盘K线形态",
    "t1_intraday_adj": "T+1日内涨幅(后复权)",
    "t1_low_drawdown_adj": "T+1开盘至最低下探比例(后复权)",
    "t1_high_upside_adj": "T+1开盘至最高上行比例(后复权)",
    "t1_close_change_adj": "T+1开盘至收盘变化比例(后复权)",
    # T+2（卖出后可知）
    "t2_close_vs_t1_open_adj": "T+2收盘对T+1开盘涨幅(后复权)",
    "t2_high_vs_t1_open_adj": "T+2最高对T+1开盘涨幅(后复权)",
}

# cash_trades.csv 的导出列：隐藏内部执行字段，涨幅列改为百分数显示
CASH_TRADES_EXCLUDED_COLUMNS = ("lot_mode", "lots", "shares", "exit_status")
CASH_TRADES_CN = {
    key: value
    for key, value in {**TRADE_CN, **CONTEXT_CN}.items()
    if key not in CASH_TRADES_EXCLUDED_COLUMNS
}
CASH_TRADES_CN["t1_intraday_ret"] = "买入日毛收益率"

# 列顺序：基本信息 → 日期 → T 日盘后 → T+1 开盘/买入 → T+1 收盘 → 止盈/卖出 → 卖出后路径 → 现金结果
# 同一阶段内按“先价格/涨幅，后执行结果”排列，避免把买入价和卖出价混在一起。
CASH_TRADES_COLUMN_ORDER = [
    # 基本信息
    "hit_date",
    "ts_code",
    "name",
    "industry",
    # 日期
    "buy_date",
    "plan_sell_date",
    "sell_date",
    "exit_offset",
    # T 日盘后（选股时可知）
    "t_circ_mv_yi",
    "amount_ratio_t_prev",
    "t_open_vs_prev_close_adj",
    "t_close_vs_prev_close_adj",
    "t_low_vs_prev_close_adj",
    # T+1 开盘/买入（T+1 开盘时可知）
    "entry_gap_close_raw",
    "buy_base_raw",
    "buy_price_raw",
    # T+1 收盘（买入后可知）
    "amount_ratio_t1_t",
    "t1_kline",
    "t1_intraday_ret",
    "t1_intraday_adj",
    "t1_low_drawdown_adj",
    "t1_high_upside_adj",
    "t1_close_change_adj",
    # 止盈/卖出
    "exit_threshold",
    "exit_tier",
    "exit_mode",
    "sell_price_raw",
    "delay_days",
    "is_delayed",
    "close_limit_down",
    # T+2 路径指标（卖出窗口首日，卖出后可知）
    "t2_close_vs_t1_open_adj",
    "t2_high_vs_t1_open_adj",
    # 现金结果
    "buy_amount",
    "buy_fee",
    "sell_amount",
    "sell_fee",
    "net_pnl",
    "gross_return",
    "net_return",
    "error",
]

# cash_trades.csv 中按百分数显示的列
CASH_TRADES_PCT_COLUMNS = (
    "t_open_vs_prev_close_adj",
    "t_close_vs_prev_close_adj",
    "t_low_vs_prev_close_adj",
    "entry_gap_close_raw",
    "t1_intraday_ret",
    "t1_intraday_adj",
    "t1_low_drawdown_adj",
    "t1_high_upside_adj",
    "t1_close_change_adj",
    "t2_close_vs_t1_open_adj",
    "t2_high_vs_t1_open_adj",
    "gross_return",
    "net_return",
)

# 买入失败类 exit_status（无持仓、不计入已实现统计）
BUY_FAIL_STATUSES = (
    "无T+1开盘价",
    "无T日收盘价",
    "无T+1最低价",
    "限价未成交",
    "买入价无效",
)

# 现金计算输入列名（与 build_cash_input 的输出列对应）
OPTIONAL_T1_CLOSE_COLUMN = "次日收盘价(不复权)"
OPTIONAL_T_CLOSE_COLUMN = "命中日收盘价(不复权)"
OPTIONAL_T1_LOW_COLUMN = "次日最低价(不复权)"

# calc_trades 输出列（顺序与报告/CSV 依赖保持一致）
TRADE_COLUMNS = [
    "hit_date",
    "ts_code",
    "name",
    "buy_date",
    "plan_sell_date",
    "sell_date",
    "exit_offset",
    "t1_intraday_ret",
    "exit_threshold",
    "exit_tier",
    "exit_mode",
    "delay_days",
    "lot_mode",
    "lots",
    "shares",
    "buy_base_raw",
    "buy_price_raw",
    "sell_price_raw",
    "buy_amount",
    "buy_fee",
    "sell_amount",
    "sell_fee",
    "net_pnl",
    "gross_return",
    "net_return",
    "exit_status",
    "is_delayed",
    "close_limit_down",
    "error",
]


def normalize_entry_gap_ranges(
    value,
) -> tuple[tuple[float, float], ...]:
    """把单区间或区间并集规整为 tuple[tuple[lo, hi], ...]。

    支持：
    - (lo, hi)
    - ((lo, hi), (lo2, hi2))
    - [lo, hi] / [lo, hi, lo2, hi2]（CLI 的扁平写法）
    """
    if value is None:
        raise ValueError("T+1 买入低开区间不能为空")
    items = list(value)
    if not items:
        raise ValueError("T+1 买入低开区间不能为空")

    if all(isinstance(item, (list, tuple)) for item in items):
        raw_ranges = items
    elif all(not isinstance(item, (list, tuple)) for item in items):
        if len(items) % 2 != 0:
            raise ValueError("T+1 买入低开区间需要偶数个数值：每两个数组成一个闭区间")
        raw_ranges = [(items[i], items[i + 1]) for i in range(0, len(items), 2)]
    else:
        raise ValueError("T+1 买入低开区间不能混用数值和区间")

    ranges: list[tuple[float, float]] = []
    for item in raw_ranges:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"区间必须是 (下限, 上限)：{item!r}")
        try:
            lo = float(item[0])
            hi = float(item[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"区间边界必须是数字：{item!r}") from exc
        if not np.isfinite(lo) or not np.isfinite(hi):
            raise ValueError(f"区间边界必须是有限数字：{item!r}")
        if lo > hi:
            raise ValueError(f"区间下限不能大于上限：{item!r}")
        ranges.append((lo, hi))

    # 排序并去重；重叠区间不合并，过滤时按并集逐段 OR 即可。
    ranges = sorted(set(ranges), key=lambda pair: (pair[0], pair[1]))
    return tuple(ranges)


def fmt_entry_gap_ranges(value) -> str:
    """把单区间/并集格式化为 [-6.00%, -1.00%] ∪ [-9.00%, -8.00%]。"""
    ranges = normalize_entry_gap_ranges(value)
    return " ∪ ".join(f"[{lo:.2%}, {hi:.2%}]" for lo, hi in ranges)


def parse_args(
    argv: list[str] | None = None, description: str | None = None
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=description
        or (
            "limit_up_shakeout 命中样本二次筛选后的现金盈亏"
            "（整手、费用、滑点、跌停顺延）"
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
        "--gap",
        nargs=2,
        type=float,
        default=DEFAULT_GAP,
        metavar=("MIN", "MAX"),
        help=f"T 日高开闭区间（小数），默认 {DEFAULT_GAP}",
    )
    parser.add_argument(
        "--close-chg",
        nargs=2,
        type=float,
        default=DEFAULT_CLOSE_CHG,
        metavar=("MIN", "MAX"),
        help=f"T 日收盘涨跌幅闭区间（小数），默认 {DEFAULT_CLOSE_CHG}",
    )
    parser.add_argument(
        "--n-days-rise",
        nargs="+",
        default=None,
        metavar="VAL",
        help=(
            '近 N 日涨幅条件："unlimited" 表示不限；或 "N MIN MAX" 表示'
            "近 N 日涨幅 ∈ [MIN, MAX]（小数，MIN/MAX 可写 none 表示该侧不设限）。"
            f"默认 {DEFAULT_N_DAYS_RISE}"
        ),
    )
    parser.add_argument(
        "--entry-gap",
        nargs="+",
        type=float,
        default=None,
        metavar="PCT",
        help=(
            "T+1 买入低开闭区间（小数，原始价，相对 T 日收盘）。"
            "可写 2 个数（单区间）或 2N 个数（多区间并集），"
            "例如：--entry-gap -0.06 -0.01 -0.09 -0.08。"
            f"默认 {fmt_entry_gap_ranges(DEFAULT_ENTRY_GAP).replace('%', '%%')}"
        ),
    )
    parser.add_argument(
        "--delta",
        type=float,
        default=DEFAULT_DELTA,
        metavar="PCT",
        help=(
            "T+1 限价买入下探深度（百分点，1.0 即 1%%）：买入基准价 = "
            "(1 + a - delta%%) × close(T)，a = open(T+1)/close(T)-1（不复权）；"
            "delta>0 时 T+1 最低价 <= 基准价才成交（按基准价成交），"
            "日内未触及则该样本不交易；0 = 开盘价买入（原规则）；"
            f"默认 {DEFAULT_DELTA:g}"
        ),
    )
    parser.add_argument(
        "--require-low-support",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_REQUIRE_LOW_SUPPORT,
        help=(
            "是否要求信号日最低价 >= 前一日收盘价（后复权 "
            "low(T) / close(T-1)），默认要求；用 --no-require-low-support 关闭"
        ),
    )
    parser.add_argument(
        "--exit-tiers",
        default=None,
        help=(
            "动态止盈分档 JSON：[[下界, 上界, 止盈率], ...]，区间左开右闭，"
            "下界/上界为 null 表示 -∞/+∞；默认 "
            f"{json.dumps(tiers_to_jsonable(DEFAULT_EXIT_TIERS), ensure_ascii=False)}"
        ),
    )
    parser.add_argument(
        "--open-exit",
        type=float,
        default=None,
        help="固定止盈阈值（退化为单段分档），与 --exit-tiers 互斥；缺省用动态分档",
    )
    parser.add_argument(
        "--position",
        type=float,
        default=DEFAULT_POSITION,
        help=f"每个样本投入资金上限（现金口径），默认 {DEFAULT_POSITION:,.0f}",
    )
    parser.add_argument(
        "--lot-mode",
        choices=("nearest", "floor"),
        default=DEFAULT_LOT_MODE,
        help="手数取整：nearest=最接近预算，floor=不超过预算",
    )
    parser.add_argument(
        "--sell-n",
        "--hold-days",
        dest="sell_n",
        type=int,
        default=DEFAULT_SELL_N,
        metavar="N",
        help=(
            "最长持有到 T+N（交易日），默认 "
            f"{DEFAULT_SELL_N}；T+1 买入后，T+2 起每日盘中触及止盈目标"
            "即卖出，一直未触发则 T+N 收盘卖出。N=2 等价于原来的 T+2 固定卖出"
        ),
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
        "--report",
        dest="generate_report",
        action="store_true",
        help=(
            "生成 report.md / CSV"
            f"（默认{'生成' if DEFAULT_GENERATE_REPORT else '不生成'}）"
        ),
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
    args = parser.parse_args(argv)
    args.n_days_rise = _resolve_cli_n_days_rise(args.n_days_rise)
    try:
        args.entry_gap = normalize_entry_gap_ranges(
            args.entry_gap if args.entry_gap is not None else DEFAULT_ENTRY_GAP
        )
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _parse_rise_bound(text: str) -> float | None:
    value = text.strip().lower()
    if value in ("none", "null", ""):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(
            f"--n-days-rise 的边界必须是数字或 none（当前：{text}）"
        ) from exc


def _resolve_cli_n_days_rise(values: list[str] | None):
    """把 --n-days-rise 的原始参数规整为 "unlimited" 或 (days, lo, hi)。"""
    if values is None:
        return DEFAULT_N_DAYS_RISE
    if len(values) == 1:
        text = values[0].strip().lower()
        if text in ("unlimited", "none", "off"):
            return "unlimited"
        raise SystemExit(
            "--n-days-rise 只传一个值时只支持 unlimited；"
            "区间写法为：--n-days-rise N MIN MAX"
        )
    if len(values) != 3:
        raise SystemExit("--n-days-rise 需要 'unlimited' 或 'N MIN MAX'")
    try:
        days = int(values[0])
    except ValueError as exc:
        raise SystemExit(f"--n-days-rise 的 N 必须是整数（当前：{values[0]}）") from exc
    try:
        cfg = normalize_n_days_rise(
            (days, _parse_rise_bound(values[1]), _parse_rise_bound(values[2]))
        )
    except ValueError as exc:
        raise SystemExit(f"--n-days-rise 无效：{exc}") from exc
    return "unlimited" if cfg is None else cfg


def resolve_exit_tiers(args: argparse.Namespace) -> tuple[tuple, str]:
    """解析止盈分档：--exit-tiers > --open-exit（固定单段） > 默认分档。"""
    if args.exit_tiers is not None and args.open_exit is not None:
        raise SystemExit("--exit-tiers 与 --open-exit 互斥，请只传其一")
    if args.exit_tiers is not None:
        try:
            return parse_exit_tiers(args.exit_tiers), "命令行 --exit-tiers"
        except ValueError as exc:
            raise SystemExit(f"--exit-tiers 无效：{exc}") from exc
    if args.open_exit is not None:
        try:
            return tiers_from_scalar(args.open_exit), "命令行 --open-exit（固定阈值）"
        except ValueError as exc:
            raise SystemExit(f"--open-exit 无效：{exc}") from exc
    return DEFAULT_EXIT_TIERS, "默认 DEFAULT_EXIT_TIERS"


def compact_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def resolve_end_date(value: str, hit_table: str = "hit_limit_up_shakeout") -> str:
    if value is None or str(value).strip().lower() in ("", "latest", "none"):
        max_hit = db.scalar(f"SELECT MAX(trade_date) FROM {hit_table}")
        if not max_hit:
            raise SystemExit(f"{hit_table} 为空，请先执行 hits update")
        return str(max_hit)
    return compact_date(value)


def load_hits(
    start: str, end: str, hit_table: str = "hit_limit_up_shakeout"
) -> pd.DataFrame:
    df = db.read_df(
        f"""
        SELECT trade_date, ts_code, name, industry
        FROM {hit_table}
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY trade_date, ts_code
        """,
        (start, end),
    )
    if df.empty:
        return df
    df = df.rename(columns={"trade_date": "hit_date"})
    df["hit_date"] = df["hit_date"].astype(str)
    df["ts_code"] = df["ts_code"].astype(str)
    df["name"] = df["name"].fillna("").astype(str)
    df["industry"] = df["industry"].fillna("").astype(str)
    return df


def load_calendar() -> list[str]:
    df = db.read_df(
        """
        SELECT cal_date
        FROM trade_cal
        WHERE exchange = 'SSE' AND is_open = 1
        ORDER BY cal_date
        """
    )
    return df["cal_date"].astype(str).tolist()


def attach_dates(
    hits: pd.DataFrame,
    calendar: list[str],
    n_days_rise,
    sell_n: int = DEFAULT_SELL_N,
) -> pd.DataFrame:
    date_pos = {d: i for i, d in enumerate(calendar)}
    out = hits.copy()
    out["cal_pos"] = out["hit_date"].map(date_pos)

    offsets = [(-1, "prev_date"), (1, "t1_date")]
    rise_cfg = normalize_n_days_rise(n_days_rise)
    if rise_cfg is not None:
        offsets.append((-rise_cfg[0], "rise_ref_date"))
    for offset, name in offsets:
        values: list[str | None] = []
        for pos in out["cal_pos"]:
            if pd.isna(pos):
                values.append(None)
                continue
            target = int(pos) + offset
            values.append(calendar[target] if 0 <= target < len(calendar) else None)
        out[name] = values

    # 卖出窗口：T+2 .. T+N（含端点）。T+1 买入当天不可卖（A 股 T+1）。
    out["sell_dates"] = [
        calendar[int(pos) + 2 : int(pos) + 1 + sell_n] if pd.notna(pos) else []
        for pos in out["cal_pos"]
    ]
    for k in range(2, sell_n + 1):
        out[f"t{k}_date"] = out["sell_dates"].map(
            lambda dates, i=k - 2: dates[i] if len(dates) > i else None
        )
    out["final_sell_date"] = out["sell_dates"].map(
        lambda dates: dates[-1] if dates else None
    )
    return out


def normalize_n_days_rise(value):
    """把 DEFAULT_N_DAYS_RISE / CLI 值规整为 (days, lo, hi)；不限时返回 None。

    支持：
    - None / "unlimited" / "none" / "off"：不限；
    - (days, lo, hi)：近 days 日涨幅 ∈ [lo, hi]，lo/hi 为 None 表示该侧不设限。
    """
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() in ("", "unlimited", "none", "off"):
            return None
        raise ValueError(f'只支持 "unlimited" 或 (N, MIN, MAX)，当前：{value!r}')
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"需要 (N, MIN, MAX) 三个值，当前：{value!r}")
    days_raw, lo_raw, hi_raw = value
    if isinstance(days_raw, bool):
        raise ValueError(f"N 必须是正整数，当前：{days_raw!r}")
    try:
        days = int(days_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"N 必须是正整数，当前：{days_raw!r}") from exc
    if days <= 0:
        raise ValueError(f"N 必须是正整数，当前：{days_raw!r}")

    def to_bound(raw, name: str):
        if raw is None:
            return None
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in ("", "none", "null"):
                return None
            raw = text
        if isinstance(raw, bool):
            raise ValueError(f"{name} 必须是数字或 None，当前：{raw!r}")
        try:
            bound = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是数字或 None，当前：{raw!r}") from exc
        if not np.isfinite(bound):
            raise ValueError(f"{name} 必须是有限数字或 None，当前：{raw!r}")
        return bound

    lo = to_bound(lo_raw, "MIN")
    hi = to_bound(hi_raw, "MAX")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"MIN 必须 <= MAX（当前 {lo} > {hi}）")
    if lo is None and hi is None:
        return None
    return days, lo, hi


def fmt_n_days_rise(value) -> str:
    """人读的涨幅条件，如“近5日涨幅 ∈ [-10.00%, 10.00%]”或“不限”。"""
    cfg = normalize_n_days_rise(value)
    if cfg is None:
        return "不限（不要求 T-N 行情）"
    days, lo, hi = cfg
    if lo is None:
        return f"近{days}日涨幅 <= {hi:.2%}"
    if hi is None:
        return f"近{days}日涨幅 >= {lo:.2%}"
    return f"近{days}日涨幅 ∈ [{lo:.2%}, {hi:.2%}]"


def fmt_n_days_rise_cli(value) -> str:
    """命令行 --n-days-rise 的写法。"""
    cfg = normalize_n_days_rise(value)
    if cfg is None:
        return "unlimited"
    days, lo, hi = cfg
    lo_text = "none" if lo is None else f"{lo:g}"
    hi_text = "none" if hi is None else f"{hi:g}"
    return f"{days} {lo_text} {hi_text}"


def load_prices(codes: list[str], min_date: str, max_date: str) -> pd.DataFrame:
    """按代码 + 日期区间整段加载行情（返回区间内这些代码的全部日线）。

    只在确实需要逐日连续行情时使用；命中样本分析通常只用到少数角色日期，
    优先用 load_prices_by_keys（主键精确取数，行数少几个数量级）。
    """
    frames = []
    for i in range(0, len(codes), PRICE_CHUNK):
        chunk = codes[i : i + PRICE_CHUNK]
        placeholders = ",".join(["%s"] * len(chunk))
        frames.append(
            db.read_df(
                f"""
                SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                       d.pre_close, d.amount, a.adj_factor, l.down_limit,
                       b.circ_mv
                FROM daily d
                LEFT JOIN adj_factor a
                  ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date
                LEFT JOIN stk_limit l
                  ON l.ts_code = d.ts_code AND l.trade_date = d.trade_date
                LEFT JOIN daily_basic b
                  ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date
                WHERE d.ts_code IN ({placeholders})
                  AND d.trade_date BETWEEN %s AND %s
                """,
                (*chunk, min_date, max_date),
            )
        )
    prices = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return _finalize_prices(prices)


def load_prices_by_keys(keys, chunk_size: int = PRICE_KEY_CHUNK) -> pd.DataFrame:
    """按 (ts_code, trade_date) 精确取行情（主键 row constructor IN）。

    脚本只需要每个命中样本的少数角色日期（T-5/T-1/T/T+1/T+2，以及可能的顺延
    窗口）。按“代码 IN (...) AND 日期区间”整段加载会把区间内这些代码的全部
    日线都拉回来（本机实测约 410 万行，网络差时单次 2~3 分钟）；这里逐键精确
    取数后通常只有几万行。
    """
    unique = sorted(
        {
            (str(code), str(date))
            for code, date in keys
            if pd.notna(code) and pd.notna(date)
        }
    )
    if not unique:
        return pd.DataFrame()
    frames = []
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i : i + chunk_size]
        placeholders = ",".join(["(%s, %s)"] * len(chunk))
        params = [value for pair in chunk for value in pair]
        frames.append(
            db.read_df(
                f"""
                SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
                       d.pre_close, d.amount, a.adj_factor, l.down_limit,
                       b.circ_mv
                FROM daily d
                LEFT JOIN adj_factor a
                  ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date
                LEFT JOIN stk_limit l
                  ON l.ts_code = d.ts_code AND l.trade_date = d.trade_date
                LEFT JOIN daily_basic b
                  ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date
                WHERE (d.ts_code, d.trade_date) IN ({placeholders})
                """,
                params,
            )
        )
    prices = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return _finalize_prices(prices)


def _finalize_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if prices.empty:
        return prices
    for col in (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "amount",
        "adj_factor",
        "down_limit",
        "circ_mv",
    ):
        prices[col] = pd.to_numeric(prices[col], errors="coerce")
    prices["adj_open"] = prices["open"] * prices["adj_factor"]
    prices["adj_high"] = prices["high"] * prices["adj_factor"]
    prices["adj_low"] = prices["low"] * prices["adj_factor"]
    prices["adj_close"] = prices["close"] * prices["adj_factor"]
    prices["trade_date"] = prices["trade_date"].astype(str)
    prices["ts_code"] = prices["ts_code"].astype(str)
    return prices


def price_keys_for_hits(hits: pd.DataFrame) -> set[tuple[str, str]]:
    """命中样本筛选阶段需要的全部 (代码, 日期) 键。"""
    keys: set[tuple[str, str]] = set()
    for date_col in ("hit_date", "prev_date", "rise_ref_date", "t1_date"):
        if date_col not in hits.columns:
            continue
        for code, date in zip(hits["ts_code"], hits[date_col]):
            if pd.notna(date):
                keys.add((str(code), str(date)))
    if "sell_dates" in hits.columns:
        for code, dates in zip(hits["ts_code"], hits["sell_dates"]):
            for date in dates or []:
                keys.add((str(code), str(date)))
    return keys


def delay_price_keys(
    samples: pd.DataFrame, calendar: list[str], max_delay_days: int
) -> set[tuple[str, str]]:
    """顺延卖出可能用到的 (代码, 日期) 键。

    只有“T+N 收盘卖出且收盘跌停”的样本才会进入顺延分支；取这些样本之后
    max_delay_days 个交易日，保证 calc_trades 的顺延查价全部命中缓存。
    """
    if max_delay_days <= 0:
        return set()
    date_pos = {d: i for i, d in enumerate(calendar)}
    candidates = samples.loc[
        samples["included"] & samples["sell_limit_down"].astype(bool),
        ["ts_code", "final_sell_date"],
    ]
    keys: set[tuple[str, str]] = set()
    for code, final_sell_date in zip(
        candidates["ts_code"], candidates["final_sell_date"]
    ):
        pos = date_pos.get(str(final_sell_date))
        if pos is None:
            continue
        for date in calendar[pos + 1 : pos + 1 + max_delay_days]:
            keys.add((str(code), date))
    return keys


def _merge_role(
    samples: pd.DataFrame, prices: pd.DataFrame, date_col: str, columns: dict[str, str]
) -> pd.DataFrame:
    right = prices[["ts_code", "trade_date", *columns]].rename(
        columns={"trade_date": date_col, **columns}
    )
    return samples.merge(right, on=["ts_code", date_col], how="left")


def build_samples(
    hits: pd.DataFrame,
    prices: pd.DataFrame,
    gap_range: tuple[float, float],
    close_chg_range: tuple[float, float],
    n_days_rise,
    exit_tiers: tuple,
    entry_gap_ranges: tuple[tuple[float, float], ...] | tuple[float, float],
    delta_pct: float = 0.0,
    require_low_support: bool = False,
    compute_reasons: bool = True,
    sell_n: int = DEFAULT_SELL_N,
) -> pd.DataFrame:
    """构建筛选样本框架（理论口径与现金口径共用）。

    n_days_rise 为近 N 日涨幅条件："unlimited" 或 (N, MIN, MAX)；
    MIN/MAX 为 None 表示该侧不设限。不限时不要求 T-N 行情。

    entry_gap_ranges 为 T+1 原始价高开比例的闭区间并集：
    每个区间为 (open_raw(T+1) - close_raw(T)) / close_raw(T) 的上下界。
    可传单个 (lo, hi)，也可传 ((lo, hi), (lo2, hi2)) 表示并集。

    delta_pct 为 T+1 限价买入下探深度（百分点）：0 = 开盘价买入（原规则）；
    >0 时买入价 = open(T+1) - (delta_pct/100)×close(T)，T+1 最低价触及才成交，
    未成交样本不纳入（included=False）。止盈分档涨幅与卖出价均以买入价为基准。

    sell_n 为最长持有到 T+N（默认 2）：T+2 起逐日检查，开盘高于目标价按开盘价
    卖出、盘中最高价触及目标价按目标价卖出；一直未触发则 T+N 收盘卖出。

    compute_reasons=False 时跳过逐行排除原因（网格搜索用不到，可省数秒）。
    """
    entry_gap_ranges = normalize_entry_gap_ranges(entry_gap_ranges)
    rise_cfg = normalize_n_days_rise(n_days_rise)
    samples = _merge_role(
        hits,
        prices,
        "hit_date",
        {
            "open": "t_open_raw",
            "close": "t_close_raw",
            "pre_close": "t_pre_close_raw",
            "amount": "t_amount",
            "circ_mv": "t_circ_mv",
            "adj_open": "t_adj_open",
            "adj_low": "t_adj_low",
            "adj_close": "t_adj_close",
        },
    )
    samples = _merge_role(
        samples,
        prices,
        "prev_date",
        {
            "close": "prev_close_raw",
            "amount": "prev_amount",
            "adj_close": "prev_adj_close",
        },
    )
    if rise_cfg is not None:
        if "rise_ref_date" not in samples.columns:
            raise ValueError(
                "启用近 N 日涨幅条件时 hits 必须包含 rise_ref_date；"
                "请用相同的 n_days_rise 调用 attach_dates"
            )
        samples = _merge_role(
            samples, prices, "rise_ref_date", {"adj_close": "rise_ref_adj_close"}
        )
    samples = _merge_role(
        samples,
        prices,
        "t1_date",
        {
            "open": "t1_open_raw",
            "close": "t1_close_raw",
            "high": "t1_high_raw",
            "low": "t1_low_raw",
            "pre_close": "t1_pre_close_raw",
            "amount": "t1_amount",
            "adj_open": "t1_adj_open",
            "adj_high": "t1_adj_high",
            "adj_low": "t1_adj_low",
            "adj_close": "t1_adj_close",
        },
    )
    for k in range(2, sell_n + 1):
        samples = _merge_role(
            samples,
            prices,
            f"t{k}_date",
            {
                "open": f"t{k}_open_raw",
                "high": f"t{k}_high_raw",
                "close": f"t{k}_close_raw",
                "down_limit": f"t{k}_down_limit",
                "adj_open": f"t{k}_adj_open",
                "adj_high": f"t{k}_adj_high",
                "adj_close": f"t{k}_adj_close",
            },
        )

    # 多次 merge 后 DataFrame 可能高度碎片化，这里复制一次以合并底层块。
    samples = samples.copy()

    samples["gap_open"] = samples["t_open_raw"] / samples["t_pre_close_raw"] - 1.0
    samples["gap_open_prev_raw"] = (
        samples["t_open_raw"] / samples["prev_close_raw"] - 1.0
    )
    # T 日收盘涨跌幅：同样用交易所除权后的昨收，避免分红/送转造成失真。
    samples["close_chg"] = samples["t_close_raw"] / samples["t_pre_close_raw"] - 1.0
    # T+1 买入低开：统一用原始价（软件显示价格），相对 T 日收盘：
    # (open_raw(T+1) - close_raw(T)) / close_raw(T)
    # 这是 --entry-gap / DEFAULT_ENTRY_GAP 实际使用的口径。
    samples["entry_gap"] = (samples["t1_open_raw"] - samples["t_close_raw"]) / samples[
        "t_close_raw"
    ]
    # 兼容旧口径：T+1 交易所除权后昨收（普通交易日等于 T 日收盘，
    # 除权除息日会与软件显示的原始价高开比例不同）。仅作诊断字段，不参与筛选。
    samples["entry_gap_pre_close_raw"] = (
        samples["t1_open_raw"] / samples["t1_pre_close_raw"] - 1.0
    )
    # 保留原字段名，等价于 entry_gap；cash_trades.csv 用它导出。
    samples["entry_gap_close_raw"] = samples["entry_gap"]
    if rise_cfg is not None:
        samples["ret_rise"] = (
            samples["t_adj_close"] / samples["rise_ref_adj_close"] - 1.0
        )
    else:
        samples["ret_rise"] = np.nan

    # ------------------------------------------------------------------
    # T / T+1 / T+2 结构指标（后复权）
    samples["t1_intraday_adj"] = samples["t1_adj_close"] / samples["t1_adj_open"] - 1.0
    samples["t2_close_vs_t1_open_adj"] = (
        samples["t2_adj_close"] / samples["t1_adj_open"] - 1.0
    )
    samples["t2_high_vs_t1_open_adj"] = (
        samples["t2_adj_high"] / samples["t1_adj_open"] - 1.0
    )
    samples["t1_low_drawdown_adj"] = (
        samples["t1_adj_open"] - samples["t1_adj_low"]
    ) / samples["t_adj_close"]
    samples["t1_high_upside_adj"] = (
        samples["t1_adj_high"] - samples["t1_adj_open"]
    ) / samples["t_adj_close"]
    samples["t1_close_change_adj"] = (
        samples["t1_adj_close"] - samples["t1_adj_open"]
    ) / samples["t_adj_close"]
    samples["t_open_vs_prev_close_adj"] = (
        samples["t_adj_open"] - samples["prev_adj_close"]
    ) / samples["prev_adj_close"]
    samples["t_close_vs_prev_close_adj"] = (
        samples["t_adj_close"] - samples["prev_adj_close"]
    ) / samples["prev_adj_close"]
    samples["t_low_vs_prev_close_adj"] = (
        samples["t_adj_low"] - samples["prev_adj_close"]
    ) / samples["prev_adj_close"]
    samples["amount_ratio_t_prev"] = samples["t_amount"] / samples["prev_amount"]
    samples["amount_ratio_t1_t"] = samples["t1_amount"] / samples["t_amount"]

    # T+1 K 线形态（同日复权因子相消，用不复权价比较即可）
    samples["t1_kline"] = ""
    kline_valid = samples["t1_open_raw"].notna() & samples["t1_close_raw"].notna()
    samples.loc[
        kline_valid & (samples["t1_close_raw"] > samples["t1_open_raw"]),
        "t1_kline",
    ] = "阳线"
    samples.loc[
        kline_valid & (samples["t1_close_raw"] < samples["t1_open_raw"]),
        "t1_kline",
    ] = "阴线"
    samples.loc[
        kline_valid & (samples["t1_close_raw"] == samples["t1_open_raw"]),
        "t1_kline",
    ] = "开=收"

    # ------------------------------------------------------------------
    # T+1 买入价与限价成交判定（不复权）：
    # a = open(T+1)/close(T) - 1，买入价 = (1 + a - delta) × close(T)，
    # 等价于 open(T+1) - delta × close(T)；delta=0 时即开盘价买入（原规则）。
    # delta>0 视为开盘下方限价单：T+1 最低价 <= 买入价才算成交（按买入价成交）。
    delta_frac = float(delta_pct) / 100.0
    if delta_frac > 0:
        samples["buy_price_raw"] = (
            samples["t1_open_raw"] - delta_frac * samples["t_close_raw"]
        )
        samples["buy_filled"] = (
            samples["buy_price_raw"].gt(0)
            & samples["t1_low_raw"].notna()
            & samples["t1_low_raw"].le(samples["buy_price_raw"] + LIMIT_EPS)
        )
        # 买入价后复权（同日复权因子 = t1_adj_open / t1_open_raw）
        samples["buy_adj"] = samples["buy_price_raw"] * (
            samples["t1_adj_open"] / samples["t1_open_raw"]
        )
    else:
        samples["buy_price_raw"] = samples["t1_open_raw"]
        samples["buy_adj"] = samples["t1_adj_open"]
        samples["buy_filled"] = samples["t1_open_raw"].notna()

    # 止盈分档涨幅：T+1 收盘对买入价的涨幅；delta=0 时即原日内涨幅 close/open-1
    # （买入价与收盘价同为不复权，比值不受复权影响）
    samples["t1_intraday_ret"] = (
        samples["t1_close_raw"] / samples["buy_price_raw"] - 1.0
    )
    # 逐样本动态止盈阈值：按 T+1 收盘对买入价涨幅分档；缺失/无效为 NaN
    samples["exit_threshold"] = apply_exit_thresholds(
        samples["t1_intraday_ret"], exit_tiers
    )
    samples["exit_tier"] = pd.Categorical.from_codes(
        apply_exit_tier_codes(samples["t1_intraday_ret"], exit_tiers).to_numpy(),
        categories=list(tier_labels(exit_tiers)),
        ordered=True,
    )
    # T+2..T+N 各价对买入价的涨幅（后复权/不复权）：
    # 卖出判定从 T+2 开始，逐日检查开盘/最高价是否触及止盈目标；
    # 一直未触发则在 T+N 收盘卖出。
    for k in range(2, sell_n + 1):
        samples[f"t{k}_open_ret"] = samples[f"t{k}_adj_open"] / samples["buy_adj"] - 1.0
        samples[f"t{k}_high_ret"] = samples[f"t{k}_adj_high"] / samples["buy_adj"] - 1.0
        samples[f"t{k}_close_ret"] = (
            samples[f"t{k}_adj_close"] / samples["buy_adj"] - 1.0
        )
        samples[f"t{k}_open_ret_raw"] = (
            samples[f"t{k}_open_raw"] / samples["buy_price_raw"] - 1.0
        )
        samples[f"t{k}_high_ret_raw"] = (
            samples[f"t{k}_high_raw"] / samples["buy_price_raw"] - 1.0
        )
        samples[f"t{k}_close_ret_raw"] = (
            samples[f"t{k}_close_raw"] / samples["buy_price_raw"] - 1.0
        )

    # 逐日扫描卖出窗口：先看开盘（跳空高于目标价），再看盘中最高价
    # （触及目标价即按目标价成交）。一旦触发即锁定卖出日，后续不再参与。
    samples["exit_at_open"] = False
    samples["exit_intraday"] = False
    samples["exit_offset"] = np.nan
    samples["exit_date"] = None
    samples["exit_price_raw"] = np.nan
    samples["return_adj"] = np.nan
    samples["return_raw"] = np.nan
    samples["exit_type"] = None
    samples["exit_status"] = "已卖出"
    samples["exit_error"] = ""

    target_raw = samples["buy_price_raw"] * (1.0 + samples["exit_threshold"])
    remaining = samples["buy_filled"].astype(bool) & samples["exit_threshold"].notna()

    for k in range(2, sell_n + 1):
        open_ret = samples[f"t{k}_open_ret"]
        high_ret = samples[f"t{k}_high_ret"]
        open_raw = samples[f"t{k}_open_raw"]
        high_raw = samples[f"t{k}_high_raw"]
        date_col = samples[f"t{k}_date"]

        hit_open = remaining & open_ret.notna() & (open_ret > samples["exit_threshold"])
        hit_intraday = (
            remaining
            & ~hit_open
            & high_ret.notna()
            & (high_ret >= samples["exit_threshold"])
        )

        samples.loc[hit_open, "exit_at_open"] = True
        samples.loc[hit_open, "return_adj"] = open_ret[hit_open]
        samples.loc[hit_open, "return_raw"] = (
            open_raw[hit_open] / samples.loc[hit_open, "buy_price_raw"] - 1.0
        )
        samples.loc[hit_open, "exit_price_raw"] = open_raw[hit_open]
        samples.loc[hit_open, "exit_type"] = f"T+{k} 开盘"
        samples.loc[hit_open, "exit_date"] = date_col[hit_open]
        samples.loc[hit_open, "exit_offset"] = k

        samples.loc[hit_intraday, "exit_intraday"] = True
        samples.loc[hit_intraday, "return_adj"] = samples.loc[
            hit_intraday, "exit_threshold"
        ]
        samples.loc[hit_intraday, "return_raw"] = samples.loc[
            hit_intraday, "exit_threshold"
        ]
        samples.loc[hit_intraday, "exit_price_raw"] = np.minimum(
            target_raw[hit_intraday], high_raw[hit_intraday]
        )
        samples.loc[hit_intraday, "exit_type"] = f"T+{k} 盘中"
        samples.loc[hit_intraday, "exit_date"] = date_col[hit_intraday]
        samples.loc[hit_intraday, "exit_offset"] = k

        remaining &= ~(hit_open | hit_intraday)

    # 整个窗口都未触及目标价：按 T+N 收盘卖出。
    last_k = sell_n
    last_close_ret = samples[f"t{last_k}_close_ret"]
    hit_close = remaining & last_close_ret.notna()
    samples.loc[hit_close, "return_adj"] = last_close_ret[hit_close]
    samples.loc[hit_close, "return_raw"] = (
        samples.loc[hit_close, f"t{last_k}_close_raw"]
        / samples.loc[hit_close, "buy_price_raw"]
        - 1.0
    )
    samples.loc[hit_close, "exit_price_raw"] = samples.loc[
        hit_close, f"t{last_k}_close_raw"
    ]
    samples.loc[hit_close, "exit_type"] = f"T+{last_k} 收盘"
    samples.loc[hit_close, "exit_date"] = samples.loc[hit_close, f"t{last_k}_date"]
    samples.loc[hit_close, "exit_offset"] = last_k

    # 卖出日（T+N）收盘是否跌停；只对“T+N 收盘卖出”分支做统计/顺延标记。
    samples["sell_down_limit_missing"] = False
    samples["sell_limit_down"] = False
    last_down_limit = samples[f"t{last_k}_down_limit"]
    last_close_raw = samples[f"t{last_k}_close_raw"]
    samples.loc[hit_close, "sell_down_limit_missing"] = (
        last_down_limit.isna() | last_down_limit.le(0)
    )[hit_close]
    samples.loc[hit_close, "sell_limit_down"] = (
        last_down_limit.gt(0) & last_close_raw.le(last_down_limit + LIMIT_EPS)
    ).fillna(False)[hit_close]

    # 无法分档 / 整个卖出窗口都没有可用行情
    no_threshold = samples["buy_filled"].astype(bool) & samples["exit_threshold"].isna()
    samples.loc[no_threshold, "exit_status"] = "无法分档"
    samples.loc[no_threshold, "exit_error"] = (
        "T+1收盘对买入价涨幅无效，无法落入任何止盈档位"
    )
    no_exit = (
        samples["buy_filled"].astype(bool)
        & samples["exit_threshold"].notna()
        & samples["return_adj"].isna()
    )
    samples.loc[no_exit, "exit_status"] = f"无T+{sell_n}行情"
    samples.loc[no_exit, "exit_error"] = f"缺少T+2..T+{sell_n}卖出行情"

    numeric_cols = [
        "gap_open",
        "gap_open_prev_raw",
        "close_chg",
        "entry_gap",
        "entry_gap_pre_close_raw",
        "entry_gap_close_raw",
        "ret_rise",
        "t1_intraday_ret",
        "exit_threshold",
        "return_adj",
        "return_raw",
        "t1_intraday_adj",
        "t2_close_vs_t1_open_adj",
        "t2_high_vs_t1_open_adj",
        "t1_low_drawdown_adj",
        "t1_high_upside_adj",
        "t1_close_change_adj",
        "t_open_vs_prev_close_adj",
        "t_close_vs_prev_close_adj",
        "t_low_vs_prev_close_adj",
        "amount_ratio_t_prev",
        "amount_ratio_t1_t",
    ]
    for k in range(2, sell_n + 1):
        numeric_cols.extend(
            [
                f"t{k}_open_ret",
                f"t{k}_high_ret",
                f"t{k}_close_ret",
                f"t{k}_open_ret_raw",
                f"t{k}_high_ret_raw",
                f"t{k}_close_ret_raw",
            ]
        )
    for col in numeric_cols:
        samples[col] = samples[col].replace([np.inf, -np.inf], np.nan)

    # 闭区间：两端都包含，允许恰好等于边界。
    gap_lo, gap_hi = gap_range
    close_lo, close_hi = close_chg_range
    samples["passed_gap"] = samples["gap_open"].ge(gap_lo - GAP_EPS) & samples[
        "gap_open"
    ].le(gap_hi + GAP_EPS)
    samples["passed_close"] = samples["close_chg"].ge(close_lo - GAP_EPS) & samples[
        "close_chg"
    ].le(close_hi + GAP_EPS)
    if rise_cfg is None:
        samples["passed_rise"] = True
    else:
        _, rise_lo, rise_hi = rise_cfg
        passed_rise = pd.Series(True, index=samples.index)
        if rise_lo is not None:
            passed_rise &= samples["ret_rise"].ge(rise_lo - GAP_EPS)
        if rise_hi is not None:
            passed_rise &= samples["ret_rise"].le(rise_hi + GAP_EPS)
        samples["passed_rise"] = passed_rise

    # T+1 低开区间支持多段并集：任一区间命中即通过（闭区间）。
    passed_entry_gap = pd.Series(False, index=samples.index)
    for entry_lo, entry_hi in entry_gap_ranges:
        passed_entry_gap |= samples["entry_gap"].ge(entry_lo - GAP_EPS) & samples[
            "entry_gap"
        ].le(entry_hi + GAP_EPS)
    samples["passed_entry_gap"] = passed_entry_gap
    # 可选条件：信号日最低价（后复权）不低于前一交易日收盘价（后复权）。
    samples["passed_low_support"] = samples["t_adj_low"].ge(samples["prev_adj_close"])
    included_mask = (
        samples["passed_gap"]
        & samples["passed_close"]
        & samples["passed_rise"]
        & samples["passed_entry_gap"]
    )
    if require_low_support:
        included_mask &= samples["passed_low_support"]
    samples["included"] = (
        included_mask & samples["return_adj"].notna() & samples["buy_filled"]
    )

    for src, dst in (
        ("gap_open", "gap_open_pct"),
        ("gap_open_prev_raw", "gap_open_prev_raw_pct"),
        ("close_chg", "close_chg_pct"),
        ("entry_gap", "entry_gap_pct"),
        ("entry_gap_pre_close_raw", "entry_gap_pre_close_raw_pct"),
        ("entry_gap_close_raw", "entry_gap_close_raw_pct"),
        ("ret_rise", "ret_rise_pct"),
        ("t1_intraday_ret", "t1_intraday_ret_pct"),
        ("exit_threshold", "exit_threshold_pct"),
        ("t2_open_ret", "t2_open_ret_pct"),
        ("t2_high_ret", "t2_high_ret_pct"),
        ("t2_close_ret", "t2_close_ret_pct"),
        ("return_adj", "return_pct"),
        ("return_raw", "return_raw_pct"),
    ):
        samples[dst] = samples[src] * 100.0

    samples["year"] = samples["hit_date"].str[:4]

    def gap_bucket(value: float) -> str | None:
        if pd.isna(value):
            return None
        lo, hi = gap_range
        if value < lo - GAP_EPS:
            return f"< {lo:.0%}"
        if value > hi + GAP_EPS:
            return f"> {hi:.0%}"
        width = hi - lo
        if width <= 0:
            return f"{lo:.0%} ~ {hi:.0%}"
        step = width / 3
        if value <= lo + step:
            return f"{lo:.0%} ~ {lo + step:.0%}"
        if value <= lo + 2 * step:
            return f"{lo + step:.0%} ~ {lo + 2 * step:.0%}"
        return f"{lo + 2 * step:.0%} ~ {hi:.0%}"

    samples["gap_bucket"] = samples["gap_open"].map(gap_bucket)
    if compute_reasons:
        samples["exclusion_reason"] = samples.apply(
            lambda row: _exclusion_reason(row, rise_cfg, require_low_support),
            axis=1,
        )
    else:
        samples["exclusion_reason"] = ""
    return samples


def _exclusion_reason(
    row: pd.Series,
    rise_cfg,
    require_low_support: bool = False,
) -> str:
    if bool(row["included"]):
        return ""
    reasons: list[str] = []
    if pd.isna(row["cal_pos"]):
        reasons.append("命中日不在交易日历")
    if pd.isna(row["gap_open"]):
        reasons.append("缺少T日开盘价或交易所昨收")
    elif not bool(row["passed_gap"]):
        reasons.append("T日高开不在区间")
    if pd.isna(row["close_chg"]):
        reasons.append("缺少T日收盘价或交易所昨收")
    elif not bool(row["passed_close"]):
        reasons.append("T日收盘涨跌幅不在区间")
    if require_low_support and not bool(row["passed_low_support"]):
        reasons.append("T日最低价低于前一交易日收盘价")
    if rise_cfg is not None:
        rise_days = rise_cfg[0]
        if pd.isna(row["rise_ref_date"]):
            reasons.append(f"命中日前不足{rise_days}个交易日")
        elif pd.isna(row["ret_rise"]):
            reasons.append(f"缺少T-{rise_days}行情")
        elif not bool(row["passed_rise"]):
            reasons.append(f"近{rise_days}日涨幅不在区间")
    if pd.isna(row["entry_gap"]):
        reasons.append("缺少T+1开盘价或T日收盘价")
    elif not bool(row["passed_entry_gap"]):
        reasons.append("T+1开盘不在区间")
    if pd.notna(row["buy_price_raw"]) and not bool(row["buy_filled"]):
        if pd.isna(row["t1_low_raw"]):
            reasons.append("缺少T+1最低价，无法判定限价成交")
        else:
            reasons.append("T+1日内未触及限价买入价")
    if pd.isna(row["t1_date"]) or pd.isna(row["final_sell_date"]):
        reasons.append("缺少T+1或T+N交易日")
    elif pd.isna(row["exit_threshold"]):
        if pd.isna(row["buy_price_raw"]):
            reasons.append("买入价缺失，无法确定止盈档位")
        else:
            reasons.append("缺少T+1收盘价，无法确定止盈档位")
    elif pd.isna(row["return_adj"]):
        reasons.append("缺少T+2..T+N卖出行情（停牌/行情未产生）")
    return "；".join(dict.fromkeys(reasons))


# ==============================================================================
# 现金盈亏
# ==============================================================================


def build_price_cache(prices: pd.DataFrame) -> dict:
    """把已加载的行情转成 load_price_row 的缓存，顺延卖出时避免逐笔查库。"""
    cache: dict = {}
    for row in prices.itertuples(index=False):
        key = (str(row.ts_code), str(row.trade_date))
        cache.setdefault(
            key,
            {
                "ts_code": row.ts_code,
                "trade_date": row.trade_date,
                **{
                    col: getattr(row, col)
                    for col in (
                        "open",
                        "high",
                        "close",
                        "pre_close",
                        "adj_factor",
                        "down_limit",
                    )
                },
            },
        )
    return cache


def load_price_row(code: str, trade_date: str, cache: dict) -> dict | None:
    key = (code, trade_date)
    if key in cache:
        return cache[key]
    df = db.read_df(
        """
        SELECT d.ts_code, d.trade_date, d.open, d.high, d.close, d.pre_close,
               a.adj_factor, l.down_limit
        FROM daily d
        LEFT JOIN adj_factor a
          ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date
        LEFT JOIN stk_limit l
          ON l.ts_code = d.ts_code AND l.trade_date = d.trade_date
        WHERE d.ts_code = %s AND d.trade_date = %s
        """,
        (code, trade_date),
    )
    if df.empty:
        cache[key] = None
        return None
    row = df.iloc[0].to_dict()
    for col in ("open", "high", "close", "pre_close", "adj_factor", "down_limit"):
        row[col] = pd.to_numeric(row.get(col), errors="coerce")
    cache[key] = row
    return row


def next_trade_dates(calendar: list[str], start_date: str, days: int) -> list[str]:
    try:
        pos = calendar.index(start_date)
    except ValueError:
        return []
    return calendar[pos + 1 : pos + 1 + days]


def to_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    return bool(value)


def is_limit_down(price: float | None, down_limit: float | None) -> bool:
    return (
        price is not None
        and down_limit is not None
        and np.isfinite(price)
        and np.isfinite(down_limit)
        and down_limit > 0
        and price <= down_limit + LIMIT_EPS
    )


def build_cash_input(included: pd.DataFrame) -> pd.DataFrame:
    """把 build_samples 的 included 样本转成现金计算的输入列。

    卖出路径（T+2..T+N 哪一天触发止盈、或 T+N 收盘卖出）已在 build_samples
    中按当前 exit_tiers / delta 扫描完成；calc_trades 只负责按 --delta 重新
    核对买入价，并把卖出计划换算成现金盈亏。
    """
    return pd.DataFrame(
        {
            "命中日期": included["hit_date"].astype(str),
            "股票代码": included["ts_code"].astype(str),
            "股票名称": included["name"].fillna("").astype(str),
            "命中日收盘价(不复权)": included["t_close_raw"],
            "次日日期": included["t1_date"].astype(str),
            "次日开盘价(不复权)": included["t1_open_raw"],
            "次日最低价(不复权)": included["t1_low_raw"],
            "次日收盘价(不复权)": included["t1_close_raw"],
            "T+1收盘对买入价涨幅": included["t1_intraday_ret"],
            "动态止盈阈值": included["exit_threshold"],
            "止盈区间": included["exit_tier"].astype(str),
            "计划卖出日": included["final_sell_date"].astype(str),
            "实际卖出日": included["exit_date"],
            "卖出偏移": included["exit_offset"],
            "卖出方式": included["exit_type"].fillna("").astype(str),
            "卖出价(不复权)": included["exit_price_raw"],
            "卖出日是否跌停": included["sell_limit_down"].astype(bool),
            "卖出状态": included["exit_status"].fillna("").astype(str),
            "卖出备注": included["exit_error"].fillna("").astype(str),
            # 兼容旧调用方（cash_grid_search / capital_cycle）：保留 T+2 原始列。
            "次次日日期": included["t2_date"].astype(str),
            "次次日开盘价(不复权)": included["t2_open_raw"],
            "次次日最高价(不复权)": included["t2_high_raw"],
            "次次日收盘价(不复权)": included["t2_close_raw"],
            "次次日跌停价(不复权)": included["t2_down_limit"],
        }
    )


def resolve_buy(row: pd.Series, args: argparse.Namespace) -> dict:
    """逐笔参考实现：判定 T+1 买入，返回买入基准价（不含滑点）与成交状态。

    批量计算请用 calc_trades（向量化，口径与此一致）；本函数保留用于
    单样本核对/调试。

    delta=0：按 T+1 不复权开盘价买入（原规则）；
    delta>0（百分点）：限价单，基准价 = open(T+1) - (delta/100)×close(T)，
    等价于 (1 + a - delta%)×close(T)，a = open(T+1)/close(T)-1（不复权）；
    T+1 不复权最低价 <= 基准价才算成交（按基准价成交），未触及则买入失败。
    """
    t1_open = pd.to_numeric(row["次日开盘价(不复权)"], errors="coerce")
    if pd.isna(t1_open) or t1_open <= 0:
        return {
            "ok": False,
            "status": "无T+1开盘价",
            "base": np.nan,
            "error": "缺少T+1开盘价",
        }
    delta_frac = float(args.delta) / 100.0
    if delta_frac <= 0:
        return {"ok": True, "status": "已买入", "base": float(t1_open), "error": ""}

    t_close = pd.to_numeric(row.get(OPTIONAL_T_CLOSE_COLUMN), errors="coerce")
    if pd.isna(t_close) or t_close <= 0:
        return {
            "ok": False,
            "status": "无T日收盘价",
            "base": np.nan,
            "error": "缺少T日收盘价，无法计算限价买入价",
        }
    base = float(t1_open) - delta_frac * float(t_close)
    if base <= 0:
        return {
            "ok": False,
            "status": "买入价无效",
            "base": np.nan,
            "error": f"限价买入价无效（{base:.3f} <= 0）",
        }
    t1_low = pd.to_numeric(row.get(OPTIONAL_T1_LOW_COLUMN), errors="coerce")
    if pd.isna(t1_low):
        return {
            "ok": False,
            "status": "无T+1最低价",
            "base": np.nan,
            "error": "缺少T+1最低价，无法判定限价成交",
        }
    if float(t1_low) > base + LIMIT_EPS:
        return {
            "ok": False,
            "status": "限价未成交",
            "base": np.nan,
            "error": "T+1日内未触及限价买入价",
        }
    return {"ok": True, "status": "已买入", "base": base, "error": ""}


def _float_column(df: pd.DataFrame, column: str) -> np.ndarray:
    """取数值列（缺失列返回全 NaN），避免逐行 pd.to_numeric 的开销。"""
    if column not in df.columns:
        return np.full(len(df), np.nan, dtype="float64")
    return pd.to_numeric(df[column], errors="coerce").to_numpy(
        dtype="float64", na_value=np.nan
    )


def _bool_column(df: pd.DataFrame, column: str) -> np.ndarray:
    if column not in df.columns:
        return np.zeros(len(df), dtype=bool)
    return df[column].map(to_bool).to_numpy(dtype=bool)


def _calc_trades_t2_legacy(
    samples: pd.DataFrame,
    args: argparse.Namespace,
    exit_tiers: tuple,
    calendar: list[str] | None = None,
    price_cache: dict | None = None,
) -> pd.DataFrame:
    """逐样本计算现金盈亏（向量化实现）。

    买入按 args.delta 判定（delta=0 开盘价；delta>0 限价单，未触及不交易），
    卖出分支与止盈阈值按当前分档参数以买入基准价重新判定。只有「T+2 收盘跌停
    顺延」的少数样本走逐笔循环，其余分支全部按列计算。
    calendar / price_cache 可由调用方注入（如网格搜索批量预取后传入），
    缺省自行加载交易日历并使用空缓存（按需查库）。
    """
    if calendar is None:
        calendar = load_calendar()
    if price_cache is None:
        price_cache = {}
    if samples.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    n = len(samples)
    hit_date = samples["命中日期"].astype(str).to_numpy()
    code = samples["股票代码"].astype(str).to_numpy()
    name = samples["股票名称"].astype(str).to_numpy()
    buy_date = samples["次日日期"].astype(str).to_numpy()
    plan_sell_date = samples["次次日日期"].astype(str).to_numpy()

    t1_open = _float_column(samples, "次日开盘价(不复权)")
    t1_low = _float_column(samples, OPTIONAL_T1_LOW_COLUMN)
    t1_close = _float_column(samples, OPTIONAL_T1_CLOSE_COLUMN)
    t_close = _float_column(samples, OPTIONAL_T_CLOSE_COLUMN)
    t2_open = _float_column(samples, "次次日开盘价(不复权)")
    t2_high = _float_column(samples, "次次日最高价(不复权)")
    t2_close = _float_column(samples, "次次日收盘价(不复权)")
    t2_down = _float_column(samples, "次次日跌停价(不复权)")
    close_limit_hint = _bool_column(samples, "卖出日是否跌停")

    # ---------------- 买入 ----------------
    delta_frac = float(args.delta) / 100.0
    buy_base = np.full(n, np.nan, dtype="float64")
    m_open = np.isfinite(t1_open) & (t1_open > 0)
    buy_ok = m_open.copy()
    buy_status = np.full(n, "无T+1开盘价", dtype=object)
    buy_error = np.full(n, "缺少T+1开盘价", dtype=object)

    if delta_frac <= 0:
        buy_base[buy_ok] = t1_open[buy_ok]
    else:
        has_t_close = np.isfinite(t_close) & (t_close > 0)
        candidate = t1_open - delta_frac * t_close
        m_noclose = m_open & ~has_t_close
        m_invalid = m_open & has_t_close & ~(candidate > 0)
        m_hasbase = m_open & has_t_close & (candidate > 0)
        m_nolow = m_hasbase & ~np.isfinite(t1_low)
        m_unfilled = m_hasbase & np.isfinite(t1_low) & (t1_low > candidate + LIMIT_EPS)
        buy_ok = m_hasbase & np.isfinite(t1_low) & ~m_unfilled
        buy_base[buy_ok] = candidate[buy_ok]
        buy_status[m_noclose] = "无T日收盘价"
        buy_error[m_noclose] = "缺少T日收盘价，无法计算限价买入价"
        buy_status[m_invalid] = "买入价无效"
        for i in np.flatnonzero(m_invalid):
            buy_error[i] = f"限价买入价无效（{candidate[i]:.3f} <= 0）"
        buy_status[m_nolow] = "无T+1最低价"
        buy_error[m_nolow] = "缺少T+1最低价，无法判定限价成交"
        buy_status[m_unfilled] = "限价未成交"
        buy_error[m_unfilled] = "T+1日内未触及限价买入价"

    buy_price = np.where(buy_ok, buy_base * (1.0 + args.slippage), np.nan)

    # ---------------- 卖出 ----------------
    sell_status = buy_status.copy()
    sell_status[buy_ok] = "已卖出"
    sell_error = buy_error.copy()
    sell_error[buy_ok] = ""
    sell_date = np.full(n, None, dtype=object)
    sell_price = np.full(n, np.nan, dtype="float64")
    delay_days = np.zeros(n, dtype="int64")
    exit_mode = np.full(n, "", dtype=object)
    day1_ret = np.full(n, np.nan, dtype="float64")
    exit_threshold = np.full(n, np.nan, dtype="float64")
    exit_tier = np.full(n, None, dtype=object)
    labels = tier_labels(exit_tiers)

    if len(exit_tiers) == 1:
        # 单段分档（固定阈值）不需要 T+1 收盘价
        threshold = np.full(n, float(exit_tiers[0][2]), dtype="float64")
        proceed = buy_ok.copy()
        exit_threshold[buy_ok] = threshold[buy_ok]
        exit_tier[buy_ok] = labels[0]
    else:
        # T+1 收盘价缺失时回查数据库（极少见），再判断无法分档
        need_close = buy_ok & ~(np.isfinite(t1_close) & (t1_close > 0))
        if need_close.any():
            for i in np.flatnonzero(need_close):
                px = load_price_row(code[i], buy_date[i], price_cache)
                if px is None:
                    continue
                close = px.get("close")
                if close is not None and pd.notna(close) and float(close) > 0:
                    t1_close[i] = float(close)

        has_close = np.isfinite(t1_close) & (t1_close > 0)
        m_noclose = buy_ok & ~has_close
        sell_status[m_noclose] = "无T+1收盘价"
        sell_error[m_noclose] = "缺少T+1收盘价，无法确定止盈档位"

        ret = np.full(n, np.nan, dtype="float64")
        valid_ret = buy_ok & has_close
        ret[valid_ret] = t1_close[valid_ret] / buy_base[valid_ret] - 1.0
        valid_ret &= np.isfinite(ret)

        threshold = np.full(n, np.nan, dtype="float64")
        for label, (lo, hi, rate) in zip(labels, exit_tiers):
            mask = valid_ret.copy()
            if lo is not None:
                mask &= ret > lo
            if hi is not None:
                mask &= ret <= hi
            threshold[mask] = rate
            exit_tier[mask] = label
        proceed = valid_ret & np.isfinite(threshold)
        m_notier = valid_ret & ~proceed
        sell_status[m_notier] = "无法分档"
        sell_error[m_notier] = "T+1收盘对买入价涨幅无效，无法落入任何止盈档位"
        day1_ret[proceed] = ret[proceed]
        exit_threshold[proceed] = threshold[proceed]

    target = buy_base * (1.0 + exit_threshold)

    # 1) T+2 开盘已高于目标价
    m_open_exit = (
        proceed
        & np.isfinite(t2_open)
        & (t2_open > 0)
        & np.isfinite(target)
        & (t2_open > target)
    )
    sell_date[m_open_exit] = plan_sell_date[m_open_exit]
    sell_price[m_open_exit] = t2_open[m_open_exit]
    exit_mode[m_open_exit] = "T+2 开盘"

    # 2) 缺少最高价就无法判断盘中是否触及目标价
    rest = proceed & ~m_open_exit
    m_nohigh = rest & ~(np.isfinite(t2_high) & (t2_high > 0))
    sell_status[m_nohigh] = "无T+2最高价"
    sell_error[m_nohigh] = "缺少T+2最高价，无法判断盘中止盈"

    # 3) 盘中触及目标价：按目标价卖出（受最高价约束）
    rest &= ~m_nohigh
    m_intraday = rest & np.isfinite(target) & (t2_high >= target)
    sell_date[m_intraday] = plan_sell_date[m_intraday]
    sell_price[m_intraday] = np.minimum(target[m_intraday], t2_high[m_intraday])
    exit_mode[m_intraday] = "T+2 盘中"

    # 4) T+2 收盘卖出；收盘跌停顺延到下一交易日开盘
    rest &= ~m_intraday
    m_noclose2 = rest & ~(np.isfinite(t2_close) & (t2_close > 0))
    sell_status[m_noclose2] = "无T+2收盘价"
    sell_error[m_noclose2] = "缺少T+2收盘价"

    rest &= ~m_noclose2
    limit_down = (
        np.isfinite(t2_close)
        & (t2_close > 0)
        & np.isfinite(t2_down)
        & (t2_down > 0)
        & (t2_close <= t2_down + LIMIT_EPS)
    )
    m_close_exit = rest & ~close_limit_hint & ~limit_down
    sell_date[m_close_exit] = plan_sell_date[m_close_exit]
    sell_price[m_close_exit] = t2_close[m_close_exit]
    exit_mode[m_close_exit] = "T+2 收盘"

    m_delay = rest & ~m_close_exit
    if m_delay.any():
        date_pos = {d: i for i, d in enumerate(calendar)}
        max_delay = int(args.max_delay_days)
        for i in np.flatnonzero(m_delay):
            pos = date_pos.get(plan_sell_date[i])
            dates = calendar[pos + 1 : pos + 1 + max_delay] if pos is not None else []
            if not dates:
                sell_status[i] = "无法顺延"
                sell_error[i] = "交易日历无后续交易日"
                continue
            last_row = None
            for days, date in enumerate(dates, start=1):
                px = load_price_row(code[i], date, price_cache)
                if px is None:
                    continue
                last_row = px
                open_price = px.get("open")
                down_limit = px.get("down_limit")
                if (
                    pd.notna(open_price)
                    and open_price > 0
                    and not is_limit_down(
                        float(open_price),
                        np.nan if down_limit is None else float(down_limit),
                    )
                ):
                    sell_date[i] = date
                    sell_price[i] = float(open_price)
                    delay_days[i] = days
                    exit_mode[i] = f"顺延{days}日开盘"
                    break
            else:
                if last_row is not None and pd.notna(last_row.get("close")):
                    sell_status[i] = "超期未卖出(按最后收盘估值)"
                    sell_date[i] = str(last_row["trade_date"])
                    sell_price[i] = float(last_row["close"])
                    delay_days[i] = len(dates)
                    exit_mode[i] = f"顺延{len(dates)}日后估值"
                    sell_error[i] = "连续跌停无法卖出，按最后收盘估值"
                else:
                    sell_status[i] = "无后续行情"
                    sell_error[i] = "顺延期间无行情"

    # ---------------- 现金 ----------------
    sell_price_net = np.where(
        np.isfinite(sell_price) & (sell_price > 0),
        sell_price * (1.0 - args.slippage),
        np.nan,
    )
    valid_buy_price = buy_ok & np.isfinite(buy_price) & (buy_price > 0)
    lot_ratio = np.full(n, np.nan, dtype="float64")
    lot_ratio[valid_buy_price] = args.position / (buy_price[valid_buy_price] * 100.0)
    lots_raw = (
        np.round(lot_ratio) if args.lot_mode == "nearest" else np.floor(lot_ratio)
    )
    lots = np.where(np.isfinite(lots_raw) & (lots_raw > 0), lots_raw, 0).astype("int64")
    shares = lots * 100
    has_position = lots > 0
    sell_ok = np.isfinite(sell_price_net) & (sell_price_net > 0)
    buy_amount = np.where(has_position, shares * buy_price, 0.0)
    buy_fee = np.where(
        has_position,
        np.maximum(buy_amount * args.commission, args.min_commission)
        + buy_amount * args.transfer,
        0.0,
    )
    filled_sell = has_position & sell_ok
    sell_amount = np.where(filled_sell, shares * sell_price_net, 0.0)
    sell_fee = np.where(
        filled_sell,
        np.maximum(sell_amount * args.commission, args.min_commission)
        + sell_amount * args.stamp
        + sell_amount * args.transfer,
        0.0,
    )
    net_pnl = np.where(
        filled_sell, sell_amount - sell_fee - buy_amount - buy_fee, np.nan
    )
    net_return = np.where(filled_sell, net_pnl / (buy_amount + buy_fee), np.nan)
    gross_return = np.where(filled_sell, sell_price_net / buy_price - 1.0, np.nan)
    error = np.where(valid_buy_price & ~has_position, "资金不足1手", sell_error)

    return pd.DataFrame(
        {
            "hit_date": hit_date,
            "ts_code": code,
            "name": name,
            "buy_date": buy_date,
            "plan_sell_date": plan_sell_date,
            "sell_date": sell_date,
            "t1_intraday_ret": day1_ret,
            "exit_threshold": exit_threshold,
            "exit_tier": exit_tier,
            "exit_mode": exit_mode,
            "delay_days": delay_days,
            "lot_mode": args.lot_mode,
            "lots": lots,
            "shares": shares,
            "buy_base_raw": buy_base,
            "buy_price_raw": buy_price,
            "sell_price_raw": sell_price_net,
            "buy_amount": buy_amount,
            "buy_fee": buy_fee,
            "sell_amount": sell_amount,
            "sell_fee": sell_fee,
            "net_pnl": net_pnl,
            "gross_return": gross_return,
            "net_return": net_return,
            "exit_status": sell_status,
            "is_delayed": delay_days > 0,
            "close_limit_down": close_limit_hint,
            "error": error,
        }
    )


def calc_trades(
    samples: pd.DataFrame,
    args: argparse.Namespace,
    exit_tiers: tuple | None = None,
    calendar: list[str] | None = None,
    price_cache: dict | None = None,
) -> pd.DataFrame:
    """逐样本计算现金盈亏。

    买入：按 args.delta 在 T+1 开盘价/限价买入（与 build_samples 口径一致）。
    卖出：直接使用 build_samples 扫描好的 T+2..T+N 卖出计划；只有
    “T+N 收盘卖出且收盘跌停”的样本才走顺延循环。
    calendar / price_cache 可由调用方注入；缺省自行加载交易日历并使用空缓存。

    exit_tiers 非 None 时走旧 T+2 兼容路径，供 cash_grid_search /
    capital_cycle 等旧调用方使用；主脚本不传该参数，走 T+2..T+N 扫描结果。
    """
    if exit_tiers is not None:
        out = _calc_trades_t2_legacy(
            samples, args, exit_tiers, calendar=calendar, price_cache=price_cache
        )
        out["exit_offset"] = 2
        return out[TRADE_COLUMNS]

    if calendar is None:
        calendar = load_calendar()
    if price_cache is None:
        price_cache = {}
    if samples.empty:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    n = len(samples)
    hit_date = samples["命中日期"].astype(str).to_numpy()
    code = samples["股票代码"].astype(str).to_numpy()
    name = samples["股票名称"].astype(str).to_numpy()
    buy_date = samples["次日日期"].astype(str).to_numpy()
    plan_sell_date = samples["计划卖出日"].astype(str).to_numpy()
    exit_date_raw = samples["实际卖出日"].to_numpy(dtype=object)
    exit_date = np.array(
        [None if pd.isna(value) else str(value) for value in exit_date_raw],
        dtype=object,
    )
    exit_mode_plan = samples["卖出方式"].fillna("").astype(str).to_numpy()
    exit_price_plan = _float_column(samples, "卖出价(不复权)")
    exit_offset = _float_column(samples, "卖出偏移")
    exit_status_plan = samples["卖出状态"].fillna("").astype(str).to_numpy()
    exit_error_plan = samples["卖出备注"].fillna("").astype(str).to_numpy()
    close_limit_hint = _bool_column(samples, "卖出日是否跌停")

    t1_open = _float_column(samples, "次日开盘价(不复权)")
    t1_low = _float_column(samples, "次日最低价(不复权)")
    t1_close = _float_column(samples, "次日收盘价(不复权)")
    t_close = _float_column(samples, "命中日收盘价(不复权)")

    # ---------------- 买入 ----------------
    delta_frac = float(args.delta) / 100.0
    buy_base = np.full(n, np.nan, dtype="float64")
    m_open = np.isfinite(t1_open) & (t1_open > 0)
    buy_ok = m_open.copy()
    buy_status = np.full(n, "无T+1开盘价", dtype=object)
    buy_error = np.full(n, "缺少T+1开盘价", dtype=object)

    if delta_frac <= 0:
        buy_base[buy_ok] = t1_open[buy_ok]
    else:
        has_t_close = np.isfinite(t_close) & (t_close > 0)
        candidate = t1_open - delta_frac * t_close
        m_noclose = m_open & ~has_t_close
        m_invalid = m_open & has_t_close & ~(candidate > 0)
        m_hasbase = m_open & has_t_close & (candidate > 0)
        m_nolow = m_hasbase & ~np.isfinite(t1_low)
        m_unfilled = m_hasbase & np.isfinite(t1_low) & (t1_low > candidate + LIMIT_EPS)
        buy_ok = m_hasbase & np.isfinite(t1_low) & ~m_unfilled
        buy_base[buy_ok] = candidate[buy_ok]
        buy_status[m_noclose] = "无T日收盘价"
        buy_error[m_noclose] = "缺少T日收盘价，无法计算限价买入价"
        buy_status[m_invalid] = "买入价无效"
        for i in np.flatnonzero(m_invalid):
            buy_error[i] = f"限价买入价无效（{candidate[i]:.3f} <= 0）"
        buy_status[m_nolow] = "无T+1最低价"
        buy_error[m_nolow] = "缺少T+1最低价，无法判定限价成交"
        buy_status[m_unfilled] = "限价未成交"
        buy_error[m_unfilled] = "T+1日内未触及限价买入价"

    buy_price = np.where(buy_ok, buy_base * (1.0 + args.slippage), np.nan)

    # ---------------- 卖出（使用 build_samples 的扫描结果） ----------------
    sell_status = buy_status.copy()
    sell_error = buy_error.copy()

    plan_ok = (
        np.isfinite(exit_price_plan)
        & (exit_price_plan > 0)
        & np.array([date is not None for date in exit_date], dtype=bool)
    )
    valid_plan = buy_ok & plan_ok
    sell_status[valid_plan] = "已卖出"
    sell_error[valid_plan] = ""

    # build_samples 已经给出明确的失败原因（无法分档/无行情）
    plan_failed = buy_ok & ~plan_ok
    sell_status[plan_failed] = exit_status_plan[plan_failed]
    sell_error[plan_failed] = exit_error_plan[plan_failed]

    sell_date = np.where(
        valid_plan,
        exit_date,
        None,
    ).astype(object)
    sell_price = np.where(valid_plan, exit_price_plan, np.nan)
    exit_mode = np.where(valid_plan, exit_mode_plan, "")
    delay_days = np.zeros(n, dtype="int64")

    # 只有 T+N 收盘卖出且收盘跌停才顺延
    final_mode = exit_mode_plan == f"T+{args.sell_n} 收盘"
    m_delay = valid_plan & final_mode & close_limit_hint
    if m_delay.any():
        date_pos = {d: i for i, d in enumerate(calendar)}
        max_delay = int(args.max_delay_days)
        for i in np.flatnonzero(m_delay):
            pos = date_pos.get(plan_sell_date[i])
            dates = calendar[pos + 1 : pos + 1 + max_delay] if pos is not None else []
            if not dates:
                sell_status[i] = "无法顺延"
                sell_error[i] = "交易日历无后续交易日"
                sell_date[i] = None
                sell_price[i] = np.nan
                continue
            last_row = None
            for days, date in enumerate(dates, start=1):
                px = load_price_row(code[i], date, price_cache)
                if px is None:
                    continue
                last_row = px
                open_price = px.get("open")
                down_limit = px.get("down_limit")
                if (
                    pd.notna(open_price)
                    and open_price > 0
                    and not is_limit_down(
                        float(open_price),
                        np.nan if down_limit is None else float(down_limit),
                    )
                ):
                    sell_date[i] = date
                    sell_price[i] = float(open_price)
                    delay_days[i] = days
                    exit_mode[i] = f"顺延{days}日开盘"
                    sell_status[i] = "已卖出"
                    sell_error[i] = ""
                    break
            else:
                if last_row is not None and pd.notna(last_row.get("close")):
                    sell_status[i] = "超期未卖出(按最后收盘估值)"
                    sell_date[i] = str(last_row["trade_date"])
                    sell_price[i] = float(last_row["close"])
                    delay_days[i] = len(dates)
                    exit_mode[i] = f"顺延{len(dates)}日后估值"
                    sell_error[i] = "连续跌停无法卖出，按最后收盘估值"
                else:
                    sell_status[i] = "无后续行情"
                    sell_error[i] = "顺延期间无行情"
                    sell_date[i] = None
                    sell_price[i] = np.nan

    # ---------------- 现金 ----------------
    sell_price_net = np.where(
        np.isfinite(sell_price) & (sell_price > 0),
        sell_price * (1.0 - args.slippage),
        np.nan,
    )
    valid_buy_price = buy_ok & np.isfinite(buy_price) & (buy_price > 0)
    lot_ratio = np.full(n, np.nan, dtype="float64")
    lot_ratio[valid_buy_price] = args.position / (buy_price[valid_buy_price] * 100.0)
    lots_raw = (
        np.round(lot_ratio) if args.lot_mode == "nearest" else np.floor(lot_ratio)
    )
    lots = np.where(np.isfinite(lots_raw) & (lots_raw > 0), lots_raw, 0).astype("int64")
    shares = lots * 100
    has_position = lots > 0
    sell_ok = np.isfinite(sell_price_net) & (sell_price_net > 0)
    buy_amount = np.where(has_position, shares * buy_price, 0.0)
    buy_fee = np.where(
        has_position,
        np.maximum(buy_amount * args.commission, args.min_commission)
        + buy_amount * args.transfer,
        0.0,
    )
    filled_sell = has_position & sell_ok
    sell_amount = np.where(filled_sell, shares * sell_price_net, 0.0)
    sell_fee = np.where(
        filled_sell,
        np.maximum(sell_amount * args.commission, args.min_commission)
        + sell_amount * args.stamp
        + sell_amount * args.transfer,
        0.0,
    )
    net_pnl = np.where(
        filled_sell, sell_amount - sell_fee - buy_amount - buy_fee, np.nan
    )
    net_return = np.where(filled_sell, net_pnl / (buy_amount + buy_fee), np.nan)
    gross_return = np.where(filled_sell, sell_price_net / buy_price - 1.0, np.nan)
    error = np.where(valid_buy_price & ~has_position, "资金不足1手", sell_error)

    t1_intraday_ret = _float_column(samples, "T+1收盘对买入价涨幅")
    exit_threshold = _float_column(samples, "动态止盈阈值")
    exit_tier = samples["止盈区间"].astype(str).to_numpy()

    return pd.DataFrame(
        {
            "hit_date": hit_date,
            "ts_code": code,
            "name": name,
            "buy_date": buy_date,
            "plan_sell_date": plan_sell_date,
            "sell_date": sell_date,
            "exit_offset": exit_offset,
            "t1_intraday_ret": t1_intraday_ret,
            "exit_threshold": exit_threshold,
            "exit_tier": exit_tier,
            "exit_mode": exit_mode,
            "delay_days": delay_days,
            "lot_mode": args.lot_mode,
            "lots": lots,
            "shares": shares,
            "buy_base_raw": buy_base,
            "buy_price_raw": buy_price,
            "sell_price_raw": sell_price_net,
            "buy_amount": buy_amount,
            "buy_fee": buy_fee,
            "sell_amount": sell_amount,
            "sell_fee": sell_fee,
            "net_pnl": net_pnl,
            "gross_return": gross_return,
            "net_return": net_return,
            "exit_status": sell_status,
            "is_delayed": delay_days > 0,
            "close_limit_down": close_limit_hint,
            "error": error,
        }
    )


def stats(values: pd.Series) -> dict:
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return {"样本数": 0}
    wins = v[v > 0]
    losses = v[v < 0]
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    return {
        "样本数": len(v),
        "合计": float(v.sum()),
        "平均": float(v.mean()),
        "中位数": float(v.median()),
        "标准差": float(v.std(ddof=1)) if len(v) > 1 else None,
        "胜率": float((v > 0).mean()),
        "平均盈利": avg_win,
        "平均亏损": avg_loss,
        "盈亏比": (
            avg_win / abs(avg_loss)
            if avg_win is not None and avg_loss not in (None, 0)
            else None
        ),
        "最小": float(v.min()),
        "最大": float(v.max()),
    }


# ==============================================================================
# 报告
# ==============================================================================


def fmt_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.2f}"


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def build_export_context(included: pd.DataFrame) -> pd.DataFrame:
    """cash_trades.csv 的分析字段，按 (命中日期, 股票代码) 附着。"""
    industry = (
        included["industry"].fillna("").astype(str)
        if "industry" in included.columns
        else pd.Series("", index=included.index, dtype="object")
    )
    context = pd.DataFrame(
        {
            "hit_date": included["hit_date"].astype(str),
            "ts_code": included["ts_code"].astype(str),
            "industry": industry.to_numpy(),
            "t_circ_mv_yi": (included["t_circ_mv"] / 10000.0).round(2).to_numpy(),
            "amount_ratio_t_prev": included["amount_ratio_t_prev"].round(4).to_numpy(),
            # T+1 开盘相对 T 日收盘的原始价高开比例：
            # (open_raw(T+1) - close_raw(T)) / close_raw(T)
            "entry_gap_close_raw": included["entry_gap_close_raw"].to_numpy(),
            "amount_ratio_t1_t": included["amount_ratio_t1_t"].round(4).to_numpy(),
            "t1_kline": included["t1_kline"].fillna("").astype(str).to_numpy(),
            "t1_intraday_adj": included["t1_intraday_adj"].to_numpy(),
            "t2_close_vs_t1_open_adj": included["t2_close_vs_t1_open_adj"].to_numpy(),
            "t2_high_vs_t1_open_adj": included["t2_high_vs_t1_open_adj"].to_numpy(),
            "t1_low_drawdown_adj": included["t1_low_drawdown_adj"].to_numpy(),
            "t1_high_upside_adj": included["t1_high_upside_adj"].to_numpy(),
            "t1_close_change_adj": included["t1_close_change_adj"].to_numpy(),
            "t_open_vs_prev_close_adj": included["t_open_vs_prev_close_adj"].to_numpy(),
            "t_close_vs_prev_close_adj": included[
                "t_close_vs_prev_close_adj"
            ].to_numpy(),
            "t_low_vs_prev_close_adj": included["t_low_vs_prev_close_adj"].to_numpy(),
        }
    )
    return context.drop_duplicates(["hit_date", "ts_code"])


def build_cash_trades_export(
    trades: pd.DataFrame, context: pd.DataFrame | None = None
) -> pd.DataFrame:
    """cash_trades.csv 的导出视图。

    - 不导出内部执行字段：手数取整方式 / 手数 / 股数 / 卖出状态；
    - 收益率列按百分数显示；
    - 按 CASH_TRADES_COLUMN_ORDER 重排，便于阅读。
    """
    out = trades.drop(columns=list(CASH_TRADES_EXCLUDED_COLUMNS)).copy()
    if context is not None:
        out = out.merge(context, on=["hit_date", "ts_code"], how="left")
    for column in ("industry", "t1_kline"):
        if column in out.columns:
            out[column] = out[column].fillna("").astype(str)
    for column in CASH_TRADES_PCT_COLUMNS:
        if column in out.columns:
            out[column] = out[column].map(lambda value: fmt_pct(value, signed=False))
    if "exit_offset" in out.columns:
        out["exit_offset"] = out["exit_offset"].map(
            lambda value: "" if pd.isna(value) else int(value)
        )
    ordered = [c for c in CASH_TRADES_COLUMN_ORDER if c in out.columns]
    ordered += [c for c in out.columns if c not in ordered]
    return out[ordered].rename(columns=CASH_TRADES_CN)


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
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)


def build_cash_report(
    trades: pd.DataFrame,
    args: argparse.Namespace,
    exit_tiers: tuple,
    generated_at: str,
    start: str,
    end: str,
    title: str = "# limit_up_shakeout 二次筛选样本现金收益",
    hit_table: str = "hit_limit_up_shakeout",
) -> str:
    realized = trades[trades["exit_status"] == "已卖出"].copy()
    delayed = trades[trades["delay_days"] > 0].copy()
    buy_failed = trades[trades["exit_status"].isin(BUY_FAIL_STATUSES)].copy()
    not_sold = trades[
        (trades["exit_status"] != "已卖出")
        & ~trades["exit_status"].isin(BUY_FAIL_STATUSES)
    ].copy()
    mark_to_market = not_sold[
        not_sold["exit_status"].astype(str).str.startswith("超期未卖出")
    ].copy()
    unsold = not_sold[
        ~not_sold["exit_status"].astype(str).str.startswith("超期未卖出")
    ].copy()
    stats_pnl = stats(realized["net_pnl"])
    stats_ret = stats(realized["net_return"])

    exit_rows = []
    # 错误行的卖出方式为空字符串，不参与“卖出方式分布”
    sold_or_pending = trades[trades["exit_mode"].astype(str).ne("")]
    for mode, group in sold_or_pending.groupby("exit_mode", sort=True):
        exit_rows.append(
            {
                "卖出方式": str(mode),
                "样本数": len(group),
                "占比": fmt_pct(len(group) / len(trades), signed=False),
                "净盈亏合计": fmt_money(group["net_pnl"].sum()),
                "净收益率均值": fmt_pct(group["net_return"].mean()),
            }
        )

    tier_rate_map = dict(zip(tier_labels(exit_tiers), (t[2] for t in exit_tiers)))
    tier_rows = []
    for label in tier_labels(exit_tiers):
        group = trades[trades["exit_tier"] == label]
        if group.empty:
            continue
        tier_rows.append(
            {
                "止盈区间": label,
                "止盈阈值": fmt_pct(tier_rate_map[label]),
                "样本数": len(group),
                "占比": fmt_pct(len(group) / len(trades), signed=False),
                "净盈亏合计": fmt_money(group["net_pnl"].sum()),
                "净收益率均值": fmt_pct(group["net_return"].mean()),
                "胜率": fmt_pct((group["net_pnl"] > 0).mean(), signed=False),
            }
        )

    yearly_rows = []
    for year in sorted(realized["hit_date"].str[:4].unique()):
        group = realized[realized["hit_date"].str[:4] == year]
        yearly_rows.append(
            {
                "年份": year,
                "样本数": len(group),
                "净盈亏合计": fmt_money(group["net_pnl"].sum()),
                "平均净盈亏": fmt_money(group["net_pnl"].mean()),
                "平均净收益率": fmt_pct(group["net_return"].mean()),
                "胜率": fmt_pct((group["net_pnl"] > 0).mean(), signed=False),
            }
        )

    def trade_rows(df: pd.DataFrame) -> list[dict]:
        out = []
        for row in df.itertuples(index=False):
            out.append(
                {
                    "命中日": row.hit_date,
                    "代码": row.ts_code,
                    "名称": row.name,
                    "买入日": row.buy_date,
                    "实际卖出日": row.sell_date,
                    "卖出偏移": (
                        int(row.exit_offset) if pd.notna(row.exit_offset) else "-"
                    ),
                    "手数": int(row.lots),
                    "买入价": f"{row.buy_price_raw:.3f}",
                    "卖出价": f"{row.sell_price_raw:.3f}",
                    "净盈亏": fmt_money(row.net_pnl),
                    "净收益率": fmt_pct(row.net_return),
                    "卖出方式": row.exit_mode,
                }
            )
        return out

    summary_rows = [
        {"指标": "样本数", "数值": str(len(trades))},
        {"指标": "最长持有交易日 N", "数值": str(args.sell_n)},
        {"指标": "实际买入样本数", "数值": str(int((trades["shares"] > 0).sum()))},
        {
            "指标": "买入失败样本数（限价未成交/缺价格）",
            "数值": str(len(buy_failed)),
        },
        {"指标": "已卖出样本数", "数值": str(len(realized))},
        {"指标": "延期卖出样本数", "数值": str(len(delayed))},
        {"指标": "超期未卖出(按最后收盘估值)", "数值": str(len(mark_to_market))},
        {"指标": "买入后无法卖出/无行情样本数", "数值": str(len(unsold))},
        {"指标": "买入金额合计", "数值": fmt_money(trades["buy_amount"].sum())},
        {"指标": "买入费用合计", "数值": fmt_money(trades["buy_fee"].sum())},
        {"指标": "卖出金额合计", "数值": fmt_money(realized["sell_amount"].sum())},
        {"指标": "卖出费用合计", "数值": fmt_money(realized["sell_fee"].sum())},
        {"指标": "已实现净盈亏合计", "数值": fmt_money(realized["net_pnl"].sum())},
        {"指标": "平均每笔净盈亏", "数值": fmt_money(stats_pnl.get("平均"))},
        {"指标": "每笔净盈亏中位数", "数值": fmt_money(stats_pnl.get("中位数"))},
        {"指标": "净收益率均值", "数值": fmt_pct(stats_ret.get("平均"))},
        {"指标": "净收益率中位数", "数值": fmt_pct(stats_ret.get("中位数"))},
        {"指标": "胜率", "数值": fmt_pct(stats_pnl.get("胜率"), signed=False)},
        {"指标": "盈亏比", "数值": fmt_money(stats_pnl.get("盈亏比"))},
        {
            "指标": "已实现收益/预算合计",
            "数值": fmt_pct(
                realized["net_pnl"].sum() / (len(realized) * args.position)
                if len(realized)
                else None
            ),
        },
    ]

    top = realized.nlargest(10, "net_pnl")
    bottom = realized.nsmallest(10, "net_pnl")
    delayed_view = delayed.sort_values(["hit_date", "ts_code"])

    lines = [
        title,
        "",
        f"- 生成时间：{generated_at}",
        f"- 命中表：`{hit_table}`",
        f"- 统计区间：`{start}` ~ `{end}`",
        f"- T 日高开区间：`{fmt_range(args.gap)}`",
        f"- T 日收盘涨跌幅区间：`{fmt_range(args.close_chg)}`",
        f"- 涨幅条件：{fmt_n_days_rise(args.n_days_rise)}",
        f"- T+1 买入低开区间（原始价，相对 T 日收盘）：`{fmt_entry_gap_ranges(args.entry_gap)}`",
        f"- 买入 delta：`{args.delta:g}%`（0 = 开盘价买入）",
        f"- 持有窗口：`T+2 ~ T+{args.sell_n}`（T+1 买入；盘中触及目标价即卖出，"
        f"一直未触发则 T+{args.sell_n} 收盘卖出）",
        f"- 信号日最低价条件：{'要求' if args.require_low_support else '不要求'}"
        "（后复权 `low(T) >= close(T-1)`）",
        f"- 样本数：{len(trades)}",
        f"- 每笔预算：`{args.position:,.0f}` 元",
        f"- 手数模式：`{args.lot_mode}`（100 股/手）",
        "- 买入执行："
        + (
            f"T+1 限价单，基准价 = `(1 + a - {args.delta:g}%) × close(T)`"
            f"（`a = open(T+1)/close(T)-1`，即开盘下方 {args.delta:g}%）；"
            "`low(T+1) <= 基准价` 才成交（按基准价，另计滑点），未触及则该样本不交易"
            if args.delta > 0
            else "T+1 开盘价（delta=0，原规则）"
        ),
        f"- 动态止盈分档：{format_tiers_summary(exit_tiers)}",
        "- 止盈/止损基准：目标价 = 买入基准价 × (1 + 阈值)；分档涨幅 = "
        "`close(T+1)/买入基准价 - 1`（delta=0 时即原日内涨幅 `close/open`）。",
        "- 卖出执行：T+2 起逐日检查；开盘价高于目标价按开盘价卖出，"
        "盘中最高价触及目标价按目标价卖出；"
        f"一直未触发则 T+{args.sell_n} 收盘卖出（收盘跌停顺延）。",
        f"- 佣金：`{args.commission:.5%}`，最低 `{args.min_commission:.0f}` 元；"
        f"印花税：`{args.stamp:.3%}`（卖出）；过户费：`{args.transfer:.4%}`（双边）；"
        f"滑点：`{args.slippage:.2%}`（单边）",
        f"- 尾盘跌停：`T+{args.sell_n} 收盘卖出` 且收盘跌停时，顺延到下一交易日开盘，"
        f"最多 `{args.max_delay_days}` 个交易日。",
        "",
        "## 汇总",
        "",
        md_table(summary_rows, ["指标", "数值"]),
        "",
        "> 已实现净盈亏合计只统计“已卖出”样本；延期卖出单独列示。",
        "",
        "## 卖出方式分布",
        "",
        md_table(
            exit_rows, ["卖出方式", "样本数", "占比", "净盈亏合计", "净收益率均值"]
        ),
        "",
        "## 止盈区间分布（按 T+1 收盘对买入价涨幅分档）",
        "",
        md_table(
            tier_rows,
            [
                "止盈区间",
                "止盈阈值",
                "样本数",
                "占比",
                "净盈亏合计",
                "净收益率均值",
                "胜率",
            ],
        ),
        "",
        "## 延期卖出明细",
        "",
    ]

    if delayed_view.empty:
        lines.append("_无_")
    else:
        delayed_rows = []
        for row in delayed_view.itertuples(index=False):
            delayed_rows.append(
                {
                    "命中日": row.hit_date,
                    "代码": row.ts_code,
                    "名称": row.name,
                    "计划卖出日": row.plan_sell_date,
                    "实际卖出日": row.sell_date,
                    "延期天数": int(row.delay_days),
                    "卖出方式": row.exit_mode,
                    "卖出价": f"{row.sell_price_raw:.3f}",
                    "净盈亏": fmt_money(row.net_pnl),
                    "净收益率": fmt_pct(row.net_return),
                }
            )
        lines.append(
            md_table(
                delayed_rows,
                [
                    "命中日",
                    "代码",
                    "名称",
                    "计划卖出日",
                    "实际卖出日",
                    "延期天数",
                    "卖出方式",
                    "卖出价",
                    "净盈亏",
                    "净收益率",
                ],
            )
        )

    lines.extend(
        [
            "",
            "## 分年统计（已卖出样本）",
            "",
            md_table(
                yearly_rows,
                ["年份", "样本数", "净盈亏合计", "平均净盈亏", "平均净收益率", "胜率"],
            ),
            "",
            "## 净盈亏最高 10 笔",
            "",
            md_table(
                trade_rows(top),
                [
                    "命中日",
                    "代码",
                    "名称",
                    "买入日",
                    "实际卖出日",
                    "卖出偏移",
                    "手数",
                    "买入价",
                    "卖出价",
                    "净盈亏",
                    "净收益率",
                    "卖出方式",
                ],
            ),
            "",
            "## 净盈亏最低 10 笔",
            "",
            md_table(
                trade_rows(bottom),
                [
                    "命中日",
                    "代码",
                    "名称",
                    "买入日",
                    "实际卖出日",
                    "卖出偏移",
                    "手数",
                    "买入价",
                    "卖出价",
                    "净盈亏",
                    "净收益率",
                    "卖出方式",
                ],
            ),
            "",
            "## 口径说明",
            "",
            "- 买入基准价：delta=0 时为 T+1 不复权开盘价；delta>0 时为限价 "
            "(1+a-delta%)×close(T)（a=open(T+1)/close(T)-1，不复权），T+1 最低价触及基准价"
            "才成交（按基准价成交，未考虑限价单排队与部分成交），未触及则该样本不交易。"
            "实际买入价 = 基准价×(1+滑点)；卖出价用实际卖出日不复权价格。",
            "- 止盈目标价 = 买入基准价 × (1 + 动态止盈阈值)；阈值按 T+1 收盘对买入价涨幅 "
            "close(T+1)/买入基准价-1 分档确定（delta=0 时即原日内涨幅口径），负阈值为止损档。",
            "- 卖出日按 T+2..T+N 顺序扫描：开盘价高于目标价按开盘价成交；否则盘中最高价"
            "触及目标价按目标价成交；一直未触发则 T+N 收盘卖出。",
            "- 盘中触及目标价时假设能按目标价精确成交，受当日最高价约束；未考虑滑点前的盘中路径。",
            "- T+1 收盘价缺失（且数据库也查不到）时无法分档，该样本按错误处理、不交易；"
            "T 日收盘价/T+1 最低价缺失时无法判定限价买入，同样不交易。",
            "- 佣金、印花税、过户费按成交金额计算；佣金有最低 5 元限制。",
            "- 滑点默认 0；可通过 `--slippage 0.001` 加入 0.1% 单边滑点。",
            "- 同一交易日多个样本可能重叠，合计净盈亏不是组合净值，也不代表可同时投入的资金。",
            "- “超期未卖出”样本按最后可用收盘价做市值估值，会在明细中标注，不应视为已实现收益。",
            "",
            "## 输出文件",
            "",
            "- `report.md`：本报告",
            "- `cash_trades.csv`：逐笔现金盈亏明细",
            "- `cash_summary.csv`：汇总指标",
            "- `cash_yearly_stats.csv`：分年统计",
            "- `run_meta.json`：运行参数",
            "",
        ]
    )
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    strategy: str = "limit_up_shakeout",
    hit_table: str = "hit_limit_up_shakeout",
    display_name: str = "limit_up_shakeout 二次筛选样本",
    output_prefix: str = "limitup_shakeout_returns",
    description: str | None = None,
) -> int:
    """脚本入口；默认统计 limit_up_shakeout 命中样本的现金盈亏。"""
    args = parse_args(argv, description=description)
    # 止盈分档先解析校验，错误早暴露
    exit_tiers, tiers_source = resolve_exit_tiers(args)
    if args.position <= 0:
        raise SystemExit("--position 必须 > 0")
    if args.max_delay_days < 0:
        raise SystemExit("--max-delay-days 必须 >= 0")
    if args.delta < 0:
        raise SystemExit("--delta 必须 >= 0（百分点；0 = 开盘价买入）")
    if args.sell_n < 2:
        raise SystemExit("--sell-n 必须 >= 2（T+1 买入，最早 T+2 卖出）")
    for name, rng in (
        ("--gap", args.gap),
        ("--close-chg", args.close_chg),
    ):
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"{name} 需要两个数，且 MIN <= MAX")
    if not args.entry_gap:
        raise SystemExit("--entry-gap 至少需要一个闭区间")

    start = compact_date(args.start)
    end = resolve_end_date(args.end, hit_table)
    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")

    print("=" * 90)
    print(f"{display_name}现金收益")
    print(f"  统计区间：{start} ~ {end}")
    print(f"  命中策略：{strategy}")
    print(f"  T日高开：{fmt_range(args.gap)}（闭区间）")
    print(f"  T日收盘涨跌幅：{fmt_range(args.close_chg)}（闭区间）")
    print(f"  涨幅条件：{fmt_n_days_rise(args.n_days_rise)}")
    print(
        f"  T+1买入低开（原始价，相对T日收盘）："
        f"{fmt_entry_gap_ranges(args.entry_gap)}（闭区间）"
    )
    print(
        f"  T日最低价>=前一日收盘：{'要求' if args.require_low_support else '不要求'}"
    )
    if args.delta > 0:
        print(
            f"  买入规则：T+1 限价单，基准价 = (1 + a - {args.delta:g}%) × close(T)"
            "（a = open(T+1)/close(T)-1，不复权，即开盘下方 delta%）"
        )
        print("  成交条件：T+1 最低价 <= 基准价（按基准价成交）；未触及则跳过该样本")
    else:
        print("  买入规则：T+1 开盘价买入（delta=0，原规则）")
    print(
        "  止盈规则：按 T+1 收盘对买入价涨幅（收盘/买入价-1，左开右闭）分档确定逐样本止盈阈值"
    )
    print(f"    {format_tiers_summary(exit_tiers)}")
    print(f"    分档来源：{tiers_source}")
    print(
        f"  卖出规则：T+2..T+{args.sell_n} 逐日检查，开盘高于目标价按开盘价卖出，"
        f"盘中最高价触及目标价按目标价卖出；一直未触发则 T+{args.sell_n} 收盘卖出"
    )
    print(
        f"  现金口径：每笔预算 {args.position:,.0f} 元；手数模式 {args.lot_mode}；"
        f"滑点 {args.slippage:.2%}；尾盘跌停最多顺延 {args.max_delay_days} 日"
    )
    print(f"  生成报告：{'是' if args.generate_report else '否'}")
    print("-" * 90)

    print(f"读取命中样本 {start} ~ {end} ...")
    hits = load_hits(start, end, hit_table)
    if hits.empty:
        raise SystemExit(f"指定区间没有 {strategy} 命中样本")
    print(f"命中样本：{len(hits):,} 条")

    calendar = load_calendar()
    hits = attach_dates(hits, calendar, args.n_days_rise, args.sell_n)

    print("读取行情（按样本角色日期精确取数）...")
    prices = load_prices_by_keys(price_keys_for_hits(hits))
    if prices.empty:
        raise SystemExit("没有读取到行情数据")

    samples = build_samples(
        hits,
        prices,
        args.gap,
        args.close_chg,
        args.n_days_rise,
        exit_tiers,
        args.entry_gap,
        delta_pct=args.delta,
        require_low_support=args.require_low_support,
        sell_n=args.sell_n,
    )
    included = samples[samples["included"]].copy()
    if included.empty:
        raise SystemExit("筛选后没有可计算收益的样本")

    # 预先取回“T+N 收盘跌停”样本的顺延窗口，避免 calc_trades 逐笔查库
    delay_keys = delay_price_keys(samples, calendar, args.max_delay_days)
    delay_keys -= set(zip(prices["ts_code"], prices["trade_date"]))
    if delay_keys:
        delay_prices = load_prices_by_keys(delay_keys)
        if not delay_prices.empty:
            prices = pd.concat([prices, delay_prices], ignore_index=True)
    print(f"行情行数：{len(prices):,}")

    # 其余筛选全部满足、但限价单未成交而被跳过的样本数（delta=0 时恒为 0）
    filters_mask = (
        samples["passed_gap"]
        & samples["passed_close"]
        & samples["passed_rise"]
        & samples["passed_entry_gap"]
    )
    if args.require_low_support:
        filters_mask &= samples["passed_low_support"]
    not_filled_n = int((filters_mask & ~samples["buy_filled"]).sum())

    print(f"最终纳入：{len(included):,} 条 / 命中 {len(samples):,} 条")
    if args.delta > 0:
        print(f"限价未成交跳过：{not_filled_n:,} 条（其余筛选满足但 T+1 未触及买入价）")

    cash_input = build_cash_input(included)
    export_context = build_export_context(included)
    price_cache = build_price_cache(prices)
    trades = calc_trades(cash_input, args, calendar=calendar, price_cache=price_cache)
    realized = trades[trades["exit_status"] == "已卖出"].copy()
    stats_pnl = stats(realized["net_pnl"])
    stats_ret = stats(realized["net_return"])
    delayed_n = int((trades["delay_days"] > 0).sum())
    mark_n = int(trades["exit_status"].astype(str).str.startswith("超期未卖出").sum())
    buy_fail_n = int(trades["exit_status"].isin(BUY_FAIL_STATUSES).sum())
    limit_miss_n = int((trades["exit_status"] == "限价未成交").sum())
    not_sold_n = len(trades) - len(realized) - buy_fail_n
    unsold_n = not_sold_n - mark_n

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"{output_prefix}_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        build_cash_trades_export(trades, export_context).to_csv(
            run_dir / "cash_trades.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame([{"指标": k, "数值": v} for k, v in stats_pnl.items()]).to_csv(
            run_dir / "cash_summary.csv", index=False, encoding="utf-8-sig"
        )

        yearly_rows = []
        for year in sorted(realized["hit_date"].str[:4].unique()):
            group = realized[realized["hit_date"].str[:4] == year]
            yearly_rows.append(
                {
                    "年份": year,
                    "样本数": len(group),
                    "净盈亏合计": float(group["net_pnl"].sum()),
                    "平均净盈亏": float(group["net_pnl"].mean()),
                    "平均净收益率": float(group["net_return"].mean()),
                    "胜率": float((group["net_pnl"] > 0).mean()),
                }
            )
        pd.DataFrame(yearly_rows).to_csv(
            run_dir / "cash_yearly_stats.csv", index=False, encoding="utf-8-sig"
        )

        meta = {
            "generated_at": generated_at,
            "strategy": strategy,
            "hit_table": hit_table,
            "start": start,
            "end": end,
            "gap_range": list(args.gap),
            "close_chg_range": list(args.close_chg),
            "n_days_rise": args.n_days_rise,
            "entry_gap_range": (
                list(args.entry_gap[0]) if len(args.entry_gap) == 1 else None
            ),
            "entry_gap_ranges": [list(r) for r in args.entry_gap],
            "entry_gap_union": fmt_entry_gap_ranges(args.entry_gap),
            "delta": args.delta,
            "require_low_support": args.require_low_support,
            "exit_tiers": tiers_to_jsonable(exit_tiers),
            "exit_tiers_source": tiers_source,
            "hit_samples": len(samples),
            "included_samples": len(included),
            "buy_filled_samples": int(samples["buy_filled"].sum()),
            "buy_not_filled_samples": not_filled_n,
            "prices_rows": len(prices),
            "position": args.position,
            "lot_mode": args.lot_mode,
            "max_delay_days": args.max_delay_days,
            "sell_n": args.sell_n,
            "commission": args.commission,
            "min_commission": args.min_commission,
            "stamp": args.stamp,
            "transfer": args.transfer,
            "slippage": args.slippage,
            "realized_samples": len(realized),
            "delayed_samples": delayed_n,
            "mark_to_market_samples": mark_n,
            "unsold_samples": unsold_n,
            "buy_failed_samples": buy_fail_n,
            "limit_not_filled_samples": limit_miss_n,
            "total_net_pnl": float(realized["net_pnl"].sum()),
            "avg_net_pnl": stats_pnl.get("平均"),
            "avg_net_return": stats_ret.get("平均"),
            "win_rate": stats_pnl.get("胜率"),
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        report = build_cash_report(
            trades,
            args,
            exit_tiers,
            generated_at,
            start,
            end,
            title=f"# {display_name}现金收益",
            hit_table=hit_table,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    print("")
    print(f"实际买入：{int((trades['shares'] > 0).sum())} 条 / 样本 {len(trades)} 条")
    if buy_fail_n:
        print(
            f"买入失败：{buy_fail_n} 条（其中限价未成交 {limit_miss_n} 条，"
            "其余为缺 T 日收盘价/T+1 最低价等）"
        )
    print(
        f"已卖出：{len(realized)} 条；延期卖出：{delayed_n} 条；"
        f"超期未卖出(估值)：{mark_n} 条；买入后无法卖出/无行情：{unsold_n} 条"
    )
    print(f"已实现净盈亏合计：{fmt_money(realized['net_pnl'].sum())} 元")
    print(f"平均每笔净盈亏：{fmt_money(stats_pnl.get('平均'))} 元")
    print(f"平均净收益率：{fmt_pct(stats_ret.get('平均'))}")
    print(f"净收益率中位数：{fmt_pct(stats_ret.get('中位数'))}")
    print(f"胜率：{fmt_pct(stats_pnl.get('胜率'), signed=False)}")
    if delayed_n:
        print(f"延期卖出样本共 {delayed_n} 条，详见 cash_trades.csv / report.md")
    if run_dir is not None:
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（加 --report 可生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
