"""统计 uptrend_limit_down 命中样本经筛选后的收益分布。

筛选条件（T 为命中日）：
1. T 日低开落在闭区间：DEFAULT_GAP[0] <= raw_open(T) / raw_pre_close(T) - 1 <= DEFAULT_GAP[1]
2. 近 N 个交易日涨幅（含 T 日）落在闭区间：
   DEFAULT_RISE[0] <= close(T) / close(T-N) - 1 <= DEFAULT_RISE[1]
3. T+1 买入低开落在闭区间：DEFAULT_ENTRY_GAP[0] <= raw_open(T+1) / raw_pre_close(T+1) - 1 <= DEFAULT_ENTRY_GAP[1]
   （raw_pre_close(T+1) 是交易所除权后的 T 日收盘，普通日等于 T 日收盘）

收益：T+1 开盘买入；
1. T+2 开盘相对 T+1 开盘涨幅 > DEFAULT_OPEN_EXIT：按 T+2 开盘卖出；
2. 否则若 T+2 最高价相对 T+1 开盘涨幅 >= DEFAULT_OPEN_EXIT：按 DEFAULT_OPEN_EXIT 止盈；
3. 否则持有到 T+2 收盘卖出：
    open_ret = open(T+2) / open(T+1) - 1
    high_ret = high(T+2) / open(T+1) - 1
    close_ret = close(T+2) / open(T+1) - 1
    return = open_ret if open_ret > DEFAULT_OPEN_EXIT
             else DEFAULT_OPEN_EXIT if high_ret >= DEFAULT_OPEN_EXIT
             else close_ret

价格口径：
- 低开用不复权 raw_open / raw_pre_close，raw_pre_close 是交易所除权后的昨收；
- 近 N 日涨幅和买卖收益用后复权价，等价于分红再投资；
- CSV 同时保留不复权收益，便于核对。

用法：
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --report
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --start 2024-01-01 --end 2026-10-23 --report
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --gap -0.05 -0.02 --entry-gap -0.05 -0.02 --rise 0.40 0.80

DEFAULT_END = "latest" 表示自动使用命中表最新日期；DEFAULT_GENERATE_REPORT = False 表示默认只打印控制台，不生成报告文件。

样本来自 hit_uptrend_limit_down。若策略逻辑或参数有调整，先执行：
    uv run quant hits update -s uptrend_limit_down --from-date 2018-01-01
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
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from quant.data import db  # noqa: E402

DEFAULT_START = "20250101"
DEFAULT_END = "20261231" 
DEFAULT_GAP = (-0.11, 0.00)
DEFAULT_RISE = (0.5,11)
DEFAULT_RISE_DAYS = 10
DEFAULT_OPEN_EXIT = 0.03
DEFAULT_ENTRY_GAP = (-0.11, 0.00)

# 不用改
DEFAULT_GENERATE_REPORT = False 
PRICE_CHUNK = 500
GAP_EPS = 1e-9
LIMIT_EPS = 1e-6

CSV_COLUMN_NAMES: dict[str, str] = {
    "hit_date": "命中日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "cal_pos": "交易日序号",
    "prev_date": "前一交易日",
    "rise_ref_date": "涨幅基准日",
    "t1_date": "次日日期",
    "t2_date": "次次日日期",
    "t_open_raw": "命中日开盘价(不复权)",
    "t_close_raw": "命中日收盘价(不复权)",
    "t_pre_close_raw": "命中日昨收价(除权后)",
    "t_adj_open": "命中日开盘价(后复权)",
    "t_adj_close": "命中日收盘价(后复权)",
    "prev_close_raw": "前一交易日收盘价(不复权)",
    "rise_ref_adj_close": "涨幅基准日收盘价(后复权)",
    "t1_open_raw": "次日开盘价(不复权)",
    "t1_pre_close_raw": "次日昨收价(除权后)",
    "t1_adj_open": "次日开盘价(后复权)",
    "entry_gap": "次日开盘低开幅度",
    "entry_gap_close_raw": "次日开盘低开幅度(对T日收盘)",
    "passed_entry_gap": "是否满足次日低开条件",
    "t2_open_raw": "次次日开盘价(不复权)",
    "t2_high_raw": "次次日最高价(不复权)",
    "t2_close_raw": "次次日收盘价(不复权)",
    "t2_down_limit": "次次日跌停价(不复权)",
    "t2_adj_open": "次次日开盘价(后复权)",
    "t2_adj_high": "次次日最高价(后复权)",
    "t2_adj_close": "次次日收盘价(后复权)",
    "gap_open": "低开幅度",
    "gap_open_prev_raw": "低开幅度(对前一日原始收盘)",
    "ret_rise": "近N日涨幅",
    "t2_open_ret": "次次日开盘收益",
    "t2_high_ret": "次次日最高收益",
    "t2_close_ret": "次次日收盘收益",
    "t2_open_ret_raw": "次次日开盘收益(不复权)",
    "t2_high_ret_raw": "次次日最高收益(不复权)",
    "t2_close_ret_raw": "次次日收盘收益(不复权)",
    "exit_at_open": "是否开盘卖出",
    "exit_intraday": "是否盘中止盈",
    "return_adj": "收益率(后复权)",
    "return_raw": "收益率(不复权)",
    "exit_type": "卖出方式",
    "sell_limit_down": "卖出日是否跌停",
    "sell_down_limit_missing": "卖出日跌停价是否缺失",
    "passed_gap": "是否满足低开条件",
    "passed_rise": "是否满足涨幅条件",
    "included": "是否纳入统计",
    "gap_open_pct": "低开幅度(%)",
    "gap_open_prev_raw_pct": "低开幅度(对前一日原始收盘,%)",
    "entry_gap_pct": "次日开盘低开幅度(%)",
    "entry_gap_close_raw_pct": "次日开盘低开幅度(对T日收盘,%)",
    "ret_rise_pct": "近N日涨幅(%)",
    "t2_open_ret_pct": "次次日开盘收益(%)",
    "t2_high_ret_pct": "次次日最高收益(%)",
    "t2_close_ret_pct": "次次日收盘收益(%)",
    "return_pct": "收益率(%)",
    "return_raw_pct": "收益率(不复权,%)",
    "year": "年份",
    "gap_bucket": "低开区间",
    "exclusion_reason": "未纳入原因",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统计 uptrend_limit_down 二次筛选样本的 T+1 开盘买 / T+2 卖出收益分布"
    )
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"命中日起始，默认 {DEFAULT_START}")
    parser.add_argument("--end", default=DEFAULT_END,
                        help=f"命中日结束，默认 {DEFAULT_END}（命中表最新日期）")
    parser.add_argument("--gap", nargs=2, type=float, default=DEFAULT_GAP,
                        metavar=("MIN", "MAX"),
                        help=f"T 日低开闭区间（小数），默认 {DEFAULT_GAP}，要求 MIN <= gap <= MAX")
    parser.add_argument("--rise", nargs=2, type=float, default=DEFAULT_RISE,
                        metavar=("MIN", "MAX"),
                        help=f"近 N 日涨幅闭区间（小数），默认 {DEFAULT_RISE}，要求 MIN <= 涨幅 <= MAX")
    parser.add_argument("--rise-days", type=int, default=DEFAULT_RISE_DAYS,
                        help=f"涨幅回看交易日数，默认 {DEFAULT_RISE_DAYS}")
    parser.add_argument("--open-exit", type=float, default=DEFAULT_OPEN_EXIT,
                        help=f"T+2 开盘涨幅超过该值时按开盘卖出；否则盘中最高价触及该值时按该值止盈，默认 {DEFAULT_OPEN_EXIT}")
    parser.add_argument("--entry-gap", nargs=2, type=float, default=DEFAULT_ENTRY_GAP,
                        metavar=("MIN", "MAX"),
                        help=f"T+1 买入低开闭区间（小数），默认 {DEFAULT_ENTRY_GAP}，要求 MIN <= gap <= MAX")
    parser.add_argument("--report", dest="generate_report", action="store_true",
                        help="生成 report.md / CSV / 收益分布图")
    parser.add_argument("--no-report", dest="generate_report", action="store_false",
                        help="只打印控制台，不生成报告文件（默认）")
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument("--output-dir", default="outputs", help="报告输出根目录，默认 outputs")
    return parser.parse_args(argv)


def compact_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def csv_frame(df: pd.DataFrame, rise_days: int) -> pd.DataFrame:
    """CSV 输出前统一改成中文表头，避免中英文列名混用。"""
    names = dict(CSV_COLUMN_NAMES)
    names["ret_rise"] = f"近{rise_days}日涨幅"
    names["ret_rise_pct"] = f"近{rise_days}日涨幅(%)"
    missing = [col for col in df.columns if col not in names]
    if missing:
        raise ValueError(f"CSV 列缺少中文表头映射：{missing}")
    return df.rename(columns=names)


def load_hits(start: str, end: str) -> pd.DataFrame:
    df = db.read_df(
        """
        SELECT trade_date, ts_code, name
        FROM hit_uptrend_limit_down
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


def attach_dates(hits: pd.DataFrame, calendar: list[str],
                 rise_days: int) -> pd.DataFrame:
    date_pos = {d: i for i, d in enumerate(calendar)}
    out = hits.copy()
    out["cal_pos"] = out["hit_date"].map(date_pos)

    offsets = (
        (-1, "prev_date"),
        (-rise_days, "rise_ref_date"),
        (1, "t1_date"),
        (2, "t2_date"),
    )
    for offset, name in offsets:
        values: list[str | None] = []
        for pos in out["cal_pos"]:
            if pd.isna(pos):
                values.append(None)
                continue
            target = int(pos) + offset
            values.append(calendar[target] if 0 <= target < len(calendar) else None)
        out[name] = values
    return out


def load_prices(codes: list[str], min_date: str, max_date: str) -> pd.DataFrame:
    frames = []
    for i in range(0, len(codes), PRICE_CHUNK):
        chunk = codes[i:i + PRICE_CHUNK]
        placeholders = ",".join(["%s"] * len(chunk))
        frames.append(
            db.read_df(
                f"""
                SELECT d.ts_code, d.trade_date, d.open, d.high, d.close,
                       d.pre_close, a.adj_factor, l.down_limit
                FROM daily d
                LEFT JOIN adj_factor a
                  ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date
                LEFT JOIN stk_limit l
                  ON l.ts_code = d.ts_code AND l.trade_date = d.trade_date
                WHERE d.ts_code IN ({placeholders})
                  AND d.trade_date BETWEEN %s AND %s
                """,
                (*chunk, min_date, max_date),
            )
        )
    prices = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if prices.empty:
        return prices
    for col in ("open", "high", "close", "pre_close", "adj_factor", "down_limit"):
        prices[col] = pd.to_numeric(prices[col], errors="coerce")
    prices["adj_open"] = prices["open"] * prices["adj_factor"]
    prices["adj_high"] = prices["high"] * prices["adj_factor"]
    prices["adj_close"] = prices["close"] * prices["adj_factor"]
    prices["trade_date"] = prices["trade_date"].astype(str)
    prices["ts_code"] = prices["ts_code"].astype(str)
    return prices


def _merge_role(samples: pd.DataFrame, prices: pd.DataFrame, date_col: str,
                columns: dict[str, str]) -> pd.DataFrame:
    right = prices[["ts_code", "trade_date", *columns]].rename(
        columns={"trade_date": date_col, **columns}
    )
    return samples.merge(right, on=["ts_code", date_col], how="left")


def build_samples(hits: pd.DataFrame, prices: pd.DataFrame,
                  gap_range: tuple[float, float], rise_range: tuple[float, float],
                  rise_days: int, open_exit_threshold: float,
                  entry_gap_range: tuple[float, float]) -> pd.DataFrame:
    samples = _merge_role(
        hits, prices, "hit_date",
        {
            "open": "t_open_raw",
            "close": "t_close_raw",
            "pre_close": "t_pre_close_raw",
            "adj_open": "t_adj_open",
            "adj_close": "t_adj_close",
        },
    )
    samples = _merge_role(
        samples, prices, "prev_date", {"close": "prev_close_raw"}
    )
    samples = _merge_role(
        samples, prices, "rise_ref_date", {"adj_close": "rise_ref_adj_close"}
    )
    samples = _merge_role(
        samples, prices, "t1_date",
        {
            "open": "t1_open_raw",
            "pre_close": "t1_pre_close_raw",
            "adj_open": "t1_adj_open",
        },
    )
    samples = _merge_role(
        samples, prices, "t2_date",
        {
            "open": "t2_open_raw", "high": "t2_high_raw",
            "close": "t2_close_raw", "down_limit": "t2_down_limit",
            "adj_open": "t2_adj_open", "adj_high": "t2_adj_high",
            "adj_close": "t2_adj_close",
        },
    )

    samples["gap_open"] = samples["t_open_raw"] / samples["t_pre_close_raw"] - 1.0
    samples["gap_open_prev_raw"] = (
        samples["t_open_raw"] / samples["prev_close_raw"] - 1.0
    )
    # T+1 买入低开：用 T+1 的交易所除权后昨收，普通日等于 T 日收盘。
    samples["entry_gap"] = (
        samples["t1_open_raw"] / samples["t1_pre_close_raw"] - 1.0
    )
    samples["entry_gap_close_raw"] = (
        samples["t1_open_raw"] / samples["t_close_raw"] - 1.0
    )
    samples["ret_rise"] = samples["t_adj_close"] / samples["rise_ref_adj_close"] - 1.0
    samples["t2_open_ret"] = (
        samples["t2_adj_open"] / samples["t1_adj_open"] - 1.0
    )
    samples["t2_high_ret"] = (
        samples["t2_adj_high"] / samples["t1_adj_open"] - 1.0
    )
    samples["t2_close_ret"] = (
        samples["t2_adj_close"] / samples["t1_adj_open"] - 1.0
    )
    samples["t2_open_ret_raw"] = (
        samples["t2_open_raw"] / samples["t1_open_raw"] - 1.0
    )
    samples["t2_high_ret_raw"] = (
        samples["t2_high_raw"] / samples["t1_open_raw"] - 1.0
    )
    samples["t2_close_ret_raw"] = (
        samples["t2_close_raw"] / samples["t1_open_raw"] - 1.0
    )
    samples["exit_at_open"] = samples["t2_open_ret"].gt(open_exit_threshold)
    samples["exit_intraday"] = (
        ~samples["exit_at_open"]
        & samples["t2_high_ret"].ge(open_exit_threshold)
    )
    samples["return_adj"] = samples["t2_close_ret"]
    samples["return_raw"] = samples["t2_close_ret_raw"]
    samples.loc[samples["exit_intraday"], "return_adj"] = open_exit_threshold
    samples.loc[samples["exit_intraday"], "return_raw"] = open_exit_threshold
    samples.loc[samples["exit_at_open"], "return_adj"] = samples.loc[
        samples["exit_at_open"], "t2_open_ret"
    ]
    samples.loc[samples["exit_at_open"], "return_raw"] = samples.loc[
        samples["exit_at_open"], "t2_open_ret_raw"
    ]
    # 开盘未触发止盈且缺少最高价时，无法判断盘中是否触及，按缺失处理。
    missing_high = ~samples["exit_at_open"] & samples["t2_high_ret"].isna()
    samples.loc[missing_high, "return_adj"] = np.nan
    samples.loc[missing_high, "return_raw"] = np.nan

    samples["exit_type"] = "T+2 收盘"
    samples.loc[samples["exit_intraday"], "exit_type"] = "T+2 盘中"
    samples.loc[samples["exit_at_open"], "exit_type"] = "T+2 开盘"
    samples.loc[samples["return_adj"].isna(), "exit_type"] = None

    # 卖出日（T+2）收盘是否跌停；只做统计备注，不改变收益计算。
    samples["sell_down_limit_missing"] = (
        samples["t2_down_limit"].isna() | samples["t2_down_limit"].le(0)
    )
    samples["sell_limit_down"] = (
        samples["t2_down_limit"].gt(0)
        & samples["t2_close_raw"].le(samples["t2_down_limit"] + LIMIT_EPS)
    ).fillna(False)

    numeric_cols = [
        "gap_open", "gap_open_prev_raw", "entry_gap", "entry_gap_close_raw",
        "ret_rise", "t2_open_ret", "t2_high_ret", "t2_close_ret",
        "t2_open_ret_raw", "t2_high_ret_raw", "t2_close_ret_raw",
        "return_adj", "return_raw",
    ]
    for col in numeric_cols:
        samples[col] = samples[col].replace([np.inf, -np.inf], np.nan)

    # 闭区间：两端都包含，允许恰好等于边界。
    gap_lo, gap_hi = gap_range
    entry_lo, entry_hi = entry_gap_range
    samples["passed_gap"] = (
        samples["gap_open"].ge(gap_lo - GAP_EPS)
        & samples["gap_open"].le(gap_hi + GAP_EPS)
    )
    rise_lo, rise_hi = rise_range
    samples["passed_rise"] = (
        samples["ret_rise"].ge(rise_lo - GAP_EPS)
        & samples["ret_rise"].le(rise_hi + GAP_EPS)
    )
    samples["passed_entry_gap"] = (
        samples["entry_gap"].ge(entry_lo - GAP_EPS)
        & samples["entry_gap"].le(entry_hi + GAP_EPS)
    )
    samples["included"] = (
        samples["passed_gap"]
        & samples["passed_rise"]
        & samples["passed_entry_gap"]
        & samples["return_adj"].notna()
    )

    for src, dst in (
        ("gap_open", "gap_open_pct"),
        ("gap_open_prev_raw", "gap_open_prev_raw_pct"),
        ("entry_gap", "entry_gap_pct"),
        ("entry_gap_close_raw", "entry_gap_close_raw_pct"),
        ("ret_rise", "ret_rise_pct"),
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
        if value < lo:
            return f"< {lo:.0%}"
        if value > hi:
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
    samples["exclusion_reason"] = samples.apply(
        lambda row: _exclusion_reason(row, rise_days), axis=1
    )
    return samples


def _exclusion_reason(row: pd.Series, rise_days: int) -> str:
    if bool(row["included"]):
        return ""
    reasons: list[str] = []
    if pd.isna(row["cal_pos"]):
        reasons.append("命中日不在交易日历")
    if pd.isna(row["gap_open"]):
        reasons.append("缺少T日开盘价或交易所昨收")
    elif not bool(row["passed_gap"]):
        reasons.append("T日低开不在区间")
    if pd.isna(row["rise_ref_date"]):
        reasons.append(f"命中日前不足{rise_days}个交易日")
    elif pd.isna(row["ret_rise"]):
        reasons.append(f"缺少T-{rise_days}行情")
    elif not bool(row["passed_rise"]):
        reasons.append(f"近{rise_days}日涨幅不在区间")
    if pd.isna(row["entry_gap"]):
        reasons.append("缺少T+1开盘价或T+1昨收")
    elif not bool(row["passed_entry_gap"]):
        reasons.append("T+1开盘不在区间")
    if pd.isna(row["t1_date"]) or pd.isna(row["t2_date"]):
        reasons.append("缺少T+1/T+2交易日")
    elif pd.isna(row["return_adj"]):
        if not bool(row["exit_at_open"]) and pd.isna(row["t2_high_ret"]):
            reasons.append("缺少T+2最高价，无法判断盘中止盈")
        else:
            reasons.append("缺少T+1开盘价或T+2行情（停牌/行情未产生）")
    return "；".join(dict.fromkeys(reasons))


def distribution_stats(rets: pd.Series) -> dict[str, float | int | None]:
    rets = pd.to_numeric(rets, errors="coerce").dropna().astype(float)
    n = len(rets)
    if n == 0:
        return {
            "样本数": 0,
            "平均收益率": None,
            "中位数": None,
            "标准差": None,
            "胜率": None,
            "平均盈利": None,
            "平均亏损": None,
            "盈亏比": None,
            "最小收益率": None,
            "最大收益率": None,
            "均值95%置信区间下限": None,
            "均值95%置信区间上限": None,
        }

    mean = float(rets.mean())
    std = float(rets.std(ddof=1)) if n > 1 else None
    wins = rets[rets > 0]
    losses = rets[rets < 0]
    avg_win = float(wins.mean()) if not wins.empty else None
    avg_loss = float(losses.mean()) if not losses.empty else None
    profit_factor = (
        avg_win / abs(avg_loss)
        if avg_win is not None and avg_loss not in (None, 0)
        else None
    )
    stats: dict[str, float | int | None] = {
        "样本数": n,
        "平均收益率": mean,
        "中位数": float(rets.median()),
        "标准差": std,
        "胜率": float((rets > 0).mean()),
        "平均盈利": avg_win,
        "平均亏损": avg_loss,
        "盈亏比": profit_factor,
        "最小收益率": float(rets.min()),
        "最大收益率": float(rets.max()),
        "均值95%置信区间下限": None,
        "均值95%置信区间上限": None,
    }
    if std is not None:
        se = std / np.sqrt(n)
        stats["均值95%置信区间下限"] = mean - 1.96 * se
        stats["均值95%置信区间上限"] = mean + 1.96 * se
    for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        stats[f"P{int(q * 100):02d}"] = float(rets.quantile(q))
    return stats


def grouped_stats(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    rows = []
    for key, group in df.groupby(group_col, sort=True, observed=True):
        rets = pd.to_numeric(group["return_adj"], errors="coerce").dropna()
        if rets.empty:
            continue
        rows.append({
            "分组": str(key),
            "样本数": len(rets),
            "平均收益率": float(rets.mean()),
            "中位数": float(rets.median()),
            "胜率": float((rets > 0).mean()),
            "最小收益率": float(rets.min()),
            "最大收益率": float(rets.max()),
            "标准差": float(rets.std(ddof=1)) if len(rets) > 1 else None,
        })
    return pd.DataFrame(rows)


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def fmt_num(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.2f}"


def fmt_range(value: tuple[float, float]) -> str:
    return f"[{fmt_pct(value[0], signed=False)}, {fmt_pct(value[1], signed=False)}]"


def md_table(rows: list[dict[str, object]],
             columns: list[str] | None = None) -> str:
    if not rows:
        return "_无_"
    columns = columns or list(rows[0].keys())
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join(lines)


def write_plot(included: pd.DataFrame, path: Path) -> bool:
    if included.empty:
        return False

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rets = included["return_adj"] * 100.0
    years = sorted(included["year"].unique())
    data = [included.loc[included["year"] == y, "return_adj"] * 100.0 for y in years]

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].hist(rets, bins=30, color="#4C78A8", edgecolor="white")
    axes[0].axvline(0, color="black", linewidth=1)
    axes[0].axvline(rets.mean(), color="#E45756", linewidth=1.5,
                    label=f"Mean {rets.mean():.2f}%")
    axes[0].axvline(rets.median(), color="#F58518", linewidth=1.5,
                    linestyle="--", label=f"Median {rets.median():.2f}%")
    axes[0].set_title("Return distribution")
    axes[0].set_xlabel("Return (%)")
    axes[0].set_ylabel("Samples")
    axes[0].legend()

    box = axes[1].boxplot(data, showmeans=True, patch_artist=True)
    for patch in box["boxes"]:
        patch.set_facecolor("#A5C8E1")
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set_xticks(range(1, len(years) + 1))
    axes[1].set_xticklabels(years)
    axes[1].set_title("Return by hit year")
    axes[1].set_xlabel("Year")
    axes[1].set_ylabel("Return (%)")

    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def build_report(samples: pd.DataFrame, included: pd.DataFrame,
                 stats: dict[str, float | int | None], yearly: pd.DataFrame,
                 gap_stats: pd.DataFrame, args: argparse.Namespace,
                 start: str, end: str, missing_adj: int,
                 plot_created: bool, generated_at: str) -> str:
    total = len(samples)
    gap_ok = int(samples["passed_gap"].sum())
    rise_ok = int(samples["passed_rise"].sum())
    both_ok = int((samples["passed_gap"] & samples["passed_rise"]).sum())
    entry_ok = int(samples["passed_entry_gap"].sum())
    all_ok = int((
        samples["passed_gap"]
        & samples["passed_rise"]
        & samples["passed_entry_gap"]
    ).sum())
    n_included = int(samples["included"].sum())
    n_sell_limit_down = int(included["sell_limit_down"].sum()) if n_included else 0
    n_sell_limit_down_missing = (
        int(included["sell_down_limit_missing"].sum()) if n_included else 0
    )
    missing_forward = int(samples["return_adj"].isna().sum())
    missing_rise = int(samples["ret_rise"].isna().sum())
    missing_gap = int(samples["gap_open"].isna().sum())

    funnel = [
        {"阶段": "命中样本总数", "样本数": total, "说明": "hit_uptrend_limit_down"},
        {"阶段": f"T日低开 ∈ {fmt_range(args.gap)}", "样本数": gap_ok, "说明": "raw_open / raw_pre_close - 1"},
        {"阶段": f"近{args.rise_days}日涨幅 ∈ {fmt_range(args.rise)}", "样本数": rise_ok, "说明": "后复权收盘价"},
        {"阶段": "同时满足低开+涨幅", "样本数": both_ok, "说明": "尚未要求T+1买入低开"},
        {"阶段": f"T+1开盘 ∈ {fmt_range(args.entry_gap)}", "样本数": entry_ok, "说明": "raw_open(T+1)/raw_pre_close(T+1)-1"},
        {"阶段": "三项同时满足", "样本数": all_ok, "说明": "尚未要求T+1/T+2行情完整"},
        {"阶段": "最终纳入收益统计", "样本数": n_included, "说明": "且T+1开盘、T+2行情可计算"},
        {"阶段": "三项满足但缺T+1/T+2行情", "样本数": all_ok - n_included, "说明": "停牌或行情尚未产生"},
    ]
    coverage = [
        {"项目": "缺T日开盘/昨收", "样本数": missing_gap, "说明": "无法计算低开"},
        {"项目": f"缺T-{args.rise_days}行情", "样本数": missing_rise, "说明": "无法计算近N日涨幅"},
        {"项目": "缺T+1/T+2行情", "样本数": missing_forward, "说明": "无法计算前向收益"},
    ]

    summary_rows = [
        {"指标": "样本数", "数值": str(stats["样本数"])},
        {"指标": "平均收益率", "数值": fmt_pct(stats["平均收益率"])},
        {"指标": "中位数", "数值": fmt_pct(stats["中位数"])},
        {"指标": "标准差", "数值": fmt_pct(stats["标准差"], signed=False)},
        {"指标": "胜率", "数值": fmt_pct(stats["胜率"], signed=False)},
        {"指标": "平均盈利", "数值": fmt_pct(stats["平均盈利"])},
        {"指标": "平均亏损", "数值": fmt_pct(stats["平均亏损"])},
        {"指标": "盈亏比", "数值": fmt_num(stats["盈亏比"])},
        {"指标": "最小收益率", "数值": fmt_pct(stats["最小收益率"])},
        {"指标": "最大收益率", "数值": fmt_pct(stats["最大收益率"])},
        {"指标": "均值95%置信区间",
         "数值": (
             f"[{fmt_pct(stats['均值95%置信区间下限'])}, "
             f"{fmt_pct(stats['均值95%置信区间上限'])}]"
         )},
    ]

    quantile_rows = [
        {"分位": f"P{int(q * 100):02d}", "收益率": fmt_pct(stats.get(f"P{int(q * 100):02d}"))}
        for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
    ]

    yearly_rows = []
    for row in yearly.itertuples(index=False):
        yearly_rows.append({
            "年份": row.分组,
            "样本数": row.样本数,
            "平均": fmt_pct(row.平均收益率),
            "中位数": fmt_pct(row.中位数),
            "胜率": fmt_pct(row.胜率, signed=False),
            "最小": fmt_pct(row.最小收益率),
            "最大": fmt_pct(row.最大收益率),
            "标准差": fmt_pct(row.标准差, signed=False),
        })

    gap_rows = []
    for row in gap_stats.itertuples(index=False):
        gap_rows.append({
            "低开区间": row.分组,
            "样本数": row.样本数,
            "平均": fmt_pct(row.平均收益率),
            "中位数": fmt_pct(row.中位数),
            "胜率": fmt_pct(row.胜率, signed=False),
            "最小": fmt_pct(row.最小收益率),
            "最大": fmt_pct(row.最大收益率),
        })

    exit_rows = []
    if n_included:
        for exit_type in ("T+2 开盘", "T+2 盘中", "T+2 收盘"):
            group = included[included["exit_type"] == exit_type]
            rets = pd.to_numeric(group["return_adj"], errors="coerce").dropna()
            if rets.empty:
                continue
            exit_rows.append({
                "卖出方式": exit_type,
                "样本数": len(rets),
                "占比": fmt_pct(len(rets) / n_included, signed=False),
                "平均": fmt_pct(rets.mean()),
                "中位数": fmt_pct(rets.median()),
                "胜率": fmt_pct((rets > 0).mean(), signed=False),
            })

    def sample_rows(df: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for row in df.itertuples(index=False):
            rows.append({
                "命中日": row.hit_date,
                "代码": row.ts_code,
                "名称": row.name,
                "低开": fmt_pct(row.gap_open),
                f"{args.rise_days}日涨幅": fmt_pct(row.ret_rise),
                "次日低开": fmt_pct(row.entry_gap),
                "卖出方式": row.exit_type,
                "收益率": fmt_pct(row.return_adj),
            })
        return rows

    top = included.nlargest(5, "return_adj") if not included.empty else included
    bottom = included.nsmallest(5, "return_adj") if not included.empty else included

    lines = [
        "# uptrend_limit_down 筛选样本收益分布",
        "",
        f"- 生成时间：{generated_at}",
        f"- 命中表：`hit_uptrend_limit_down`",
        f"- 命中区间：`{start}` ~ `{end}`",
        f"- T 日低开区间：`{fmt_range(args.gap)}`（闭区间；`raw_open / raw_pre_close - 1`）",
        f"- 近 {args.rise_days} 日涨幅区间：`{fmt_range(args.rise)}`（闭区间，窗口含 T 日；`后复权 close(T) / close(T-{args.rise_days}) - 1`）",
        f"- T+1 买入低开区间：`{fmt_range(args.entry_gap)}`（闭区间；`raw_open(T+1) / raw_pre_close(T+1) - 1`；普通交易日等于 `open(T+1)/close(T) - 1`）",
        "- 卖出规则（后复权）：",
        f"  - T+2 开盘 / T+1 开盘 - 1 > `{fmt_pct(args.open_exit, signed=False)}`：按 T+2 开盘卖出；",
        f"  - 否则 T+2 最高价 / T+1 开盘 - 1 >= `{fmt_pct(args.open_exit, signed=False)}`：按 `{fmt_pct(args.open_exit, signed=False)}` 止盈；",
        "  - 否则持有到 T+2 收盘卖出。",
        "- 买入：T+1 开盘",
        "- 收益率：卖出价 / T+1 后复权开盘 - 1（盘中止盈按阈值计）",
        "- 交易成本：未扣除手续费、印花税和滑点",
        (
            f"- 卖出日跌停备注：最终样本中 {n_sell_limit_down} 条"
            f"（{fmt_pct(n_sell_limit_down / n_included if n_included else 0.0, signed=False)}）"
            "的卖出日（T+2）收盘跌停；收益计算未做卖出限制，仍假设跌停也能卖出。"
            + (
                f" 另有 {n_sell_limit_down_missing} 条缺少跌停价，无法判断。"
                if n_sell_limit_down_missing else ""
            )
        ),
        "",
        "## 样本漏斗",
        "",
        md_table(funnel, ["阶段", "样本数", "说明"]),
        "",
        "> 前三项为单项条件，之后为逐级筛选；最后一行是三项都满足但无法计算前向收益的样本。区间条件均为闭区间，恰好等于边界也算满足。",
        "",
        "## 数据覆盖（全部命中样本）",
        "",
        md_table(coverage, ["项目", "样本数", "说明"]),
        "",
        "> 缺失项可能重叠，不能相加。",
        "",
        "## 收益分布（最终样本）",
        "",
        md_table(summary_rows, ["指标", "数值"]),
        "",
        "## 卖出方式分布",
        "",
        md_table(exit_rows, ["卖出方式", "样本数", "占比", "平均", "中位数", "胜率"]),
        "",
        "## 分位数",
        "",
        md_table(quantile_rows, ["分位", "收益率"]),
        "",
        "## 分年统计",
        "",
        md_table(yearly_rows, ["年份", "样本数", "平均", "中位数", "胜率", "最小", "最大", "标准差"]),
        "",
        "## 按低开幅度分组",
        "",
        md_table(gap_rows, ["低开区间", "样本数", "平均", "中位数", "胜率", "最小", "最大"]),
        "",
        "## 最佳 / 最差样本",
        "",
        "### 收益最高 5 个",
        "",
        md_table(sample_rows(top), ["命中日", "代码", "名称", "低开", f"{args.rise_days}日涨幅", "次日低开", "卖出方式", "收益率"]),
        "",
        "### 收益最低 5 个",
        "",
        md_table(sample_rows(bottom), ["命中日", "代码", "名称", "低开", f"{args.rise_days}日涨幅", "次日低开", "卖出方式", "收益率"]),
        "",
    ]

    if plot_created:
        lines.extend([
            "## 收益分布图",
            "",
            "![收益分布](return_distribution.png)",
            "",
        ])

    lines.extend([
        "## 口径说明",
        "",
        "- 低开用不复权 `raw_open / raw_pre_close - 1`。`raw_pre_close` 是交易所除权后的昨收，避免分红/送转造成假低开；普通交易日它与前一日原始收盘价相同。",
        "- T+1 买入低开用 `raw_open(T+1) / raw_pre_close(T+1) - 1`。`raw_pre_close(T+1)` 是交易所除权后的 T 日收盘，普通交易日等于 `close(T)`；CSV 同时保留直接对 T 日原始收盘的 `entry_gap_close_raw` 供核对。",
        "- 近 N 日涨幅和买卖收益用后复权价，等价于分红再投资；CSV 里同时保留“收益率(不复权)”列供核对。",
        "- 卖出规则：T+2 开盘涨幅超过阈值时按开盘卖；否则用 T+2 最高价判断盘中是否触及阈值，触及则按阈值止盈；否则持有到收盘。",
        "- 盘中止盈假设能在阈值价格精确成交，未考虑滑点、盘中路径和涨跌停无法成交的情况。",
        "- 「T+2 开盘」「T+2 盘中」组胜率 100% 是规则触发的结果，不代表该组选股能力更强。",
        "- T+1、T+2 按 SSE 交易日历定位。个股在 T+1/T+2 停牌或行情尚未产生时，该样本不纳入收益统计。",
        "- 未考虑 T+1 开盘涨停买不进、跌停卖不出、涨跌停板成交限制、交易费用和滑点；这是理论开盘价/收盘价下的样本级分布。",
        "- 同一天多个样本的收益可能重叠，本报告不是复利净值曲线，不能直接当作策略年化收益。",
        f"- 行情表中缺失 `adj_factor` 的行数：{missing_adj}。",
        "",
        "## 复现",
        "",
        "```powershell",
        "uv run quant hits update -s uptrend_limit_down --from-date 2018-01-01",
        "uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py",
        "```",
        "",
        "## 输出文件",
        "",
        "- `report.md`：本报告",
        "- `samples.csv`：全部命中样本、筛选标记和排除原因",
        "- `included_samples.csv`：最终纳入收益统计的样本",
        "- `summary.csv`：收益分布指标",
        "- `yearly_stats.csv`：分年统计",
        "- `gap_bucket_stats.csv`：按低开幅度分组统计",
        "- `run_meta.json`：运行参数和样本数",
        "- `return_distribution.png`：收益分布图（有样本时生成）",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = compact_date(args.start)
    if args.end is None or str(args.end).strip().lower() in ("", "latest", "none"):
        max_hit = db.scalar("SELECT MAX(trade_date) FROM hit_uptrend_limit_down")
        if not max_hit:
            raise SystemExit("hit_uptrend_limit_down 为空，请先执行 hits update")
        end = str(max_hit)
    else:
        end = compact_date(args.end)

    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")
    if args.rise_days <= 0:
        raise SystemExit("--rise-days 必须为正整数")
    for name, rng in (
        ("--gap", args.gap),
        ("--rise", args.rise),
        ("--entry-gap", args.entry_gap),
    ):
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"{name} 需要两个数，且 MIN <= MAX")

    print("=" * 90)
    print("uptrend_limit_down 筛选策略")
    print(f"  时间区间：{start} ~ {end}")
    print(f"  命中策略：uptrend_limit_down")
    print(f"  T日低开：{fmt_range(args.gap)}（闭区间）")
    print(f"  近{args.rise_days}日涨幅：{fmt_range(args.rise)}（闭区间）")
    print(f"  T+1买入低开：{fmt_range(args.entry_gap)}（闭区间）")
    print("  买入时点：T+1 开盘")
    print(
        f"  止盈规则：T+2 开盘相对 T+1 开盘涨幅 > "
        f"{fmt_pct(args.open_exit, signed=False)} 按 T+2 开盘卖出；"
    )
    print(
        f"            否则 T+2 最高价触及 {fmt_pct(args.open_exit, signed=False)} "
        f"按 {fmt_pct(args.open_exit, signed=False)} 止盈；否则 T+2 收盘卖出"
    )
    print("  费用口径：未扣手续费、印花税、滑点")
    print(f"  生成报告：{'是' if args.generate_report else '否'}")
    print("-" * 90)
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = load_hits(start, end)
    if hits.empty:
        raise SystemExit("指定区间没有 uptrend_limit_down 命中样本")
    print(f"命中样本：{len(hits)} 条")

    calendar = load_calendar()
    hits = attach_dates(hits, calendar, args.rise_days)

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
    prices = load_prices(sorted(hits["ts_code"].unique()), min_date, max_date)
    if prices.empty:
        raise SystemExit("没有读取到行情数据")
    print(f"行情行数：{len(prices):,}")
    missing_adj = int(prices["adj_factor"].isna().sum())

    samples = build_samples(
        hits, prices, args.gap, args.rise, args.rise_days, args.open_exit,
        args.entry_gap,
    )
    included = samples[samples["included"]].copy()
    stats = distribution_stats(included["return_adj"])
    yearly = grouped_stats(included, "year")
    gap_stats = grouped_stats(included, "gap_bucket")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"uptrend_limit_down_filtered_returns_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        csv_frame(samples, args.rise_days).to_csv(
            run_dir / "samples.csv", index=False, encoding="utf-8-sig"
        )
        csv_frame(included, args.rise_days).to_csv(
            run_dir / "included_samples.csv", index=False, encoding="utf-8-sig"
        )
        summary_rows = []
        for key, value in stats.items():
            if key == "样本数":
                text = str(int(value)) if value is not None else "-"
            elif key == "盈亏比":
                text = fmt_num(value)
            elif key in ("标准差", "胜率"):
                text = fmt_pct(value, signed=False)
            else:
                text = fmt_pct(value)
            summary_rows.append({"指标": key, "数值": text})
        pd.DataFrame(summary_rows).to_csv(
            run_dir / "summary.csv", index=False, encoding="utf-8-sig"
        )
        yearly.rename(columns={"分组": "年份"}).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )
        gap_stats.rename(columns={"分组": "低开区间"}).to_csv(
            run_dir / "gap_bucket_stats.csv", index=False, encoding="utf-8-sig"
        )

        plot_created = write_plot(included, run_dir / "return_distribution.png")

        meta = {
            "generated_at": generated_at,
            "strategy": "uptrend_limit_down",
            "hit_table": "hit_uptrend_limit_down",
            "start": start,
            "end": end,
            "gap_range": list(args.gap),
            "rise_range": list(args.rise),
            "rise_days": args.rise_days,
            "open_exit_threshold": args.open_exit,
            "entry_gap_range": list(args.entry_gap),
            "hit_samples": len(samples),
            "passed_gap": int(samples["passed_gap"].sum()),
            "passed_rise": int(samples["passed_rise"].sum()),
            "included_samples": len(included),
            "open_exit_samples": int(included["exit_at_open"].sum()) if len(included) else 0,
            "intraday_exit_samples": (
                int(included["exit_intraday"].sum()) if len(included) else 0
            ),
            "close_exit_samples": (
                int((~included["exit_at_open"] & ~included["exit_intraday"]).sum())
                if len(included) else 0
            ),
            "sell_limit_down_samples": (
                int(included["sell_limit_down"].sum()) if len(included) else 0
            ),
            "sell_limit_down_missing_samples": (
                int(included["sell_down_limit_missing"].sum()) if len(included) else 0
            ),
            "missing_adj_factor_rows": missing_adj,
            "prices_rows": len(prices),
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        report = build_report(
            samples, included, stats, yearly, gap_stats, args,
            start, end, missing_adj, plot_created, generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    print("")
    print(f"最终纳入：{len(included)} 条 / 命中 {len(samples)} 条")
    print(f"平均收益率：{fmt_pct(stats['平均收益率'])}")
    print(f"中位数：{fmt_pct(stats['中位数'])}")
    print(f"胜率：{fmt_pct(stats['胜率'], signed=False)}")
    n_sell_limit_down = int(included["sell_limit_down"].sum()) if len(included) else 0
    n_sell_limit_down_missing = (
        int(included["sell_down_limit_missing"].sum()) if len(included) else 0
    )
    if len(included):
        print(
            f"备注：收益计算假设卖出日跌停也能卖出；最终样本中卖出日（T+2）收盘跌停 "
            f"{n_sell_limit_down} 条（{n_sell_limit_down / len(included):.2%}）"
        )
    else:
        print("备注：收益计算假设卖出日跌停也能卖出；最终样本 0 条，卖出日跌停 0 条")
    if n_sell_limit_down_missing:
        print(f"      其中 {n_sell_limit_down_missing} 条缺少卖出日跌停价，无法判断")
    if len(included):
        n_open = int(included["exit_at_open"].sum())
        n_intraday = int(included["exit_intraday"].sum())
        n_close = len(included) - n_open - n_intraday
        print(
            f"T+2 开盘卖出：{n_open} 条（{n_open / len(included):.2%}），"
            f"盘中止盈：{n_intraday} 条（{n_intraday / len(included):.2%}），"
            f"收盘卖出：{n_close} 条（{n_close / len(included):.2%}）"
        )
    if run_dir is not None:
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（默认关闭；加 --report 可生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
