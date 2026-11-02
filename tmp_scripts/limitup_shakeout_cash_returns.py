"""计算 limit_up_shakeout 二次筛选样本按 5 万元资金实际买入、扣费卖出的现金盈亏。

默认直接从数据库按顶部 DEFAULT_* 参数实时筛选；也可以指定
--samples-csv / --report-dir 复用 limitup_shakeout_filtered_returns.py
已经生成的 included_samples.csv。

规则：
- 每个样本最多投入 DEFAULT_POSITION（默认 5 万元）；
- 按 100 股一手计算，默认不超过 5 万元，取最大整数手；
- 买入价：T+1 不复权开盘价；
- 卖出规则沿用 included_samples.csv 的“卖出方式”：
  1. T+2 开盘卖出；
  2. T+2 盘中止盈；
  3. T+2 收盘卖出；
- 若“T+2 收盘卖出”且 T+2 收盘跌停，则视为尾盘卖不出去，
  顺延到下一个交易日开盘；若开盘仍跌停，继续顺延，最多 DEFAULT_MAX_DELAY_DAYS 个交易日。
- 手续费：
  - 佣金：默认 0.025%，最低 5 元；
  - 印花税：默认 0.05%，仅卖出；
  - 过户费：默认 0.001%，买卖双边；
  - 滑点：默认 0，可通过 --slippage 设置。

用法：
    uv run python tmp_scripts/limitup_shakeout_cash_returns.py
    uv run python tmp_scripts/limitup_shakeout_cash_returns.py --start 2024-01-01 --end latest --report
    uv run python tmp_scripts/limitup_shakeout_cash_returns.py --gap 0.05 0.11 --close-chg 0.02 0.11 --rise-days 5 --rise -0.1 0.2 --entry-gap -0.11 0 --open-exit 0.02
    uv run python tmp_scripts/limitup_shakeout_cash_returns.py --report-dir outputs/limitup_shakeout_filtered_returns_20261102_224640
    uv run python tmp_scripts/limitup_shakeout_cash_returns.py --samples-csv outputs/.../included_samples.csv --position 50000
    uv run python tmp_scripts/limitup_shakeout_cash_returns.py --lot-mode floor --slippage 0.001
"""
from __future__ import annotations

import argparse
import json
import math
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

# ==============================================================================
# 筛选参数（默认走数据库实时筛选；指定 --samples-csv / --report-dir 时沿用已有样本文件）
DEFAULT_START = "20180101"
DEFAULT_END = "latest"

DEFAULT_GAP = (0.00, 0.11)
DEFAULT_CLOSE_CHG = (0.0, 0.11)

DEFAULT_RISE_DAYS = 5
DEFAULT_RISE = (-10, 10)

DEFAULT_OPEN_EXIT =0.021   # 相对于买入价2.1% 才相当于 相对于持仓成本价涨2%
DEFAULT_ENTRY_GAP = (-0.11, 0)
# ==============================================================================

DEFAULT_POSITION = 100_000.0
DEFAULT_LOT_MODE = "floor"
DEFAULT_MAX_DELAY_DAYS = 10
DEFAULT_COMMISSION = 0.00025
DEFAULT_MIN_COMMISSION = 5.0
DEFAULT_STAMP = 0.0005
DEFAULT_TRANSFER = 0.00001
DEFAULT_SLIPPAGE = 0.0
DEFAULT_GENERATE_REPORT = True
LIMIT_EPS = 1e-6

REQUIRED_COLUMNS = (
    "命中日期",
    "股票代码",
    "股票名称",
    "次日日期",
    "次次日日期",
    "次日开盘价(不复权)",
    "次次日开盘价(不复权)",
    "次次日最高价(不复权)",
    "次次日收盘价(不复权)",
    "次次日跌停价(不复权)",
    "卖出方式",
    "卖出日是否跌停",
)

TRADE_CN = {
    "hit_date": "命中日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "buy_date": "买入日",
    "plan_sell_date": "计划卖出日",
    "sell_date": "实际卖出日",
    "exit_mode": "卖出方式",
    "delay_days": "延期天数",
    "lot_mode": "手数取整方式",
    "lots": "手数",
    "shares": "股数",
    "buy_price_raw": "买入价(不复权,含滑点)",
    "sell_price_raw": "卖出价(不复权,含滑点)",
    "buy_amount": "买入金额",
    "buy_fee": "买入费用",
    "sell_amount": "卖出金额",
    "sell_fee": "卖出费用",
    "net_pnl": "净盈亏",
    "net_return": "净收益率",
    "net_return_on_budget": "净收益率(按5万预算)",
    "exit_status": "卖出状态",
    "is_delayed": "是否延期卖出",
    "close_limit_down": "原卖出日是否收盘跌停",
    "filtered_return_adj": "原筛选收益率(后复权)",
    "error": "备注",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按每笔 5 万元、整数手、含手续费计算 limit_up_shakeout 二次筛选样本的现金收益"
    )
    parser.add_argument("--report-dir", default=None,
                        help="使用已有报告目录中的 included_samples.csv，不再重新筛选")
    parser.add_argument("--samples-csv", default=None,
                        help="直接指定 included_samples.csv；优先级高于 --report-dir")
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"命中日起始，默认 {DEFAULT_START}")
    parser.add_argument("--end", default=DEFAULT_END,
                        help=f"命中日结束，默认 {DEFAULT_END}，即命中表最新日期")
    parser.add_argument("--gap", nargs=2, type=float, default=DEFAULT_GAP,
                        metavar=("MIN", "MAX"),
                        help=f"T 日高开闭区间（小数），默认 {DEFAULT_GAP}")
    parser.add_argument("--close-chg", nargs=2, type=float, default=DEFAULT_CLOSE_CHG,
                        metavar=("MIN", "MAX"),
                        help=f"T 日收盘涨跌幅闭区间（小数），默认 {DEFAULT_CLOSE_CHG}")
    parser.add_argument("--rise", nargs=2, type=float, default=DEFAULT_RISE,
                        metavar=("MIN", "MAX"),
                        help=f"近 N 日涨幅闭区间（小数），默认 {DEFAULT_RISE}")
    parser.add_argument("--rise-days", type=int, default=DEFAULT_RISE_DAYS,
                        help=f"涨幅回看交易日数，默认 {DEFAULT_RISE_DAYS}")
    parser.add_argument("--entry-gap", nargs=2, type=float, default=DEFAULT_ENTRY_GAP,
                        metavar=("MIN", "MAX"),
                        help=f"T+1 买入低开闭区间（小数），默认 {DEFAULT_ENTRY_GAP}")
    parser.add_argument("--position", type=float, default=DEFAULT_POSITION,
                        help=f"每个样本投入资金上限，默认 {DEFAULT_POSITION:,.0f}")
    parser.add_argument("--lot-mode", choices=("nearest", "floor"), default=DEFAULT_LOT_MODE,
                        help="手数取整：nearest=最接近预算，floor=不超过预算")
    parser.add_argument("--max-delay-days", type=int, default=DEFAULT_MAX_DELAY_DAYS,
                        help=f"尾盘跌停后最多顺延的交易日数，默认 {DEFAULT_MAX_DELAY_DAYS}")
    parser.add_argument("--open-exit", type=float, default=None,
                        help=f"盘中止盈阈值；默认 {DEFAULT_OPEN_EXIT}；使用已有报告时从 run_meta.json 读取")
    parser.add_argument("--commission", type=float, default=DEFAULT_COMMISSION,
                        help=f"佣金率，默认 {DEFAULT_COMMISSION}")
    parser.add_argument("--min-commission", type=float, default=DEFAULT_MIN_COMMISSION,
                        help=f"最低佣金，默认 {DEFAULT_MIN_COMMISSION:,.0f} 元")
    parser.add_argument("--stamp", type=float, default=DEFAULT_STAMP,
                        help=f"印花税率（卖出），默认 {DEFAULT_STAMP}")
    parser.add_argument("--transfer", type=float, default=DEFAULT_TRANSFER,
                        help=f"过户费率（双边），默认 {DEFAULT_TRANSFER}")
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE,
                        help=f"单边滑点，默认 {DEFAULT_SLIPPAGE}")
    parser.add_argument("--report", dest="generate_report", action="store_true",
                        help="生成 report.md / CSV（默认开启）")
    parser.add_argument("--no-report", dest="generate_report", action="store_false",
                        help="只打印控制台，不写报告文件")
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument("--output-dir", default="outputs",
                        help="报告输出根目录，默认 outputs")
    return parser.parse_args(argv)


def latest_report_dir(outputs_dir: str) -> Path:
    root = Path(outputs_dir)
    candidates = sorted(
        [p for p in root.glob("limitup_shakeout_filtered_returns_*")
         if (p / "included_samples.csv").exists()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit(f"{root} 下没有找到 included_samples.csv，请先运行 limitup_shakeout_filtered_returns.py")
    return candidates[0]


def load_samples(args: argparse.Namespace) -> tuple[pd.DataFrame, Path]:
    if args.samples_csv:
        csv_path = Path(args.samples_csv)
    else:
        report_dir = Path(args.report_dir) if args.report_dir else latest_report_dir(args.output_dir)
        csv_path = report_dir / "included_samples.csv"
    if not csv_path.exists():
        raise SystemExit(f"找不到样本文件：{csv_path}")
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(f"included_samples.csv 缺少列：{missing}")
    return df, csv_path


def load_run_meta(csv_path: Path) -> dict:
    meta_path = csv_path.parent / "run_meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def compact_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def resolve_end_date(value: str) -> str:
    if value is None or str(value).strip().lower() in ("", "latest", "none"):
        max_hit = db.scalar("SELECT MAX(trade_date) FROM hit_limit_up_shakeout")
        if not max_hit:
            raise SystemExit("hit_limit_up_shakeout 为空，请先执行 hits update")
        return str(max_hit)
    return compact_date(value)


def build_samples_from_db(args: argparse.Namespace,
                           open_exit: float) -> tuple[pd.DataFrame, str]:
    import limitup_shakeout_filtered_returns as filtered

    start = compact_date(args.start)
    end = resolve_end_date(args.end)
    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")

    print(f"读取命中样本 {start} ~ {end} ...")
    hits = filtered.load_hits(start, end)
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条")

    calendar = filtered.load_calendar()
    hits = filtered.attach_dates(hits, calendar, args.rise_days)
    needed_dates = pd.concat(
        [hits["hit_date"], hits["prev_date"], hits["rise_ref_date"],
         hits["t1_date"], hits["t2_date"]],
        ignore_index=True,
    ).dropna()
    if needed_dates.empty:
        raise SystemExit("无法定位任何交易日")
    min_date = str(needed_dates.min())
    max_date = str(needed_dates.max())

    print(f"读取行情 {min_date} ~ {max_date} ...")
    prices = filtered.load_prices(sorted(hits["ts_code"].unique()), min_date, max_date)
    if prices.empty:
        raise SystemExit("没有读取到行情数据")
    print(f"行情行数：{len(prices):,}")

    samples = filtered.build_samples(
        hits, prices, args.gap, args.close_chg, args.rise, args.rise_days,
        open_exit, args.entry_gap,
    )
    included = samples[samples["included"]].copy()
    if included.empty:
        raise SystemExit("筛选后没有可计算收益的样本")

    cash_input = pd.DataFrame({
        "命中日期": included["hit_date"].astype(str),
        "股票代码": included["ts_code"].astype(str),
        "股票名称": included["name"].fillna("").astype(str),
        "次日日期": included["t1_date"].astype(str),
        "次次日日期": included["t2_date"].astype(str),
        "次日开盘价(不复权)": included["t1_open_raw"],
        "次次日开盘价(不复权)": included["t2_open_raw"],
        "次次日最高价(不复权)": included["t2_high_raw"],
        "次次日收盘价(不复权)": included["t2_close_raw"],
        "次次日跌停价(不复权)": included["t2_down_limit"],
        "卖出方式": included["exit_type"],
        "卖出日是否跌停": included["sell_limit_down"].astype(bool),
        "收益率(后复权)": included["return_adj"],
    })
    source = f"数据库实时筛选 {start} ~ {end}"
    print(f"筛选后纳入：{len(cash_input):,} 条")
    return cash_input, source


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


def next_trade_dates(calendar: list[str], start_date: str,
                     days: int) -> list[str]:
    try:
        pos = calendar.index(start_date)
    except ValueError:
        return []
    return calendar[pos + 1: pos + 1 + days]


def to_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    return bool(value)


def is_limit_down(price: float | None, down_limit: float | None) -> bool:
    return (
        price is not None and down_limit is not None
        and np.isfinite(price) and np.isfinite(down_limit)
        and down_limit > 0 and price <= down_limit + LIMIT_EPS
    )


def resolve_sell(row: pd.Series, calendar: list[str],
                 args: argparse.Namespace, open_exit: float,
                 price_cache: dict) -> dict:
    code = str(row["股票代码"])
    plan_sell_date = str(row["次次日日期"])
    exit_mode = str(row["卖出方式"])
    close_limit = to_bool(row.get("卖出日是否跌停", False))

    t1_open = pd.to_numeric(row["次日开盘价(不复权)"], errors="coerce")
    t2_open = pd.to_numeric(row["次次日开盘价(不复权)"], errors="coerce")
    t2_high = pd.to_numeric(row["次次日最高价(不复权)"], errors="coerce")
    t2_close = pd.to_numeric(row["次次日收盘价(不复权)"], errors="coerce")
    t2_down = pd.to_numeric(row["次次日跌停价(不复权)"], errors="coerce")

    if pd.isna(t1_open) or t1_open <= 0:
        return {"exit_status": "无T+1开盘价", "sell_date": None,
                "sell_price_raw": np.nan, "delay_days": 0,
                "exit_mode": exit_mode, "error": "缺少T+1开盘价"}

    if exit_mode == "T+2 开盘":
        if pd.isna(t2_open) or t2_open <= 0:
            return {"exit_status": "无T+2开盘价", "sell_date": None,
                    "sell_price_raw": np.nan, "delay_days": 0,
                    "exit_mode": exit_mode, "error": "缺少T+2开盘价"}
        return {"exit_status": "已卖出", "sell_date": plan_sell_date,
                "sell_price_raw": float(t2_open), "delay_days": 0,
                "exit_mode": exit_mode, "error": ""}

    if exit_mode == "T+2 盘中":
        if pd.isna(t2_high) or t2_high <= 0:
            return {"exit_status": "无T+2最高价", "sell_date": None,
                    "sell_price_raw": np.nan, "delay_days": 0,
                    "exit_mode": exit_mode, "error": "缺少T+2最高价"}
        target = float(t1_open) * (1.0 + open_exit)
        target = min(target, float(t2_high))
        return {"exit_status": "已卖出", "sell_date": plan_sell_date,
                "sell_price_raw": target, "delay_days": 0,
                "exit_mode": exit_mode, "error": ""}

    # T+2 收盘卖出。若收盘跌停，尾盘卖不出去，顺延到下一交易日开盘。
    if pd.isna(t2_close) or t2_close <= 0:
        return {"exit_status": "无T+2收盘价", "sell_date": None,
                "sell_price_raw": np.nan, "delay_days": 0,
                "exit_mode": exit_mode, "error": "缺少T+2收盘价"}
    if not close_limit and not is_limit_down(float(t2_close), float(t2_down)):
        return {"exit_status": "已卖出", "sell_date": plan_sell_date,
                "sell_price_raw": float(t2_close), "delay_days": 0,
                "exit_mode": exit_mode, "error": ""}

    dates = next_trade_dates(calendar, plan_sell_date, args.max_delay_days)
    if not dates:
        return {"exit_status": "无法顺延", "sell_date": None,
                "sell_price_raw": np.nan, "delay_days": 0,
                "exit_mode": exit_mode, "error": "交易日历无后续交易日"}

    last_row = None
    for i, date in enumerate(dates, start=1):
        px = load_price_row(code, date, price_cache)
        if px is None:
            continue
        last_row = px
        open_price = px.get("open")
        down_limit = px.get("down_limit")
        if pd.notna(open_price) and open_price > 0 and not is_limit_down(
            float(open_price), float(down_limit)
        ):
            return {"exit_status": "已卖出", "sell_date": date,
                    "sell_price_raw": float(open_price), "delay_days": i,
                    "exit_mode": f"顺延{i}日开盘", "error": ""}

    # 超过最长顺延仍无法卖出：按最后一个可用收盘价做市值估值。
    if last_row is not None and pd.notna(last_row.get("close")):
        return {"exit_status": "超期未卖出(按最后收盘估值)",
                "sell_date": str(last_row["trade_date"]),
                "sell_price_raw": float(last_row["close"]),
                "delay_days": len(dates),
                "exit_mode": f"顺延{len(dates)}日后估值",
                "error": "连续跌停无法卖出，按最后收盘估值"}
    return {"exit_status": "无后续行情", "sell_date": None,
            "sell_price_raw": np.nan, "delay_days": 0,
            "exit_mode": exit_mode, "error": "顺延期间无行情"}


def fee_buy(amount: float, args: argparse.Namespace) -> float:
    return max(amount * args.commission, args.min_commission) + amount * args.transfer


def fee_sell(amount: float, args: argparse.Namespace) -> float:
    return (
        max(amount * args.commission, args.min_commission)
        + amount * args.stamp
        + amount * args.transfer
    )


def calc_trades(samples: pd.DataFrame, args: argparse.Namespace,
                open_exit: float) -> pd.DataFrame:
    calendar = load_calendar()
    price_cache: dict = {}
    rows: list[dict] = []

    for _, row in samples.iterrows():
        hit_date = str(row["命中日期"])
        code = str(row["股票代码"])
        name = str(row["股票名称"])
        buy_date = str(row["次日日期"])
        plan_sell_date = str(row["次次日日期"])
        t1_open = pd.to_numeric(row["次日开盘价(不复权)"], errors="coerce")
        buy_price = float(t1_open) * (1.0 + args.slippage) if pd.notna(t1_open) else np.nan

        sell = resolve_sell(row, calendar, args, open_exit, price_cache)
        sell_price = sell["sell_price_raw"]
        if pd.notna(sell_price) and sell_price > 0:
            sell_price = float(sell_price) * (1.0 - args.slippage)

        lots = 0
        shares = 0
        buy_amount = 0.0
        buy_fee = 0.0
        sell_amount = 0.0
        sell_fee = 0.0
        net_pnl = np.nan
        net_return = np.nan
        error = sell.get("error", "")

        if pd.notna(buy_price) and buy_price > 0:
            lot_cost = buy_price * 100.0
            if args.lot_mode == "nearest":
                lots = int(round(args.position / lot_cost))
            else:
                lots = int(math.floor(args.position / lot_cost))
            if lots > 0:
                shares = lots * 100
                buy_amount = shares * buy_price
                buy_fee = fee_buy(buy_amount, args)
                if pd.notna(sell_price) and sell_price > 0:
                    sell_amount = shares * sell_price
                    sell_fee = fee_sell(sell_amount, args)
                    net_pnl = sell_amount - sell_fee - buy_amount - buy_fee
                    net_return = net_pnl / (buy_amount + buy_fee)
            else:
                error = "资金不足1手"

        rows.append({
            "hit_date": hit_date,
            "ts_code": code,
            "name": name,
            "buy_date": buy_date,
            "plan_sell_date": plan_sell_date,
            "sell_date": sell.get("sell_date"),
            "exit_mode": sell.get("exit_mode", str(row["卖出方式"])),
            "delay_days": int(sell.get("delay_days", 0)),
            "lot_mode": args.lot_mode,
            "lots": lots,
            "shares": shares,
            "buy_price_raw": buy_price,
            "sell_price_raw": sell_price,
            "buy_amount": buy_amount,
            "buy_fee": buy_fee,
            "sell_amount": sell_amount,
            "sell_fee": sell_fee,
            "net_pnl": net_pnl,
            "net_return": net_return,
            "net_return_on_budget": (
                net_pnl / args.position if pd.notna(net_pnl) else np.nan
            ),
            "exit_status": sell.get("exit_status"),
            "is_delayed": bool(sell.get("delay_days", 0) > 0),
            "close_limit_down": to_bool(row.get("卖出日是否跌停", False)),
            "filtered_return_adj": pd.to_numeric(
                row.get("收益率(后复权)"), errors="coerce"
            ),
            "error": error,
        })
    return pd.DataFrame(rows)


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
            if avg_win is not None and avg_loss not in (None, 0) else None
        ),
        "最小": float(v.min()),
        "最大": float(v.max()),
    }


def fmt_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.2f}"


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


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


def build_report(trades: pd.DataFrame, args: argparse.Namespace,
                 source_text: str, open_exit: float,
                 generated_at: str) -> str:
    realized = trades[trades["exit_status"] == "已卖出"].copy()
    delayed = trades[trades["delay_days"] > 0].copy()
    not_sold = trades[trades["exit_status"] != "已卖出"].copy()
    mark_to_market = not_sold[
        not_sold["exit_status"].astype(str).str.startswith("超期未卖出")
    ].copy()
    unsold = not_sold[
        ~not_sold["exit_status"].astype(str).str.startswith("超期未卖出")
    ].copy()
    stats_pnl = stats(realized["net_pnl"])
    stats_ret = stats(realized["net_return"])

    exit_rows = []
    for mode, group in trades.groupby("exit_mode", sort=True):
        exit_rows.append({
            "卖出方式": str(mode),
            "样本数": len(group),
            "占比": fmt_pct(len(group) / len(trades), signed=False),
            "净盈亏合计": fmt_money(group["net_pnl"].sum()),
            "净收益率均值": fmt_pct(group["net_return"].mean()),
        })

    yearly_rows = []
    for year in sorted(realized["hit_date"].str[:4].unique()):
        group = realized[realized["hit_date"].str[:4] == year]
        yearly_rows.append({
            "年份": year,
            "样本数": len(group),
            "净盈亏合计": fmt_money(group["net_pnl"].sum()),
            "平均净盈亏": fmt_money(group["net_pnl"].mean()),
            "平均净收益率": fmt_pct(group["net_return"].mean()),
            "胜率": fmt_pct((group["net_pnl"] > 0).mean(), signed=False),
        })

    def trade_rows(df: pd.DataFrame) -> list[dict]:
        out = []
        for row in df.itertuples(index=False):
            out.append({
                "命中日": row.hit_date,
                "代码": row.ts_code,
                "名称": row.name,
                "买入日": row.buy_date,
                "实际卖出日": row.sell_date,
                "手数": int(row.lots),
                "买入价": f"{row.buy_price_raw:.3f}",
                "卖出价": f"{row.sell_price_raw:.3f}",
                "净盈亏": fmt_money(row.net_pnl),
                "净收益率": fmt_pct(row.net_return),
                "卖出方式": row.exit_mode,
            })
        return out

    summary_rows = [
        {"指标": "样本数", "数值": str(len(trades))},
        {"指标": "实际买入样本数", "数值": str(int((trades["shares"] > 0).sum()))},
        {"指标": "已卖出样本数", "数值": str(len(realized))},
        {"指标": "延期卖出样本数", "数值": str(len(delayed))},
        {"指标": "超期未卖出(按最后收盘估值)", "数值": str(len(mark_to_market))},
        {"指标": "无法卖出/无行情样本数", "数值": str(len(unsold))},
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
        {"指标": "已实现收益/预算合计", "数值": fmt_pct(
            realized["net_pnl"].sum() / (len(realized) * args.position)
            if len(realized) else None
        )},
    ]

    top = realized.nlargest(10, "net_pnl")
    bottom = realized.nsmallest(10, "net_pnl")
    delayed_view = delayed.sort_values(["hit_date", "ts_code"])

    if source_text.startswith("数据库实时筛选"):
        filter_lines = [
            f"- 统计区间：`{args.start}` ~ `{args.end}`",
            f"- T 日高开区间：`{fmt_range(args.gap)}`",
            f"- T 日收盘涨跌幅区间：`{fmt_range(args.close_chg)}`",
            f"- 近 {args.rise_days} 日涨幅区间：`{fmt_range(args.rise)}`",
            f"- T+1 买入低开区间：`{fmt_range(args.entry_gap)}`",
        ]
    else:
        filter_lines = ["- 筛选参数：沿用已有样本文件，未重新筛选"]

    lines = [
        "# limit_up_shakeout 二次筛选样本现金收益",
        "",
        f"- 生成时间：{generated_at}",
        f"- 样本来源：{source_text}",
        *filter_lines,
        f"- 样本数：{len(trades)}",
        f"- 每笔预算：`{args.position:,.0f}` 元",
        f"- 手数模式：`{args.lot_mode}`（100 股/手）",
        f"- 盘中止盈阈值：`{fmt_pct(open_exit, signed=False)}`",
        f"- 佣金：`{args.commission:.5%}`，最低 `{args.min_commission:.0f}` 元；"
        f"印花税：`{args.stamp:.3%}`（卖出）；过户费：`{args.transfer:.4%}`（双边）；"
        f"滑点：`{args.slippage:.2%}`（单边）",
        f"- 尾盘跌停：`T+2 收盘卖出` 且收盘跌停时，顺延到下一交易日开盘，最多 `{args.max_delay_days}` 个交易日。",
        "",
        "## 汇总",
        "",
        md_table(summary_rows, ["指标", "数值"]),
        "",
        f"> 已实现净盈亏合计只统计“已卖出”样本；延期卖出单独列示。",
        "",
        "## 卖出方式分布",
        "",
        md_table(exit_rows, ["卖出方式", "样本数", "占比", "净盈亏合计", "净收益率均值"]),
        "",
        "## 延期卖出明细",
        "",
    ]

    if delayed_view.empty:
        lines.append("_无_")
    else:
        delayed_rows = []
        for row in delayed_view.itertuples(index=False):
            delayed_rows.append({
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
            })
        lines.append(md_table(
            delayed_rows,
            ["命中日", "代码", "名称", "计划卖出日", "实际卖出日",
             "延期天数", "卖出方式", "卖出价", "净盈亏", "净收益率"],
        ))

    lines.extend([
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
            ["命中日", "代码", "名称", "买入日", "实际卖出日", "手数",
             "买入价", "卖出价", "净盈亏", "净收益率", "卖出方式"],
        ),
        "",
        "## 净盈亏最低 10 笔",
        "",
        md_table(
            trade_rows(bottom),
            ["命中日", "代码", "名称", "买入日", "实际卖出日", "手数",
             "买入价", "卖出价", "净盈亏", "净收益率", "卖出方式"],
        ),
        "",
        "## 口径说明",
        "",
        "- 买入价用 T+1 不复权开盘价；卖出价用实际卖出日不复权价格。",
        "- 盘中止盈按 `T+1 开盘价 × (1 + 止盈阈值)` 估算成交价，并受 T+2 最高价约束。",
        "- 佣金、印花税、过户费按成交金额计算；佣金有最低 5 元限制。",
        "- 滑点默认 0；可通过 `--slippage 0.001` 加入 0.1% 单边滑点。",
        "- 同一交易日多个样本可能重叠，合计净盈亏不是组合净值，也不代表可同时投入的资金。",
        "- “超期未卖出”样本按最后可用收盘价做市值估值，会在明细中标注，不应视为已实现收益。",
        "",
        "## 输出文件",
        "",
        "- `cash_trades.csv`：逐笔现金盈亏明细",
        "- `summary.csv`：汇总指标",
        "- `yearly_stats.csv`：分年统计",
        "- `report.md`：本报告",
        "- `run_meta.json`：运行参数",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.position <= 0:
        raise SystemExit("--position 必须 > 0")
    if args.max_delay_days < 0:
        raise SystemExit("--max-delay-days 必须 >= 0")
    if args.rise_days <= 0:
        raise SystemExit("--rise-days 必须为正整数")
    for name, rng in (
        ("--gap", args.gap),
        ("--close-chg", args.close_chg),
        ("--rise", args.rise),
        ("--entry-gap", args.entry_gap),
    ):
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"{name} 需要两个数，且 MIN <= MAX")

    use_existing = bool(args.samples_csv or args.report_dir)
    if use_existing:
        samples, csv_path = load_samples(args)
        meta = load_run_meta(csv_path)
        open_exit = args.open_exit
        if open_exit is None:
            open_exit = float(meta.get("open_exit_threshold", DEFAULT_OPEN_EXIT))
        source_text = f"现有样本文件 `{csv_path}`"
    else:
        open_exit = args.open_exit if args.open_exit is not None else DEFAULT_OPEN_EXIT
        samples, source_text = build_samples_from_db(args, open_exit)
        csv_path = None

    print("=" * 90)
    print("limit_up_shakeout 二次筛选样本现金收益")
    print(f"  样本来源：{source_text}")
    if not use_existing:
        print(f"  统计区间：{args.start} ~ {args.end}")
        print(f"  T日高开：{fmt_range(args.gap)}；T日收盘涨跌幅：{fmt_range(args.close_chg)}")
        print(f"  近{args.rise_days}日涨幅：{fmt_range(args.rise)}；T+1低开：{fmt_range(args.entry_gap)}")
    print(f"  样本数：{len(samples)}")
    print(f"  每笔预算：{args.position:,.0f} 元；手数模式：{args.lot_mode}")
    print(f"  止盈阈值：{fmt_pct(open_exit, signed=False)}；尾盘跌停最多顺延 {args.max_delay_days} 日")
    print("-" * 90)

    trades = calc_trades(samples, args, open_exit)
    realized = trades[trades["exit_status"] == "已卖出"].copy()
    stats_pnl = stats(realized["net_pnl"])
    stats_ret = stats(realized["net_return"])
    delayed_n = int((trades["delay_days"] > 0).sum())
    mark_n = int(
        trades["exit_status"].astype(str).str.startswith("超期未卖出").sum()
    )
    not_sold_n = len(trades) - len(realized)
    unsold_n = not_sold_n - mark_n

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_cash_returns_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        trades.rename(columns=TRADE_CN).to_csv(
            run_dir / "cash_trades.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame([
            {"指标": k, "数值": v} for k, v in stats_pnl.items()
        ]).to_csv(run_dir / "summary.csv", index=False, encoding="utf-8-sig")
        # 分年统计
        yearly_rows = []
        for year in sorted(realized["hit_date"].str[:4].unique()):
            group = realized[realized["hit_date"].str[:4] == year]
            yearly_rows.append({
                "年份": year,
                "样本数": len(group),
                "净盈亏合计": float(group["net_pnl"].sum()),
                "平均净盈亏": float(group["net_pnl"].mean()),
                "平均净收益率": float(group["net_return"].mean()),
                "胜率": float((group["net_pnl"] > 0).mean()),
            })
        pd.DataFrame(yearly_rows).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )
        meta_out = {
            "generated_at": generated_at,
            "source": source_text,
            "samples_csv": str(csv_path) if csv_path is not None else None,
            "start": args.start,
            "end": args.end,
            "gap_range": list(args.gap),
            "close_chg_range": list(args.close_chg),
            "rise_range": list(args.rise),
            "rise_days": args.rise_days,
            "entry_gap_range": list(args.entry_gap),
            "samples": len(trades),
            "position": args.position,
            "lot_mode": args.lot_mode,
            "open_exit": open_exit,
            "max_delay_days": args.max_delay_days,
            "commission": args.commission,
            "min_commission": args.min_commission,
            "stamp": args.stamp,
            "transfer": args.transfer,
            "slippage": args.slippage,
            "realized_samples": len(realized),
            "delayed_samples": delayed_n,
            "mark_to_market_samples": mark_n,
            "unsold_samples": unsold_n,
            "total_net_pnl": float(realized["net_pnl"].sum()),
            "avg_net_pnl": stats_pnl.get("平均"),
            "avg_net_return": stats_ret.get("平均"),
            "win_rate": stats_pnl.get("胜率"),
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta_out, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report = build_report(trades, args, source_text, open_exit, generated_at)
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    print("")
    print(f"实际买入：{int((trades['shares'] > 0).sum())} 条 / 样本 {len(trades)} 条")
    print(
        f"已卖出：{len(realized)} 条；延期卖出：{delayed_n} 条；"
        f"超期未卖出(估值)：{mark_n} 条；无法卖出/无行情：{unsold_n} 条"
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
        print("报告目录：未生成（--no-report）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
