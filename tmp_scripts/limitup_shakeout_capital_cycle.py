#!/usr/bin/env python
"""limit_up_shakeout 二次筛选样本：固定每日投入的滚动组合收益。

与 limitup_shakeout_returns.py 的区别
------------------------------------
原脚本：每个命中样本独立投入 10 万元，逐笔统计现金盈亏，样本之间互不影响。
本脚本：账户初始资金 20 万元（可调），每个有二次筛选信号的买入日固定投入
10 万元（默认 = 初始资金的一半，也可用 --daily-invest 直接指定），不做复利；
按“两天一轮”滚动：

- T 日命中且通过二次筛选 -> T+1 开盘，用当前可投入的这一半资金买入；
- T+1 又命中 4 只 -> T+2 开盘，用另外一半 10 万元等额拆成 4 份买入；
- T 日买入的持仓按原脚本规则在 T+2 卖出，回笼资金在收盘后回到账户，
  T+3 开盘可以继续投入。如此循环。

因此每天有信号时都会投入固定的 10 万元；另外的资金留作次日投入或补足。
如果持仓因跌停无法按计划卖出，回笼资金不足，则从场外借入差额，保证次日仍能
投入约定的 10 万元；后续卖出回笼后优先还款。累计亏损导致的现金不足同样会
补足，以维持固定的每日投入，报告会单独列出最大借款及其占净值的比例。
借款利率默认 0（可调）。

筛选条件、买入价、动态止盈分档、跌停顺延等规则完全复用
limitup_shakeout_returns.py（不修改原脚本），本脚本只替换资金分配与组合记账：
- 每个有信号的买入日固定投入 `--daily-invest`（缺省 `初始资金 × --deploy-ratio`，
  默认 10 万元），等额拆分给当日所有信号；
- 每份预算按 100 股整手向下取整，买不满 1 手的信号跳过，余额留在现金；
- 卖出资金在卖出日收盘后到账，次日开盘才能再次使用（保守口径）；
- 现金不足时自动借入差额，卖出回笼后自动还款。

用法
----
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py --report
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py --capital 300000 --report
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py --capital 200000 --daily-invest 100000 --report
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py --capital 200000 --deploy-ratio 0.5 --borrow-rate 0.08 --report
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py --start 2024-01-01 --end latest --report
    uv run python tmp_scripts/limitup_shakeout_capital_cycle.py --entry-gap -0.06 -0.01 -0.09 -0.08 --delta 1.5 --report

输出（--report）
---------------
    outputs/limitup_shakeout_capital_cycle_<时间>/
        report.md
        daily_equity.csv
        portfolio_trades.csv
        yearly_stats.csv
        skipped_signals.csv（有跳过时）
        run_meta.json
        figures/equity_drawdown.png
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TMP_DIR))
os.chdir(ROOT)

import limitup_shakeout_returns as cash  # noqa: E402

# ==============================================================================
# 统一默认参数
DEFAULT_START = "20260701"
DEFAULT_END = "20261101"

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
DEFAULT_ENTRY_GAP: tuple[tuple[float, float], ...] = (
    (-0.06, -0.01),
    (-0.09, -0.08),
)

# T+1 限价买入下探深度 delta（百分点）：0 = 开盘价买入
DEFAULT_DELTA = 0
DEFAULT_REQUIRE_LOW_SUPPORT = True

# 资金与现金口径
DEFAULT_CAPITAL = 200_000.0
DEFAULT_DEPLOY_RATIO = 0.5
DEFAULT_BORROW_RATE = 0.0
DEFAULT_LOT_MODE = "floor"
DEFAULT_MAX_DELAY_DAYS = 10
DEFAULT_COMMISSION = 0.00015
DEFAULT_MIN_COMMISSION = 5.0
DEFAULT_STAMP = 0.0005
DEFAULT_TRANSFER = 0.00001
DEFAULT_SLIPPAGE = 0.0

DEFAULT_GENERATE_REPORT = True
# ==============================================================================

TRADE_CN = {
    "hit_date": "命中日期",
    "code": "股票代码",
    "name": "股票名称",
    "industry": "公司所属行业",
    "buy_date": "买入日",
    "plan_sell_date": "计划卖出日",
    "sell_date": "实际卖出日",
    "signal_budget": "本轮分配预算",
    "lots": "手数",
    "shares": "股数",
    "buy_price": "买入价(不复权,含滑点)",
    "buy_amount": "买入金额",
    "buy_fee": "买入费用",
    "cost": "买入总成本",
    "sell_price": "卖出价(不复权,含滑点)",
    "sell_amount": "卖出金额",
    "sell_fee": "卖出费用",
    "proceeds": "卖出净回笼",
    "net_pnl": "净盈亏",
    "net_return": "净收益率",
    "exit_mode": "卖出方式",
    "delay_days": "延期天数",
    "exit_status": "卖出状态",
    "close_limit_down": "计划卖出日是否收盘跌停",
    "realized": "是否已实现",
    "t_circ_mv_yi": "T日收盘后流通市值(亿元)",
    "amount_ratio_t_prev": "T成交额/T-1成交额",
    "entry_gap_close_raw": "T+1日高开比例(原始价)",
    "t1_kline": "T+1收盘K线形态",
    "t1_intraday_adj": "T+1日内涨幅(后复权)",
    "t2_close_vs_t1_open_adj": "T+2收盘对T+1开盘涨幅(后复权)",
}

TRADE_COLUMN_ORDER = [
    "hit_date",
    "code",
    "name",
    "industry",
    "buy_date",
    "plan_sell_date",
    "sell_date",
    "signal_budget",
    "lots",
    "shares",
    "buy_price",
    "buy_amount",
    "buy_fee",
    "cost",
    "sell_price",
    "sell_amount",
    "sell_fee",
    "proceeds",
    "net_pnl",
    "net_return",
    "exit_mode",
    "delay_days",
    "exit_status",
    "close_limit_down",
    "realized",
    "t_circ_mv_yi",
    "amount_ratio_t_prev",
    "entry_gap_close_raw",
    "t1_kline",
    "t1_intraday_adj",
    "t2_close_vs_t1_open_adj",
]

SKIPPED_CN = {
    "hit_date": "命中日期",
    "buy_date": "买入日",
    "code": "股票代码",
    "name": "股票名称",
    "budget": "本轮分配预算",
    "buy_price": "买入价(不复权,含滑点)",
    "reason": "跳过原因",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "limit_up_shakeout 二次筛选样本：固定每日投入、"
            "信号等额拆分、现金不足借款补足的组合收益"
        )
    )
    parser.add_argument(
        "--start", default=DEFAULT_START, help=f"命中日起始，默认 {DEFAULT_START}"
    )
    parser.add_argument(
        "--end",
        default=DEFAULT_END,
        help=f"命中日结束，默认 {DEFAULT_END}；也可写 latest",
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
            f"默认 {cash.fmt_entry_gap_ranges(DEFAULT_ENTRY_GAP).replace('%', '%%')}"
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
            f"{json.dumps(cash.tiers_to_jsonable(cash.DEFAULT_EXIT_TIERS), ensure_ascii=False)}"
        ),
    )
    parser.add_argument(
        "--open-exit",
        type=float,
        default=None,
        help="固定止盈阈值（退化为单段分档），与 --exit-tiers 互斥；缺省用动态分档",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=DEFAULT_CAPITAL,
        help=f"账户初始资金，默认 {DEFAULT_CAPITAL:,.0f} 元",
    )
    parser.add_argument(
        "--daily-invest",
        type=float,
        default=None,
        help=(
            "每个有信号的买入日固定投入金额；缺省为 初始资金 × --deploy-ratio"
            "（默认 20 万 × 50%% = 10 万）。该金额固定，不随盈亏复利"
        ),
    )
    parser.add_argument(
        "--deploy-ratio",
        type=float,
        default=DEFAULT_DEPLOY_RATIO,
        help=(
            "未指定 --daily-invest 时，每日投入占初始资金的比例，默认 0.5"
            "（即 20 万中的 10 万）；始终按初始资金计算，不随盈亏复利"
        ),
    )
    parser.add_argument(
        "--borrow-rate",
        type=float,
        default=DEFAULT_BORROW_RATE,
        help="场外借款年化利率，默认 0（无息）；按 365 天单利计息",
    )
    parser.add_argument(
        "--lot-mode",
        choices=("nearest", "floor"),
        default=DEFAULT_LOT_MODE,
        help="手数取整：nearest=最接近预算，floor=不超过预算（默认）",
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
        help="生成 report.md / CSV / 图（默认关闭）",
    )
    parser.add_argument(
        "--no-report",
        dest="generate_report",
        action="store_false",
        help="只打印控制台，不写报告文件（默认）",
    )
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument(
        "--output-dir", default="outputs", help="报告输出根目录，默认 outputs"
    )
    args = parser.parse_args(argv)
    if args.n_days_rise is None:
        args.n_days_rise = DEFAULT_N_DAYS_RISE
    else:
        args.n_days_rise = cash._resolve_cli_n_days_rise(args.n_days_rise)
    try:
        args.entry_gap = cash.normalize_entry_gap_ranges(
            args.entry_gap if args.entry_gap is not None else DEFAULT_ENTRY_GAP
        )
    except ValueError as exc:
        parser.error(str(exc))
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.capital <= 0:
        raise SystemExit("--capital 必须 > 0")
    if args.daily_invest is not None and args.daily_invest <= 0:
        raise SystemExit("--daily-invest 必须 > 0")
    if not 0 < args.deploy_ratio <= 1:
        raise SystemExit("--deploy-ratio 必须在 (0, 1] 之间")
    if args.borrow_rate < 0:
        raise SystemExit("--borrow-rate 必须 >= 0")
    if args.max_delay_days < 0:
        raise SystemExit("--max-delay-days 必须 >= 0")
    if args.delta < 0:
        raise SystemExit("--delta 必须 >= 0（百分点；0 = 开盘价买入）")
    for name, rng in (
        ("--gap", args.gap),
        ("--close-chg", args.close_chg),
    ):
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"{name} 需要两个数，且 MIN <= MAX")
    if not args.entry_gap:
        raise SystemExit("--entry-gap 至少需要一个闭区间")


def max_lots_for_budget(budget: float, price: float, args: argparse.Namespace) -> int:
    """在“买入金额 + 买入费用 <= budget”的约束下取最大整手数。

    原脚本的 position 只约束买入金额、费用另计；本脚本把每份预算视为
    包含费用的现金总额，避免为了几元手续费反复触发借款。
    """
    if budget <= 0 or price <= 0:
        return 0
    unit = price * 100.0
    lots = int(math.floor(budget / unit + 1e-12))
    while lots > 0:
        buy_amount = lots * unit
        fee = (
            max(buy_amount * args.commission, args.min_commission)
            + buy_amount * args.transfer
        )
        if buy_amount + fee <= budget + 1e-9:
            return lots
        lots -= 1
    return 0


def prepare_signals(included: pd.DataFrame) -> list[dict]:
    """把 included 样本和 calc_trades 的执行结果合并成信号列表。"""
    signals: list[dict] = []
    for row in included.itertuples(index=False):
        status = str(row.calc_exit_status)
        if status in cash.BUY_FAIL_STATUSES:
            continue
        buy_price = row.calc_buy_price_raw
        if pd.isna(buy_price) or not np.isfinite(buy_price) or buy_price <= 0:
            continue
        sell_price = row.calc_sell_price_raw
        if pd.isna(sell_price) or not np.isfinite(sell_price):
            sell_price = None
        else:
            sell_price = float(sell_price)
        sell_date = row.calc_sell_date
        if pd.isna(sell_date):
            sell_date = None
        else:
            sell_date = str(sell_date)
        signals.append(
            {
                "hit_date": str(row.hit_date),
                "code": str(row.ts_code),
                "name": str(row.name) if pd.notna(row.name) else "",
                "industry": str(row.industry) if pd.notna(row.industry) else "",
                "buy_date": str(row.t1_date),
                "plan_sell_date": str(row.t2_date),
                "sell_date": sell_date,
                "buy_price": float(buy_price),
                "sell_price_net": sell_price,
                "exit_status": status,
                "exit_mode": (
                    str(row.calc_exit_mode) if pd.notna(row.calc_exit_mode) else ""
                ),
                "delay_days": (
                    int(row.calc_delay_days)
                    if pd.notna(row.calc_delay_days)
                    else 0
                ),
                "exit_threshold": (
                    float(row.calc_exit_threshold)
                    if pd.notna(row.calc_exit_threshold)
                    else np.nan
                ),
                "t1_intraday_ret": (
                    float(row.calc_t1_intraday_ret)
                    if pd.notna(row.calc_t1_intraday_ret)
                    else np.nan
                ),
                "close_limit_down": (
                    bool(row.calc_close_limit_down)
                    if pd.notna(row.calc_close_limit_down)
                    else False
                ),
                "t_circ_mv_yi": (
                    float(row.t_circ_mv) / 10000.0
                    if pd.notna(row.t_circ_mv)
                    else np.nan
                ),
                "amount_ratio_t_prev": (
                    float(row.amount_ratio_t_prev)
                    if pd.notna(row.amount_ratio_t_prev)
                    else np.nan
                ),
                "entry_gap_close_raw": (
                    float(row.entry_gap_close_raw)
                    if pd.notna(row.entry_gap_close_raw)
                    else np.nan
                ),
                "t1_kline": (
                    str(row.t1_kline) if pd.notna(row.t1_kline) else ""
                ),
                "t1_intraday_adj": (
                    float(row.t1_intraday_adj)
                    if pd.notna(row.t1_intraday_adj)
                    else np.nan
                ),
                "t2_close_vs_t1_open_adj": (
                    float(row.t2_close_vs_t1_open_adj)
                    if pd.notna(row.t2_close_vs_t1_open_adj)
                    else np.nan
                ),
            }
        )
    return signals


def simulate(
    signals: list[dict],
    sim_days: list[str],
    close_map: dict[tuple[str, str], float],
    args: argparse.Namespace,
    capital: float,
    deploy_amount: float,
) -> dict:
    """按“初始资金一半滚动”的规则模拟组合。

    - 每个有信号的买入日开盘：把 deploy_amount 等额拆分给当日所有信号；
    - 每份预算按整手买入，预算不足 1 手则跳过；
    - 现金不足时借入差额（场外借款），卖出回笼后优先还款；
    - 卖出资金在卖出日收盘后到账，次日开盘才能使用；
    - 每日收盘按不复权收盘价重估持仓。
    """
    by_buy: dict[str, list[dict]] = defaultdict(list)
    for signal in signals:
        by_buy[signal["buy_date"]].append(signal)

    sells_by_date: dict[str, list[int]] = defaultdict(list)
    open_positions: dict[int, dict] = {}
    next_pos_id = 0

    cash_bal = float(capital)
    loan = 0.0
    total_interest = 0.0
    max_loan = 0.0
    max_loan_ratio = 0.0
    borrow_events = 0
    loan_days = 0
    max_positions = 0

    equity_rows: list[dict] = []
    trade_records: list[dict] = []
    skipped: list[dict] = []

    total_signal_days = len(by_buy)
    max_signals_per_day = max((len(v) for v in by_buy.values()), default=0)

    for day in sim_days:
        todays = by_buy.get(day, [])
        day_signals = len(todays)
        day_buys = 0
        day_sells = 0
        day_borrow = 0.0

        # ---------------- 1. 开盘买入 ----------------
        if todays:
            budget = deploy_amount / len(todays)
            orders: list[tuple[dict, int, int, float, float, float]] = []
            for signal in todays:
                px = signal["buy_price"]
                if px is None or not np.isfinite(px) or px <= 0:
                    skipped.append(
                        {
                            "hit_date": signal["hit_date"],
                            "buy_date": day,
                            "code": signal["code"],
                            "name": signal["name"],
                            "budget": budget,
                            "buy_price": px,
                            "reason": "买入价无效",
                        }
                    )
                    continue
                lots_raw = budget / (px * 100.0)
                if args.lot_mode == "nearest":
                    lots = int(round(lots_raw))
                else:
                    lots = max_lots_for_budget(budget, px, args)
                if lots <= 0:
                    skipped.append(
                        {
                            "hit_date": signal["hit_date"],
                            "buy_date": day,
                            "code": signal["code"],
                            "name": signal["name"],
                            "budget": budget,
                            "buy_price": px,
                            "reason": "预算不足1手",
                        }
                    )
                    continue
                shares = lots * 100
                buy_amount = shares * px
                buy_fee = (
                    max(buy_amount * args.commission, args.min_commission)
                    + buy_amount * args.transfer
                )
                orders.append((signal, lots, shares, buy_amount, buy_fee, budget))

            total_cost = sum(order[3] + order[4] for order in orders)
            if total_cost > cash_bal + 1e-9:
                borrowed = total_cost - cash_bal
                cash_bal += borrowed
                loan += borrowed
                day_borrow += borrowed
                borrow_events += 1
                max_loan = max(max_loan, loan)
                equity_before_buy = (
                    cash_bal
                    - loan
                    + sum(
                        pos["shares"] * pos["last_close"]
                        for pos in open_positions.values()
                    )
                )
                if equity_before_buy > 0:
                    max_loan_ratio = max(
                        max_loan_ratio, loan / equity_before_buy
                    )

            for signal, lots, shares, buy_amount, buy_fee, budget in orders:
                cost = buy_amount + buy_fee
                cash_bal -= cost
                pos = {
                    **signal,
                    "id": next_pos_id,
                    "signal_budget": budget,
                    "lots": lots,
                    "shares": shares,
                    "buy_amount": buy_amount,
                    "buy_fee": buy_fee,
                    "cost": cost,
                    "last_close": signal["buy_price"],
                    "realized": False,
                }
                next_pos_id += 1
                open_positions[pos["id"]] = pos
                if signal["sell_date"] is not None:
                    sells_by_date[signal["sell_date"]].append(pos["id"])
                day_buys += 1

        # ---------------- 2. 收盘卖出 ----------------
        for pos_id in sells_by_date.pop(day, []):
            pos = open_positions.pop(pos_id, None)
            if pos is None:
                continue
            # “超期未卖出”只是按最后收盘价估值，不是实际卖出；继续持有到期末
            if str(pos.get("exit_status", "")).startswith("超期未卖出"):
                open_positions[pos_id] = pos
                continue
            sell_price = pos.get("sell_price_net")
            if (
                sell_price is None
                or not np.isfinite(sell_price)
                or sell_price <= 0
            ):
                # 卖出价缺失：继续持有，等待后续估值
                open_positions[pos_id] = pos
                continue
            sell_amount = pos["shares"] * sell_price
            sell_fee = (
                max(sell_amount * args.commission, args.min_commission)
                + sell_amount * args.stamp
                + sell_amount * args.transfer
            )
            proceeds = sell_amount - sell_fee
            cash_bal += proceeds
            pos.update(
                {
                    "sell_price": sell_price,
                    "sell_amount": sell_amount,
                    "sell_fee": sell_fee,
                    "proceeds": proceeds,
                    "net_pnl": proceeds - pos["cost"],
                    "net_return": (
                        (proceeds - pos["cost"]) / pos["cost"]
                        if pos["cost"] > 0
                        else np.nan
                    ),
                    "realized": True,
                }
            )
            trade_records.append(pos)
            day_sells += 1

        # ---------------- 3. 计息 ----------------
        if loan > 0:
            interest = loan * args.borrow_rate / 365.0
            if interest > 0:
                loan += interest
                total_interest += interest
                max_loan = max(max_loan, loan)
            loan_days += 1

        # ---------------- 4. 还款 ----------------
        if loan > 0 and cash_bal > 0:
            repay = min(cash_bal, loan)
            cash_bal -= repay
            loan -= repay
        if loan < 1e-12:
            loan = 0.0

        # ---------------- 5. 收盘市值重估 ----------------
        invested = 0.0
        for pos in open_positions.values():
            px = close_map.get((pos["code"], day))
            if px is None or not np.isfinite(px) or px <= 0:
                px = pos["last_close"]
            else:
                pos["last_close"] = px
            invested += pos["shares"] * px

        equity = cash_bal - loan + invested
        if loan > 0 and equity > 0:
            max_loan_ratio = max(max_loan_ratio, loan / equity)
        max_positions = max(max_positions, len(open_positions))
        equity_rows.append(
            {
                "日期": day,
                "现金": cash_bal,
                "借款": loan,
                "持仓市值": invested,
                "净值": equity,
                "持仓数": len(open_positions),
                "当日信号": day_signals,
                "当日买入": day_buys,
                "当日卖出": day_sells,
                "当日新增借款": day_borrow,
                "累计利息": total_interest,
            }
        )

    # 期末仍未卖出的持仓：按最后收盘价估值，不计卖出费用
    last_day = sim_days[-1]
    for pos in open_positions.values():
        px = close_map.get((pos["code"], last_day), pos["last_close"])
        if px is None or not np.isfinite(px) or px <= 0:
            px = pos["last_close"]
        pos.update(
            {
                "sell_date": last_day,
                "sell_price": px,
                "sell_amount": pos["shares"] * px,
                "sell_fee": np.nan,
                "proceeds": np.nan,
                "net_pnl": pos["shares"] * px - pos["cost"],
                "net_return": (
                    (pos["shares"] * px - pos["cost"]) / pos["cost"]
                    if pos["cost"] > 0
                    else np.nan
                ),
                "realized": False,
                "exit_status": "期末未卖出(按最后收盘估值)",
                "exit_mode": "期末估值",
            }
        )
        trade_records.append(pos)

    equity_df = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(trade_records)
    skipped_df = pd.DataFrame(skipped)

    return {
        "equity": equity_df,
        "trades": trades_df,
        "skipped": skipped_df,
        "total_signals": len(signals),
        "total_signal_days": total_signal_days,
        "max_signals_per_day": max_signals_per_day,
        "borrow_events": borrow_events,
        "loan_days": loan_days,
        "max_loan": max_loan,
        "max_loan_ratio": (
            max_loan_ratio if borrow_events else np.nan
        ),
        "total_interest": total_interest,
        "max_positions": max_positions,
        "final_equity": (
            float(equity_df["净值"].iloc[-1]) if not equity_df.empty else capital
        ),
    }


def compute_summary(
    equity: pd.DataFrame, trades: pd.DataFrame, capital: float
) -> dict:
    """组合级汇总指标。"""
    if equity.empty:
        return {}
    eq = equity["净值"]
    curve = pd.concat([pd.Series([float(capital)]), eq], ignore_index=True)
    final = float(eq.iloc[-1])
    total_return = final / capital - 1.0
    first_date = pd.to_datetime(equity["日期"].iloc[0], format="%Y%m%d")
    last_date = pd.to_datetime(equity["日期"].iloc[-1], format="%Y%m%d")
    days = (last_date - first_date).days
    cagr = (
        (final / capital) ** (365.25 / days) - 1.0
        if days > 0 and final > 0
        else np.nan
    )
    max_dd = float((curve / curve.cummax() - 1.0).min())

    daily_ret = eq.pct_change()
    if len(daily_ret):
        daily_ret.iloc[0] = eq.iloc[0] / capital - 1.0
    std = float(daily_ret.std(ddof=1)) if len(daily_ret) > 1 else np.nan
    ann_vol = std * np.sqrt(252.0) if pd.notna(std) else np.nan
    sharpe = (
        float(daily_ret.mean()) / std * np.sqrt(252.0)
        if pd.notna(std) and std > 0
        else np.nan
    )

    closed = (
        trades[trades["realized"]].copy()
        if not trades.empty and "realized" in trades.columns
        else trades.iloc[0:0].copy()
    )
    if closed.empty or "net_pnl" not in closed.columns:
        wins = pd.Series(dtype="float64")
        losses = pd.Series(dtype="float64")
    else:
        wins = closed.loc[closed["net_pnl"] > 0, "net_pnl"]
        losses = closed.loc[closed["net_pnl"] < 0, "net_pnl"]
    avg_win = float(wins.mean()) if len(wins) else np.nan
    avg_loss = float(losses.mean()) if len(losses) else np.nan
    payoff = (
        avg_win / abs(avg_loss)
        if pd.notna(avg_win) and pd.notna(avg_loss) and avg_loss != 0
        else np.nan
    )
    profit_factor = (
        float(wins.sum() / abs(losses.sum()))
        if len(losses) and losses.sum() != 0
        else np.nan
    )
    total_fees = (
        float(trades["buy_fee"].sum() + closed["sell_fee"].sum())
        if not trades.empty
        else 0.0
    )

    invested_ratio = (
        equity["持仓市值"] / equity["净值"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)

    return {
        "final_equity": final,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "calmar": (
            cagr / abs(max_dd)
            if pd.notna(cagr) and max_dd < 0
            else np.nan
        ),
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "trades": len(closed),
        "win_rate": (
            float((closed["net_pnl"] > 0).mean()) if len(closed) else np.nan
        ),
        "avg_net_return": (
            float(closed["net_return"].mean()) if len(closed) else np.nan
        ),
        "median_net_return": (
            float(closed["net_return"].median()) if len(closed) else np.nan
        ),
        "avg_net_pnl": (
            float(closed["net_pnl"].mean()) if len(closed) else np.nan
        ),
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "total_realized_pnl": (
            float(closed["net_pnl"].sum()) if len(closed) else 0.0
        ),
        "total_fees": total_fees,
        "avg_positions": float(equity["持仓数"].mean()),
        "max_positions": int(equity["持仓数"].max()),
        "avg_invested_ratio": (
            float(invested_ratio.mean()) if invested_ratio.notna().any() else np.nan
        ),
        "days_in_market": float((equity["持仓数"] > 0).mean()),
    }


def build_yearly_stats(
    equity: pd.DataFrame, trades: pd.DataFrame, capital: float
) -> pd.DataFrame:
    if equity.empty:
        return pd.DataFrame()
    eq = equity.copy()
    eq["dt"] = pd.to_datetime(eq["日期"], format="%Y%m%d")
    eq["year"] = eq["dt"].dt.year
    rows: list[dict] = []
    prev = float(capital)
    for year, group in eq.groupby("year", sort=True):
        end_eq = float(group["净值"].iloc[-1])
        curve = pd.concat([pd.Series([prev]), group["净值"]], ignore_index=True)
        mdd = float((curve / curve.cummax() - 1.0).min())
        if not trades.empty and "realized" in trades.columns:
            sell_year = pd.to_datetime(
                trades["sell_date"], format="%Y%m%d", errors="coerce"
            ).dt.year
            year_trades = trades[trades["realized"] & (sell_year == year)]
        else:
            year_trades = trades.iloc[0:0]
        ratio = (
            group["持仓市值"] / group["净值"].replace(0, np.nan)
        ).replace([np.inf, -np.inf], np.nan)
        rows.append(
            {
                "年份": int(year),
                "交易日数": len(group),
                "年初净值": prev,
                "年末净值": end_eq,
                "年度收益": end_eq / prev - 1.0 if prev > 0 else np.nan,
                "年内最大回撤": mdd,
                "交易笔数": len(year_trades),
                "已实现盈亏": (
                    float(year_trades["net_pnl"].sum()) if len(year_trades) else 0.0
                ),
                "胜率": (
                    float((year_trades["net_pnl"] > 0).mean())
                    if len(year_trades)
                    else np.nan
                ),
                "平均仓位": (
                    float(ratio.mean()) if ratio.notna().any() else np.nan
                ),
            }
        )
        prev = end_eq
    return pd.DataFrame(rows)


def build_report(
    args: argparse.Namespace,
    result: dict,
    summary: dict,
    yearly: pd.DataFrame,
    exit_tiers: tuple,
    tiers_source: str,
    start: str,
    end: str,
    generated_at: str,
    deploy_amount: float,
) -> str:
    equity = result["equity"]
    trades = result["trades"]
    skipped = result["skipped"]
    closed = (
        trades[trades["realized"]].copy()
        if not trades.empty and "realized" in trades.columns
        else trades.iloc[0:0].copy()
    )

    summary_rows = [
        {"指标": "初始资金", "数值": cash.fmt_money(args.capital)},
        {"指标": "每日投入", "数值": cash.fmt_money(deploy_amount)},
        {
            "指标": "每日投入/初始资金",
            "数值": cash.fmt_pct(
                deploy_amount / args.capital if args.capital > 0 else np.nan,
                signed=False,
            ),
        },
        {"指标": "期末净值", "数值": cash.fmt_money(summary.get("final_equity"))},
        {"指标": "总收益", "数值": cash.fmt_pct(summary.get("total_return"))},
        {"指标": "年化收益（CAGR）", "数值": cash.fmt_pct(summary.get("cagr"))},
        {
            "指标": "最大回撤",
            "数值": cash.fmt_pct(summary.get("max_drawdown"), signed=False),
        },
        {
            "指标": "Calmar",
            "数值": (
                f"{summary['calmar']:.2f}"
                if summary.get("calmar") is not None
                and pd.notna(summary.get("calmar"))
                else "-"
            ),
        },
        {
            "指标": "年化波动",
            "数值": cash.fmt_pct(summary.get("ann_vol"), signed=False),
        },
        {
            "指标": "Sharpe（无风险利率 0）",
            "数值": (
                f"{summary['sharpe']:.2f}"
                if summary.get("sharpe") is not None
                and pd.notna(summary.get("sharpe"))
                else "-"
            ),
        },
        {"指标": "已实现交易笔数", "数值": str(summary.get("trades", 0))},
        {
            "指标": "胜率",
            "数值": cash.fmt_pct(summary.get("win_rate"), signed=False),
        },
        {
            "指标": "平均每笔净收益率",
            "数值": cash.fmt_pct(summary.get("avg_net_return")),
        },
        {
            "指标": "每笔净收益率中位数",
            "数值": cash.fmt_pct(summary.get("median_net_return")),
        },
        {
            "指标": "平均每笔净盈亏",
            "数值": cash.fmt_money(summary.get("avg_net_pnl")),
        },
        {
            "指标": "盈利因子",
            "数值": (
                f"{summary['profit_factor']:.2f}"
                if summary.get("profit_factor") is not None
                and pd.notna(summary.get("profit_factor"))
                else "-"
            ),
        },
        {
            "指标": "盈亏比（平均盈利/平均亏损）",
            "数值": (
                f"{summary['payoff_ratio']:.2f}"
                if summary.get("payoff_ratio") is not None
                and pd.notna(summary.get("payoff_ratio"))
                else "-"
            ),
        },
        {
            "指标": "已实现净盈亏合计",
            "数值": cash.fmt_money(summary.get("total_realized_pnl")),
        },
        {"指标": "交易费用合计", "数值": cash.fmt_money(summary.get("total_fees"))},
        {
            "指标": "平均持仓数",
            "数值": f"{summary.get('avg_positions', 0):.2f}",
        },
        {
            "指标": "最大同时持仓数",
            "数值": str(summary.get("max_positions", 0)),
        },
        {
            "指标": "平均仓位利用率",
            "数值": cash.fmt_pct(summary.get("avg_invested_ratio"), signed=False),
        },
        {
            "指标": "有持仓交易日占比",
            "数值": cash.fmt_pct(summary.get("days_in_market"), signed=False),
        },
        {"指标": "二次筛选后信号数", "数值": str(result["total_signals"])},
        {"指标": "有信号的交易日数", "数值": str(result["total_signal_days"])},
        {
            "指标": "单日最多信号数",
            "数值": str(result["max_signals_per_day"]),
        },
        {
            "指标": "实际买入笔数",
            "数值": str(len(trades)),
        },
        {"指标": "跳过信号数", "数值": str(len(skipped))},
        {"指标": "借款事件数", "数值": str(result["borrow_events"])},
        {"指标": "有借款余额的交易日", "数值": str(result["loan_days"])},
        {"指标": "最大借款", "数值": cash.fmt_money(result["max_loan"])},
        {
            "指标": "最大借款/净值",
            "数值": cash.fmt_pct(result.get("max_loan_ratio"), signed=False),
        },
        {"指标": "累计借款利息", "数值": cash.fmt_money(result["total_interest"])},
    ]

    exit_rows = []
    if not closed.empty and "exit_mode" in closed.columns:
        for mode, group in closed.groupby("exit_mode", sort=True):
            exit_rows.append(
                {
                    "卖出方式": str(mode),
                    "样本数": len(group),
                    "占比": cash.fmt_pct(
                        len(group) / len(closed) if len(closed) else np.nan,
                        signed=False,
                    ),
                    "净盈亏合计": cash.fmt_money(group["net_pnl"].sum()),
                    "净收益率均值": cash.fmt_pct(group["net_return"].mean()),
                }
            )

    yearly_rows = []
    for row in yearly.itertuples(index=False):
        yearly_rows.append(
            {
                "年份": row.年份,
                "交易日数": row.交易日数,
                "年初净值": cash.fmt_money(row.年初净值),
                "年末净值": cash.fmt_money(row.年末净值),
                "年度收益": cash.fmt_pct(row.年度收益),
                "年内最大回撤": cash.fmt_pct(row.年内最大回撤, signed=False),
                "交易笔数": row.交易笔数,
                "已实现盈亏": cash.fmt_money(row.已实现盈亏),
                "胜率": cash.fmt_pct(row.胜率, signed=False),
                "平均仓位": cash.fmt_pct(row.平均仓位, signed=False),
            }
        )

    skipped_rows = []
    if not skipped.empty:
        reason_counts = skipped.groupby("reason", sort=True).size()
        for reason, count in reason_counts.items():
            group = skipped[skipped["reason"] == reason]
            skipped_rows.append(
                {
                    "跳过原因": str(reason),
                    "信号数": int(count),
                    "涉及买入日数": int(group["buy_date"].nunique()),
                    "平均分配预算": cash.fmt_money(group["budget"].mean()),
                }
            )

    def trade_rows(df: pd.DataFrame) -> list[dict]:
        out = []
        for row in df.itertuples(index=False):
            out.append(
                {
                    "命中日": row.hit_date,
                    "代码": row.code,
                    "名称": row.name,
                    "买入日": row.buy_date,
                    "卖出日": row.sell_date,
                    "分配预算": cash.fmt_money(row.signal_budget),
                    "手数": int(row.lots),
                    "买入价": f"{row.buy_price:.3f}",
                    "卖出价": (
                        f"{row.sell_price:.3f}"
                        if pd.notna(row.sell_price)
                        else "-"
                    ),
                    "净盈亏": cash.fmt_money(row.net_pnl),
                    "净收益率": cash.fmt_pct(row.net_return),
                    "卖出方式": row.exit_mode,
                    "延期天数": int(row.delay_days),
                }
            )
        return out

    if not closed.empty:
        top = closed.nlargest(10, "net_pnl")
        bottom = closed.nsmallest(10, "net_pnl")
    else:
        top = closed
        bottom = closed

    lines = [
        "# limit_up_shakeout 固定每日投入滚动组合收益",
        "",
        f"- 生成时间：{generated_at}",
        "- 命中表：`hit_limit_up_shakeout`",
        f"- 统计区间：`{start}` ~ `{end}`",
        f"- 初始资金：**{args.capital:,.0f} 元**",
        f"- 每日投入：**{deploy_amount:,.0f} 元**"
        f"（固定，不随盈亏复利；约占初始资金的 "
        f"{deploy_amount / args.capital:.0%}）",
        "- 轮动方式：每个有信号的买入日开盘固定投入一轮；"
        "持仓按原脚本的 T+2 止盈/止损规则卖出，"
        "卖出资金在收盘后到账，次日开盘继续投入。",
        "- 资金不足：从场外借入差额，保证当日仍能按约定金额买入；"
        "包括跌停未回笼、累计亏损等导致的现金不足。后续卖出回笼后优先还款。",
        f"- 借款年化利率：`{args.borrow_rate:.2%}`（默认 0，单利、按 365 天计）",
        f"- T 日高开区间：`{cash.fmt_range(args.gap)}`",
        f"- T 日收盘涨跌幅区间：`{cash.fmt_range(args.close_chg)}`",
        f"- 涨幅条件：{cash.fmt_n_days_rise(args.n_days_rise)}",
        f"- T+1 买入低开区间（原始价，相对 T 日收盘）：`{cash.fmt_entry_gap_ranges(args.entry_gap)}`",
        f"- 买入 delta：`{args.delta:g}%`（0 = 开盘价买入）",
        f"- 信号日最低价条件：{'要求' if args.require_low_support else '不要求'}"
        "（后复权 `low(T) >= close(T-1)`）",
        f"- 动态止盈分档：{cash.format_tiers_summary(exit_tiers)}",
        f"- 分档来源：{tiers_source}",
        f"- 手数模式：`{args.lot_mode}`（100 股/手）",
        f"- 佣金：`{args.commission:.5%}`，最低 `{args.min_commission:.0f}` 元；"
        f"印花税：`{args.stamp:.3%}`（卖出）；过户费：`{args.transfer:.4%}`（双边）；"
        f"滑点：`{args.slippage:.2%}`（单边）",
        f"- 尾盘跌停：`T+2 收盘卖出` 且收盘跌停时，顺延到下一交易日开盘，"
        f"最多 `{args.max_delay_days}` 个交易日。",
        "",
        "## 汇总",
        "",
        cash.md_table(summary_rows, ["指标", "数值"]),
        "",
        "> 已实现盈亏只统计实际卖出的交易；期末仍未卖出的持仓按最后收盘价估值，"
        "单独标记，不计入已实现盈亏。",
        "",
        "> 每日投入固定为约定金额，不随账户净值增长而加仓；因此“平均仓位利用率”"
        "会随净值增长而下降，这是不复利的直接结果。",
        "",
        "## 分年统计",
        "",
        cash.md_table(
            yearly_rows,
            [
                "年份",
                "交易日数",
                "年初净值",
                "年末净值",
                "年度收益",
                "年内最大回撤",
                "交易笔数",
                "已实现盈亏",
                "胜率",
                "平均仓位",
            ],
        ),
        "",
        "## 卖出方式分布",
        "",
        cash.md_table(
            exit_rows,
            ["卖出方式", "样本数", "占比", "净盈亏合计", "净收益率均值"],
        ),
        "",
        "## 跳过信号",
        "",
    ]

    if skipped_rows:
        lines.append(
            cash.md_table(
                skipped_rows,
                ["跳过原因", "信号数", "涉及买入日数", "平均分配预算"],
            )
        )
        lines.append("")
        lines.append(
            "> 跳过主要发生在单日信号过多、等额拆分后不足 1 手时；"
            "被跳过的预算留在现金，不重新分配给其他股票。"
        )
    else:
        lines.append("_无_")

    lines.extend(
        [
            "",
            "## 净盈亏最高 10 笔",
            "",
            cash.md_table(
                trade_rows(top),
                [
                    "命中日",
                    "代码",
                    "名称",
                    "买入日",
                    "卖出日",
                    "分配预算",
                    "手数",
                    "买入价",
                    "卖出价",
                    "净盈亏",
                    "净收益率",
                    "卖出方式",
                    "延期天数",
                ],
            ),
            "",
            "## 净盈亏最低 10 笔",
            "",
            cash.md_table(
                trade_rows(bottom),
                [
                    "命中日",
                    "代码",
                    "名称",
                    "买入日",
                    "卖出日",
                    "分配预算",
                    "手数",
                    "买入价",
                    "卖出价",
                    "净盈亏",
                    "净收益率",
                    "卖出方式",
                    "延期天数",
                ],
            ),
            "",
            "## 口径说明",
            "",
            "- 本脚本不修改 `limitup_shakeout_returns.py`；筛选条件、买入价、"
            "动态止盈分档、跌停顺延、费用口径均复用原脚本。",
            "- 每个有信号的买入日固定投入 `--daily-invest`"
            "（缺省为 `--capital × --deploy-ratio`，默认 20 万 × 50% = 10 万），"
            "等额拆分给当日所有二次筛选后信号；每份预算按 100 股整手向下取整，"
            "取整余额留在现金，不重新分配给其他股票。",
            "- 每日投入金额固定，不随盈亏复利；盈利留在现金，任何原因导致的现金不足"
            "都通过场外借款补足，以维持固定的每日投入。"
            "报告中的“最大借款/净值”可用于评估亏损阶段的杠杆水平。",
            "- 卖出资金在卖出日收盘后到账，次日开盘才能使用；"
            "因此同一天开盘卖出的资金不会用于当天开盘买入（保守口径）。",
            "- 现金不足时自动借入差额；借款默认无息，可用 `--borrow-rate` 设置年化利率。"
            "卖出回笼后优先还款，还款后剩余的现金留作次日投入。",
            "- 持仓市值按不复权收盘价重估；持仓期通常只有 1~2 个交易日，"
            "除权除息日会有轻微偏差。",
            "- 盘中触及止盈目标价时假设能按目标价精确成交，受 T+2 最高价约束；"
            "未考虑涨停买不进、跌停排队、部分成交和盘中流动性。",
            "- 这是样本内回测；筛选条件和参数都应做样本外/滚动验证。",
            "",
            "## 输出文件",
            "",
            "- `report.md`：本报告",
            "- `daily_equity.csv`：逐日现金、借款、持仓市值、净值、仓位",
            "- `portfolio_trades.csv`：实际执行的逐笔交易",
            "- `yearly_stats.csv`：分年统计",
            "- `skipped_signals.csv`：因预算不足 1 手等原因跳过的信号（如有）",
            "- `run_meta.json`：运行参数与摘要",
            "- `figures/equity_drawdown.png`：净值、回撤、仓位曲线",
            "",
        ]
    )
    return "\n".join(lines)


def write_figure(equity: pd.DataFrame, out_dir: Path) -> None:
    if equity.empty:
        return
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.sans-serif": ["Microsoft YaHei", "SimHei"],
            "axes.unicode_minus": False,
            "figure.dpi": 130,
            "savefig.dpi": 220,
            "axes.grid": True,
            "grid.alpha": 0.22,
            "grid.linestyle": "--",
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    x = pd.to_datetime(equity["日期"], format="%Y%m%d")
    nav = equity["净值"]
    dd = nav / nav.cummax() - 1.0
    invested_ratio = (
        equity["持仓市值"] / equity["净值"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], np.nan)

    fig, axes = plt.subplots(3, 1, figsize=(12, 9.6), sharex=True)
    axes[0].plot(x, nav / 10000.0, color="#3B6FB6", linewidth=1.6)
    axes[0].set_ylabel("净值（万元）", fontsize=9)
    axes[0].set_title("半仓滚动组合净值", fontsize=12)

    axes[1].plot(x, dd * 100.0, color="#C0392B", linewidth=1.3)
    axes[1].fill_between(x, dd * 100.0, 0, color="#C0392B", alpha=0.12)
    axes[1].set_ylabel("回撤（%）", fontsize=9)
    axes[1].set_title("回撤", fontsize=11)

    axes[2].plot(x, invested_ratio * 100.0, color="#2E8B57", linewidth=1.2)
    axes[2].set_ylabel("仓位（%）", fontsize=9)
    axes[2].set_title("持仓市值 / 净值", fontsize=11)
    axes[2].set_ylim(bottom=0)

    fig.tight_layout()
    fig.savefig(fig_dir / "equity_drawdown.png", bbox_inches="tight")
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    exit_tiers, tiers_source = cash.resolve_exit_tiers(args)
    validate_args(args)

    start = cash.compact_date(args.start)
    end = cash.resolve_end_date(args.end)
    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")

    deploy_amount = (
        args.daily_invest
        if args.daily_invest is not None
        else args.capital * args.deploy_ratio
    )
    if deploy_amount <= 0:
        raise SystemExit("每日投入必须 > 0")
    # 每日投入固定为 deploy_amount，不随账户净值变化；盈利不参与加仓，
    # 现金不足（跌停未回笼、累计亏损等）时由 simulate 从场外借入差额。
    args.position = deploy_amount

    print("=" * 96)
    print("limit_up_shakeout 固定每日投入滚动组合收益")
    print(f"  统计区间：{start} ~ {end}")
    print(f"  初始资金：{args.capital:,.0f} 元")
    print(
        f"  每日投入：{deploy_amount:,.0f} 元"
        f"（固定，不随盈亏复利；约占初始资金 "
        f"{deploy_amount / args.capital:.0%}）"
    )
    print(f"  借款年化利率：{args.borrow_rate:.2%}")
    print(f"  T日高开：{cash.fmt_range(args.gap)}（闭区间）")
    print(f"  T日收盘涨跌幅：{cash.fmt_range(args.close_chg)}（闭区间）")
    print(f"  涨幅条件：{cash.fmt_n_days_rise(args.n_days_rise)}")
    print(
        "  T+1买入低开（原始价，相对T日收盘）："
        f"{cash.fmt_entry_gap_ranges(args.entry_gap)}（闭区间）"
    )
    print(
        f"  T日最低价>=前一日收盘：{'要求' if args.require_low_support else '不要求'}"
    )
    print(f"  止盈规则：{cash.format_tiers_summary(exit_tiers)}")
    print(f"  分档来源：{tiers_source}")
    print(
        f"  现金口径：整手 {args.lot_mode}；滑点 {args.slippage:.2%}；"
        f"尾盘跌停最多顺延 {args.max_delay_days} 日"
    )
    print(f"  生成报告：{'是' if args.generate_report else '否'}")
    print("-" * 96)

    print(f"读取命中样本 {start} ~ {end} ...")
    hits = cash.load_hits(start, end)
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条")

    calendar = cash.load_calendar()
    hits = cash.attach_dates(hits, calendar, args.n_days_rise)

    print("读取行情（按样本角色日期精确取数）...")
    prices = cash.load_prices_by_keys(cash.price_keys_for_hits(hits))
    if prices.empty:
        raise SystemExit("没有读取到行情数据")

    samples = cash.build_samples(
        hits,
        prices,
        args.gap,
        args.close_chg,
        args.n_days_rise,
        exit_tiers,
        args.entry_gap,
        delta_pct=args.delta,
        require_low_support=args.require_low_support,
        compute_reasons=False,
    )
    included = samples[samples["included"]].copy().reset_index(drop=True)
    if included.empty:
        raise SystemExit("筛选后没有可计算收益的样本")

    # 预先取回“T+2 收盘跌停”样本的顺延窗口，避免 calc_trades 逐笔查库
    delay_keys = cash.delay_price_keys(samples, calendar, args.max_delay_days)
    delay_keys -= set(zip(prices["ts_code"], prices["trade_date"]))
    if delay_keys:
        delay_prices = cash.load_prices_by_keys(delay_keys)
        if not delay_prices.empty:
            prices = pd.concat([prices, delay_prices], ignore_index=True)
    print(f"行情行数：{len(prices):,}")

    cash_input = cash.build_cash_input(included)
    price_cache = cash.build_price_cache(prices)
    calc = cash.calc_trades(
        cash_input, args, exit_tiers, calendar=calendar, price_cache=price_cache
    )

    for col in (
        "buy_price_raw",
        "sell_price_raw",
        "sell_date",
        "exit_status",
        "exit_mode",
        "delay_days",
        "exit_threshold",
        "t1_intraday_ret",
        "close_limit_down",
    ):
        included[f"calc_{col}"] = calc[col].to_numpy()

    signals = prepare_signals(included)
    if not signals:
        raise SystemExit("筛选后没有可执行的买入信号")

    # 收盘市值重估用：不复权收盘价
    close_map: dict[tuple[str, str], float] = {}
    for row in prices.itertuples(index=False):
        if pd.notna(row.close):
            close_map[(str(row.ts_code), str(row.trade_date))] = float(row.close)

    buy_dates = sorted({signal["buy_date"] for signal in signals})
    sell_dates = sorted(
        {signal["sell_date"] for signal in signals if signal["sell_date"]}
    )
    sim_end = max([end, *buy_dates, *sell_dates])
    sim_days = [day for day in calendar if start <= day <= sim_end]
    if not sim_days:
        raise SystemExit("交易日历没有覆盖回测区间")

    max_signals_per_day = max(
        (sum(1 for s in signals if s["buy_date"] == d) for d in buy_dates),
        default=0,
    )
    print(
        f"二次筛选后信号：{len(signals):,} 条；"
        f"信号日：{len(buy_dates):,} 天；"
        f"单日最多：{max_signals_per_day:,} 条"
    )
    print("开始模拟固定每日投入组合 ...")
    result = simulate(signals, sim_days, close_map, args, args.capital, deploy_amount)
    summary = compute_summary(result["equity"], result["trades"], args.capital)
    yearly = build_yearly_stats(result["equity"], result["trades"], args.capital)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_capital_cycle_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        equity_export = result["equity"].copy()
        equity_export.to_csv(
            run_dir / "daily_equity.csv", index=False, encoding="utf-8-sig"
        )

        trades = result["trades"]
        if not trades.empty:
            trade_export = trades[
                [c for c in TRADE_COLUMN_ORDER if c in trades.columns]
            ].copy()
            trade_export = trade_export.rename(columns=TRADE_CN)
            trade_export.to_csv(
                run_dir / "portfolio_trades.csv", index=False, encoding="utf-8-sig"
            )
        else:
            pd.DataFrame(columns=[TRADE_CN[c] for c in TRADE_COLUMN_ORDER]).to_csv(
                run_dir / "portfolio_trades.csv",
                index=False,
                encoding="utf-8-sig",
            )

        yearly.to_csv(run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig")

        skipped = result["skipped"]
        if not skipped.empty:
            skipped_export = skipped[
                [c for c in SKIPPED_CN if c in skipped.columns]
            ].rename(columns=SKIPPED_CN)
            skipped_export.to_csv(
                run_dir / "skipped_signals.csv",
                index=False,
                encoding="utf-8-sig",
            )

        meta = {
            "generated_at": generated_at,
            "strategy": "limit_up_shakeout",
            "hit_table": "hit_limit_up_shakeout",
            "start": start,
            "end": end,
            "capital": args.capital,
            "daily_invest": deploy_amount,
            "deploy_ratio": args.deploy_ratio,
            "effective_deploy_ratio": (
                deploy_amount / args.capital if args.capital > 0 else None
            ),
            "deploy_amount": deploy_amount,
            "borrow_rate": args.borrow_rate,
            "gap_range": list(args.gap),
            "close_chg_range": list(args.close_chg),
            "n_days_rise": args.n_days_rise,
            "entry_gap_ranges": [list(r) for r in args.entry_gap],
            "entry_gap_union": cash.fmt_entry_gap_ranges(args.entry_gap),
            "delta": args.delta,
            "require_low_support": args.require_low_support,
            "exit_tiers": cash.tiers_to_jsonable(exit_tiers),
            "exit_tiers_source": tiers_source,
            "lot_mode": args.lot_mode,
            "max_delay_days": args.max_delay_days,
            "commission": args.commission,
            "min_commission": args.min_commission,
            "stamp": args.stamp,
            "transfer": args.transfer,
            "slippage": args.slippage,
            "hit_samples": len(samples),
            "included_samples": len(included),
            "signals": result["total_signals"],
            "signal_days": result["total_signal_days"],
            "max_signals_per_day": result["max_signals_per_day"],
            "skipped_signals": len(result["skipped"]),
            "borrow_events": result["borrow_events"],
            "loan_days": result["loan_days"],
            "max_loan": result["max_loan"],
            "max_loan_ratio": (
                None
                if result.get("max_loan_ratio") is None
                or pd.isna(result.get("max_loan_ratio"))
                else result["max_loan_ratio"]
            ),
            "total_interest": result["total_interest"],
            "max_positions": result["max_positions"],
            "summary": {
                key: (None if pd.isna(value) else value)
                for key, value in summary.items()
            },
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        report = build_report(
            args,
            result,
            summary,
            yearly,
            exit_tiers,
            tiers_source,
            start,
            end,
            generated_at,
            deploy_amount,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        write_figure(result["equity"], run_dir)

    print("")
    print(f"期末净值：{cash.fmt_money(summary.get('final_equity'))} 元")
    print(
        f"总收益：{cash.fmt_pct(summary.get('total_return'))}；"
        f"年化：{cash.fmt_pct(summary.get('cagr'))}；"
        f"最大回撤：{cash.fmt_pct(summary.get('max_drawdown'), signed=False)}"
    )
    print(
        f"已实现交易：{summary.get('trades', 0)} 笔；"
        f"胜率：{cash.fmt_pct(summary.get('win_rate'), signed=False)}；"
        f"平均每笔：{cash.fmt_pct(summary.get('avg_net_return'))}"
    )
    print(
        f"平均持仓：{summary.get('avg_positions', 0):.2f} 笔；"
        f"最大同时持仓：{summary.get('max_positions', 0)} 笔；"
        f"平均仓位：{cash.fmt_pct(summary.get('avg_invested_ratio'), signed=False)}"
    )
    if result["borrow_events"]:
        print(
            f"借款：{result['borrow_events']} 次；"
            f"最大借款：{cash.fmt_money(result['max_loan'])} 元"
            f"（占净值 {cash.fmt_pct(result.get('max_loan_ratio'), signed=False)}）；"
            f"累计利息：{cash.fmt_money(result['total_interest'])} 元"
        )
    if len(result["skipped"]):
        print(f"跳过信号：{len(result['skipped'])} 条（预算不足 1 手等）")
    if run_dir is not None:
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（默认关闭；加 --report 可生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
