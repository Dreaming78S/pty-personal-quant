"""统计 uptrend_limit_down 命中样本经筛选后的多日持有收益分布。

筛选条件（T 为命中日）：
1. T 日低开落在闭区间：DEFAULT_GAP[0] <= raw_open(T) / raw_pre_close(T) - 1 <= DEFAULT_GAP[1]
2. 近 N 个交易日涨幅（含 T 日）落在闭区间：
   DEFAULT_RISE[0] <= close(T) / close(T-N) - 1 <= DEFAULT_RISE[1]
3. T+1 买入低开落在闭区间：DEFAULT_ENTRY_GAP[0] <= raw_open(T+1) / raw_pre_close(T+1) - 1 <= DEFAULT_ENTRY_GAP[1]
   （raw_pre_close(T+1) 是交易所除权后的 T 日收盘，普通日等于 T 日收盘）

买卖规则（N = DEFAULT_MAX_HOLD_DAYS）：
- T+1 开盘买入；
- T+2 ~ T+N 为可卖窗口，T+N 是最迟卖出日；
- 每个可卖日先看开盘：开盘收益 >= 止盈或 <= -止损，按开盘价成交；
- 否则看盘中最高/最低：只触发一个就按该阈值成交；
- 同日同时触发止盈和止损时，用 Tushare 1 分钟行情判断谁先触发；
- 都没触发则继续持有，最迟 T+N 收盘卖出。

默认参数（以文件顶部常量为准）：
- DEFAULT_MAX_HOLD_DAYS：最多持有天数；
- DEFAULT_TAKE_PROFIT：止盈比例；
- DEFAULT_STOP_LOSS：止损比例，None 表示关闭；
- 每笔最多 5 万元、100 股整手，扣佣金/印花税/过户费/0.1% 双边滑点。

假设：
- T+1 开盘涨停也能买入；
- 跌停也能卖出；
- 不考虑停牌；窗口内任一日缺少行情则剔除该样本。

价格口径：
- 低开用不复权 raw_open / raw_pre_close；
- 近 N 日涨幅和买卖收益用后复权价；
- 分钟行情是不复权价，乘以当日 adj_factor 后与日线后复权价对齐。

分钟数据：
- 单次最多 8000 行；1 分钟单股票单日约 240 行，远低于上限；
- 接口上限 500 次/分钟、30 万次/天；脚本默认按 450 次/分钟限频，可用 --minute-rate-limit 调整；
- 同日双触发才按需拉取，结果缓存到 data_cache/stk_mins_1min/；
- 若接口返回更低限频（例如 1 次/分钟），默认不等待、不重试，直接按 --minute-fallback 降级；
  需要重试时用 --minute-retries 1 --minute-retry-wait 65。

用法：
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --report
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --start 2024-01-01 --end 2026-10-23 --report
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --gap -0.05 -0.02 --entry-gap -0.05 -0.02 --rise 0.40 0.80
    uv run python tmp_scripts/uptrend_limit_down_filtered_returns.py --max-hold-days 5 --take-profit 0.10 --stop-loss 0.08

DEFAULT_END = "latest" 表示自动使用命中表最新日期；DEFAULT_GENERATE_REPORT = False 表示默认只打印控制台，不生成报告文件。

样本来自 hit_uptrend_limit_down。若策略逻辑或参数有调整，先执行：
    uv run quant hits update -s uptrend_limit_down --from-date 2018-01-01
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from quant.data import db  # noqa: E402
from quant.data.cache import cache_dir  # noqa: E402
from quant.data.tushare_client import TushareClient  # noqa: E402

DEFAULT_START = "20260101"
DEFAULT_END = "20261231"
DEFAULT_GAP = (-0.11, 0.11)
DEFAULT_RISE = (-21, 24)
DEFAULT_RISE_DAYS = 10
DEFAULT_ENTRY_GAP = (-0.11, 0.11)
DEFAULT_MAX_HOLD_DAYS = 3
DEFAULT_TAKE_PROFIT = 0.03
DEFAULT_STOP_LOSS = 0.10   # None
DEFAULT_MINUTE_FALLBACK = "stop_first"
DEFAULT_POSITION = 50_000.0
# 文档上限 500 次/分钟，默认留余量；账户实际限频更低时用 --minute-rate-limit 调整。
DEFAULT_MINUTE_RATE_LIMIT = 450
# 限频后默认不等待、不重试，直接按 fallback 处理；需要重试时用命令行打开。
DEFAULT_MINUTE_RETRY_WAIT = 0.0
DEFAULT_MINUTE_RETRIES = 0

# 不用改
DEFAULT_GENERATE_REPORT = False
PRICE_CHUNK = 500
GAP_EPS = 1e-9
RET_EPS = 1e-6
LIMIT_EPS = 1e-6
MINUTE_FREQ = "1min"
COMMISSION = 0.00025
MIN_COMMISSION = 5.0
STAMP = 0.0005
TRANSFER = 0.00001
SLIPPAGE = 0.001
LOT = 100

CSV_COLUMN_NAMES: dict[str, str] = {
    "sample_id": "样本序号",
    "hit_date": "命中日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "cal_pos": "交易日序号",
    "prev_date": "前一交易日",
    "rise_ref_date": "涨幅基准日",
    "t1_date": "次日日期",
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
    "gap_open": "低开幅度",
    "gap_open_prev_raw": "低开幅度(对前一日原始收盘)",
    "ret_rise": "近N日涨幅",
    "passed_gap": "是否满足低开条件",
    "passed_rise": "是否满足涨幅条件",
    "passed_filters": "是否通过筛选",
    "included": "是否纳入统计",
    "base_exclusion_reason": "筛选未通过原因",
    "exclusion_reason": "未纳入原因",
    "gap_open_pct": "低开幅度(%)",
    "gap_open_prev_raw_pct": "低开幅度(对前一日原始收盘,%)",
    "entry_gap_pct": "次日开盘低开幅度(%)",
    "entry_gap_close_raw_pct": "次日开盘低开幅度(对T日收盘,%)",
    "ret_rise_pct": "近N日涨幅(%)",
    "year": "年份",
    "gap_bucket": "低开区间",
    "exit_date": "卖出日期",
    "exit_day_index": "卖出日序号(T+N)",
    "holding_days": "实际持有天数",
    "exit_type": "卖出方式",
    "exit_source": "卖出判断来源",
    "exit_minute_time": "分钟首触时间",
    "sell_limit_down": "卖出日是否跌停",
    "sell_down_limit_missing": "卖出日跌停价是否缺失",
    "sell_close_raw": "卖出日收盘价(不复权)",
    "sell_down_limit": "卖出日跌停价(不复权)",
    "down_limit": "跌停价(不复权)",
    "exit_price_raw": "卖出价(不复权)",
    "exit_price_adj": "卖出价(后复权)",
    "buy_price_raw": "买入价(不复权,含滑点)",
    "sell_price_raw": "卖出价(不复权,含滑点)",
    "shares": "股数",
    "buy_gross_raw": "买入金额(不复权)",
    "buy_fee": "买入费用",
    "sell_gross_raw": "卖出金额(不复权)",
    "sell_fee": "卖出费用",
    "net_pnl_raw": "净盈亏(不复权)",
    "capital_raw": "占用资金",
    "net_pnl_adj": "净盈亏(后复权口径,非现金)",
    "capital_adj": "占用资金(后复权口径,非现金)",
    "return_adj": "毛收益率(后复权)",
    "return_raw": "毛收益率(不复权)",
    "return_net": "净收益率(后复权,扣费+滑点)",
    "return_net_raw": "净收益率(不复权,扣费+滑点)",
    "return_pct": "毛收益率(%)",
    "return_raw_pct": "毛收益率(不复权,%)",
    "return_net_pct": "净收益率(%)",
    "return_net_raw_pct": "净收益率(不复权,%)",
    "exit_error": "卖出计算错误",
    "day_index": "持有日序号",
    "trade_date": "交易日",
    "open": "开盘价(不复权)",
    "high": "最高价(不复权)",
    "low": "最低价(不复权)",
    "close": "收盘价(不复权)",
    "adj_open": "开盘价(后复权)",
    "adj_high": "最高价(后复权)",
    "adj_low": "最低价(后复权)",
    "adj_close": "收盘价(后复权)",
    "adj_factor": "复权因子",
    "ret_open": "开盘收益",
    "ret_high": "最高收益",
    "ret_low": "最低收益",
    "ret_close": "收盘收益",
    "has_bar": "是否有完整日线",
    "hit_tp": "是否触及止盈",
    "hit_sl": "是否触及止损",
    "both_hit": "是否同日双触发",
    "minute_needed": "是否需要分钟数据",
    "minute_first": "分钟首触方向",
    "minute_time": "分钟首触时间",
    "minute_source": "分钟数据来源",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统计 uptrend_limit_down 二次筛选样本的 T+1 开盘买入、T+2~T+N 止盈止损收益分布"
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
    parser.add_argument("--entry-gap", nargs=2, type=float, default=DEFAULT_ENTRY_GAP,
                        metavar=("MIN", "MAX"),
                        help=f"T+1 买入低开闭区间（小数），默认 {DEFAULT_ENTRY_GAP}，要求 MIN <= gap <= MAX")
    parser.add_argument("--max-hold-days", type=int, default=DEFAULT_MAX_HOLD_DAYS,
                        help=f"最多持有天数 N（T+1 买入，T+2~T+N 可卖），必须 >= 2，默认 {DEFAULT_MAX_HOLD_DAYS}")
    parser.add_argument("--take-profit", type=float, default=DEFAULT_TAKE_PROFIT,
                        help=f"止盈比例（小数），默认 {DEFAULT_TAKE_PROFIT}；传 0 表示关闭止盈")
    stop_loss_default = "关闭" if DEFAULT_STOP_LOSS is None else str(DEFAULT_STOP_LOSS)
    parser.add_argument("--stop-loss", type=float, default=DEFAULT_STOP_LOSS,
                        help=f"止损比例（正数小数，例如 0.08 表示 -8%%，默认 {stop_loss_default}）；传 0 表示关闭")
    parser.add_argument("--minute-fallback", choices=("stop_first", "take_profit_first", "skip"),
                        default=DEFAULT_MINUTE_FALLBACK,
                        help="同日双触发但分钟数据缺失时的处理：stop_first 先止损（默认）、take_profit_first 先止盈、skip 剔除样本")
    parser.add_argument("--no-minute", action="store_true",
                        help="不请求分钟数据；同日双触发直接按 --minute-fallback 处理")
    parser.add_argument("--minute-cache-dir", default=None,
                        help="分钟数据缓存目录，默认 data_cache/stk_mins_1min")
    parser.add_argument("--max-minute-fetches", type=int, default=0,
                        help="单次运行最多请求多少次分钟数据；0 表示不限制（接口上限500次/分钟、30万次/天）")
    parser.add_argument("--minute-rate-limit", type=int, default=DEFAULT_MINUTE_RATE_LIMIT,
                        help=f"分钟接口每分钟请求上限；文档上限500，默认 {DEFAULT_MINUTE_RATE_LIMIT} 留余量；账户实际限频更低时按实际填")
    parser.add_argument("--minute-retries", type=int, default=DEFAULT_MINUTE_RETRIES,
                        help=f"分钟接口限频后的重试次数，默认 {DEFAULT_MINUTE_RETRIES}（不重试，直接降级）")
    parser.add_argument("--minute-retry-wait", type=float, default=DEFAULT_MINUTE_RETRY_WAIT,
                        help=f"分钟接口限频后的等待秒数，默认 {DEFAULT_MINUTE_RETRY_WAIT:g}（不等待）；例如设 65 会等待后重试")
    parser.add_argument("--position", type=float, default=DEFAULT_POSITION,
                        help=f"每笔最大资金（元），默认 {DEFAULT_POSITION:,.0f}")
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
    for col in df.columns:
        if col in names:
            continue
        if col.startswith("t") and col.endswith("_date") and col[1:-5].isdigit():
            names[col] = f"T+{col[1:-5]}日日期"
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
    df = df.reset_index(drop=True)
    df["sample_id"] = np.arange(len(df), dtype=int)
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
                 rise_days: int, max_hold_days: int) -> pd.DataFrame:
    date_pos = {d: i for i, d in enumerate(calendar)}
    out = hits.copy()
    out["cal_pos"] = out["hit_date"].map(date_pos)

    offsets = [
        (-1, "prev_date"),
        (-rise_days, "rise_ref_date"),
        (1, "t1_date"),
    ]
    offsets.extend((d, f"t{d}_date") for d in range(2, max_hold_days + 1))

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
                SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close,
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
    for col in ("open", "high", "low", "close", "pre_close", "adj_factor", "down_limit"):
        prices[col] = pd.to_numeric(prices[col], errors="coerce")
    prices["adj_open"] = prices["open"] * prices["adj_factor"]
    prices["adj_high"] = prices["high"] * prices["adj_factor"]
    prices["adj_low"] = prices["low"] * prices["adj_factor"]
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


def build_paths(samples: pd.DataFrame, prices: pd.DataFrame,
                max_hold_days: int) -> pd.DataFrame:
    date_cols = [f"t{d}_date" for d in range(2, max_hold_days + 1)]
    if not date_cols:
        return pd.DataFrame()
    paths = samples[["sample_id", "ts_code", *date_cols]].melt(
        id_vars=["sample_id", "ts_code"],
        value_vars=date_cols,
        var_name="date_col",
        value_name="trade_date",
    )
    paths["day_index"] = (
        paths["date_col"].str.extract(r"t(\d+)_date")[0].astype(int)
    )
    paths = paths.drop(columns=["date_col"])
    paths = paths.dropna(subset=["trade_date"])
    paths = paths.merge(
        prices[
            [
                "ts_code", "trade_date", "open", "high", "low", "close",
                "down_limit", "adj_open", "adj_high", "adj_low", "adj_close",
                "adj_factor",
            ]
        ],
        on=["ts_code", "trade_date"],
        how="left",
    )
    paths["trade_date"] = paths["trade_date"].astype(str)
    return paths.sort_values(["sample_id", "day_index"]).reset_index(drop=True)


def attach_path_returns(samples: pd.DataFrame, paths: pd.DataFrame) -> pd.DataFrame:
    if paths.empty:
        return paths
    entry = samples.set_index("sample_id")["t1_adj_open"]
    mapped = paths["sample_id"].map(entry)
    for src, dst in (
        ("adj_open", "ret_open"),
        ("adj_high", "ret_high"),
        ("adj_low", "ret_low"),
        ("adj_close", "ret_close"),
    ):
        paths[dst] = paths[src] / mapped - 1.0
    paths["has_bar"] = paths[
        ["adj_open", "adj_high", "adj_low", "adj_close"]
    ].notna().all(axis=1)
    return paths


def attach_touch_flags(paths: pd.DataFrame, take_profit: float | None,
                       stop_loss: float | None) -> pd.DataFrame:
    if paths.empty:
        return paths
    paths["hit_tp"] = False
    paths["hit_sl"] = False
    if take_profit is not None:
        paths["hit_tp"] = paths["ret_high"].ge(take_profit - RET_EPS)
    if stop_loss is not None:
        paths["hit_sl"] = paths["ret_low"].le(-stop_loss + RET_EPS)
    paths["both_hit"] = paths["hit_tp"] & paths["hit_sl"]
    return paths


def build_samples(hits: pd.DataFrame, prices: pd.DataFrame,
                  gap_range: tuple[float, float], rise_range: tuple[float, float],
                  rise_days: int, entry_gap_range: tuple[float, float],
                  max_hold_days: int, take_profit: float | None,
                  stop_loss: float | None) -> tuple[pd.DataFrame, pd.DataFrame]:
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
    samples["passed_filters"] = (
        samples["passed_gap"]
        & samples["passed_rise"]
        & samples["passed_entry_gap"]
    )

    for src, dst in (
        ("gap_open", "gap_open_pct"),
        ("gap_open_prev_raw", "gap_open_prev_raw_pct"),
        ("entry_gap", "entry_gap_pct"),
        ("entry_gap_close_raw", "entry_gap_close_raw_pct"),
        ("ret_rise", "ret_rise_pct"),
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
    samples["base_exclusion_reason"] = samples.apply(
        lambda row: _base_exclusion_reason(row, rise_days, max_hold_days), axis=1
    )

    paths = build_paths(samples, prices, max_hold_days)
    paths = attach_path_returns(samples, paths)
    paths = attach_touch_flags(paths, take_profit, stop_loss)
    return samples, paths


def _base_exclusion_reason(row: pd.Series, rise_days: int,
                           max_hold_days: int) -> str:
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
    if pd.isna(row["t1_date"]):
        reasons.append("缺少T+1交易日")
    if pd.isna(row[f"t{max_hold_days}_date"]):
        reasons.append(f"缺少T+{max_hold_days}交易日")
    return "；".join(dict.fromkeys(reasons))


def first_touch_from_minutes(
    bars: pd.DataFrame,
    entry_adj: float,
    take_profit: float | None,
    stop_loss: float | None,
    adj_factor: float,
) -> tuple[str | None, str | None]:
    """返回 (首触方向, 时间)。方向为 'tp' 或 'sl'；无法判断返回 (None, None)。

    分钟行情是不复权价，乘以当日 adj_factor 后与后复权日线对齐。
    若同一根 1 分钟 K 线同时触及止盈和止损：
    - 阳线（close >= open）按 open -> low -> high -> close，先止损；
    - 阴线（close < open）按 open -> high -> low -> close，先止盈。
    这是 1 分钟数据下的路径假设；更精细需要逐笔成交。
    """
    if bars is None or bars.empty:
        return None, None
    if (
        take_profit is None and stop_loss is None
    ) or not np.isfinite(entry_adj) or entry_adj <= 0:
        return None, None
    if not np.isfinite(adj_factor) or adj_factor <= 0:
        return None, None

    if "trade_time" in bars.columns:
        bars = bars.sort_values("trade_time")

    for row in bars.itertuples(index=False):
        try:
            o = float(row.open) * adj_factor
            h = float(row.high) * adj_factor
            low = float(row.low) * adj_factor
            c = float(row.close) * adj_factor
        except (TypeError, ValueError):
            continue
        if not all(np.isfinite(v) for v in (o, h, low, c)):
            continue
        ret_open = o / entry_adj - 1.0
        ret_high = h / entry_adj - 1.0
        ret_low = low / entry_adj - 1.0
        ret_close = c / entry_adj - 1.0
        minute_time = str(row.trade_time)

        # 1 分钟 bar 开盘已经跳过阈值。
        if take_profit is not None and ret_open >= take_profit - RET_EPS:
            return "tp", minute_time
        if stop_loss is not None and ret_open <= -stop_loss + RET_EPS:
            return "sl", minute_time

        hit_tp = take_profit is not None and ret_high >= take_profit - RET_EPS
        hit_sl = stop_loss is not None and ret_low <= -stop_loss + RET_EPS
        if hit_tp and hit_sl:
            # 同一根 bar 内双触发，用阴阳线判断大概路径。
            return ("sl" if ret_close >= ret_open else "tp"), minute_time
        if hit_tp:
            return "tp", minute_time
        if hit_sl:
            return "sl", minute_time
    return None, None


class MinuteLoader:
    """按 (ts_code, trade_date) 懒加载 1 分钟行情，并缓存到本地 parquet。"""

    def __init__(self, cache_root: Path, enabled: bool = True,
                 max_errors: int = 3, max_fetches: int = 0,
                 rate_limit: int = DEFAULT_MINUTE_RATE_LIMIT,
                 retries: int = DEFAULT_MINUTE_RETRIES,
                 retry_wait: float = DEFAULT_MINUTE_RETRY_WAIT):
        self.cache_root = cache_root
        self.enabled = enabled
        self.max_errors = max_errors
        self.max_fetches = max_fetches
        self.rate_limit = max(1, int(rate_limit))
        self.retries = max(0, int(retries))
        self.retry_wait = max(0.0, float(retry_wait))
        self._client: TushareClient | None = None
        self._memory: dict[tuple[str, str], pd.DataFrame] = {}
        self._errors = 0
        self._disabled = False
        self.fetched = 0
        self.cache_hits = 0
        self.failed = 0
        self.capped = 0
        self.rate_limited = False

    def _get_client(self) -> TushareClient:
        if self._client is None:
            # 文档上限 500 次/分钟；默认 450 留余量，可通过 --minute-rate-limit 调整。
            self._client = TushareClient(
                max_retries=2,
                api_rate_limits={"stk_mins": self.rate_limit},
            )
        return self._client

    def load(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        key = (ts_code, trade_date)
        if key in self._memory:
            return self._memory[key]

        path = self.cache_root / f"{trade_date}_{ts_code}.parquet"
        if path.exists():
            try:
                bars = pd.read_parquet(path)
                self.cache_hits += 1
                self._memory[key] = bars
                return bars
            except Exception:  # noqa: BLE001 - 缓存损坏时重新拉取
                pass

        if not self.enabled or self._disabled:
            self._memory[key] = pd.DataFrame()
            return self._memory[key]

        if self.max_fetches > 0 and self.fetched >= self.max_fetches:
            self.capped += 1
            self._memory[key] = pd.DataFrame()
            return self._memory[key]

        start = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 09:00:00"
        end = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]} 19:00:00"
        bars: pd.DataFrame | None = None
        last_exc: Exception | None = None
        rate_limit_hit = False
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                bars = self._get_client().call(
                    "stk_mins", ts_code=ts_code, freq=MINUTE_FREQ,
                    start_date=start, end_date=end,
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - 分钟权限/网络失败时降级
                last_exc = exc
                message = str(exc)
                if "频率" in message or "超限" in message or "每分钟" in message:
                    rate_limit_hit = True
                    match = re.search(r"(\d+)\s*次/分钟", message)
                    actual = int(match.group(1)) if match else None
                    if actual is not None and actual < self.rate_limit:
                        self.rate_limit = max(1, actual)
                        self._client = None  # 用新的限频重建客户端
                        print(
                            f"[分钟] 接口实际限频 {self.rate_limit} 次/分钟，"
                            "已自动调整"
                        )
                    if attempt < attempts - 1 and self.retry_wait > 0:
                        print(
                            f"[分钟] 接口限频，等待 {self.retry_wait:.0f} 秒后重试 ..."
                        )
                        time.sleep(self.retry_wait)
                        continue
                    self.rate_limited = True
                    self._disabled = True
                    print(
                        "[分钟] 接口限频，本次运行后续不再请求分钟数据，"
                        "同日双触发按 --minute-fallback 处理。"
                    )
                    if self.retries > 0 and self.retry_wait > 0:
                        print("如需继续重试，可调大 --minute-retries / --minute-retry-wait。")
                    else:
                        print(
                            "如需重试，加 --minute-retries 1 --minute-retry-wait 65；"
                            "或确认 Tushare 的 A股历史分钟权限。"
                        )
                    break
                break

        if last_exc is not None:
            self._errors += 1
            self.failed += 1
            if rate_limit_hit:
                self._disabled = True
            elif self._errors >= self.max_errors:
                self._disabled = True
            if self._errors == 1 and not rate_limit_hit:
                print(f"[分钟] 获取失败，将按 --minute-fallback 处理：{last_exc}")
            self._memory[key] = pd.DataFrame()
            return self._memory[key]

        self.fetched += 1
        if self.fetched == 1 or self.rate_limit <= 20 or self.fetched % 25 == 0:
            print(
                f"[分钟] 已拉取 {self.fetched} 条（{ts_code} {trade_date}）",
                flush=True,
            )
        if bars is None or bars.empty:
            self._errors += 1
            self.failed += 1
            if self._errors >= self.max_errors:
                self._disabled = True
            if self._errors == 1:
                print(
                    f"[分钟] {ts_code} {trade_date} 返回空数据，"
                    "将按 --minute-fallback 处理"
                )
            self._memory[key] = pd.DataFrame()
            return self._memory[key]
        bars = bars.copy()
        bars["trade_time"] = pd.to_datetime(bars["trade_time"])
        bars = bars.sort_values("trade_time").reset_index(drop=True)
        for col in ("open", "high", "low", "close"):
            bars[col] = pd.to_numeric(bars[col], errors="coerce")
        path.parent.mkdir(parents=True, exist_ok=True)
        bars.to_parquet(path, index=False)
        self._memory[key] = bars
        return bars


def _empty_exit_record(sample_id: int) -> dict[str, object]:
    return {
        "sample_id": int(sample_id),
        "exit_date": None,
        "exit_day_index": np.nan,
        "holding_days": np.nan,
        "exit_type": None,
        "exit_source": None,
        "exit_minute_time": None,
        "exit_price_raw": np.nan,
        "exit_price_adj": np.nan,
        "buy_price_raw": np.nan,
        "sell_price_raw": np.nan,
        "shares": np.nan,
        "buy_gross_raw": np.nan,
        "buy_fee": np.nan,
        "sell_gross_raw": np.nan,
        "sell_fee": np.nan,
        "net_pnl_raw": np.nan,
        "capital_raw": np.nan,
        "net_pnl_adj": np.nan,
        "capital_adj": np.nan,
        "return_adj": np.nan,
        "return_raw": np.nan,
        "return_net": np.nan,
        "return_net_raw": np.nan,
        "exit_error": "",
    }


def resolve_exits(
    samples: pd.DataFrame,
    paths: pd.DataFrame,
    max_hold_days: int,
    take_profit: float | None,
    stop_loss: float | None,
    minute_loader: MinuteLoader,
    minute_fallback: str,
    position: float,
) -> tuple[pd.DataFrame, dict[str, int]]:
    path_groups = {
        int(sid): group.sort_values("day_index")
        for sid, group in paths.groupby("sample_id", sort=False)
    }
    records: list[dict[str, object]] = []
    stats = {"needed": 0, "used": 0, "failed": 0}

    for row in samples.itertuples(index=False):
        sid = int(row.sample_id)
        rec = _empty_exit_record(sid)

        if not bool(row.passed_filters):
            records.append(rec)
            continue

        entry_adj = row.t1_adj_open
        entry_raw = row.t1_open_raw
        if (
            pd.isna(entry_adj) or not np.isfinite(entry_adj) or entry_adj <= 0
            or pd.isna(entry_raw) or not np.isfinite(entry_raw) or entry_raw <= 0
        ):
            rec["exit_error"] = "缺少T+1开盘价"
            records.append(rec)
            continue

        group = path_groups.get(sid)
        if group is None or group.empty:
            rec["exit_error"] = f"缺少T+2~T+{max_hold_days}交易日"
            records.append(rec)
            continue
        if len(group) < max_hold_days - 1:
            rec["exit_error"] = f"缺少T+2~T+{max_hold_days}交易日"
            records.append(rec)
            continue
        missing = group[~group["has_bar"]]
        if not missing.empty:
            bad_day = int(missing.iloc[0]["day_index"])
            rec["exit_error"] = f"缺少T+{bad_day}行情（停牌/未产生）"
            records.append(rec)
            continue

        exit_type: str | None = None
        exit_source: str | None = None
        exit_minute_time: str | None = None
        exit_adj: float | None = None
        exit_raw: float | None = None
        exit_day_index: int | None = None
        exit_date: str | None = None
        exit_error = ""

        for day in group.itertuples(index=False):
            ret_open = float(day.ret_open)

            # 1) 开盘跳空越过阈值，按开盘价成交。
            if take_profit is not None and ret_open >= take_profit - RET_EPS:
                exit_type = "开盘止盈"
                exit_source = "日线"
                exit_adj = float(day.adj_open)
                exit_raw = float(day.open)
                exit_day_index = int(day.day_index)
                exit_date = str(day.trade_date)
                break
            if stop_loss is not None and ret_open <= -stop_loss + RET_EPS:
                exit_type = "开盘止损"
                exit_source = "日线"
                exit_adj = float(day.adj_open)
                exit_raw = float(day.open)
                exit_day_index = int(day.day_index)
                exit_date = str(day.trade_date)
                break

            # 2) 盘中触及阈值。
            hit_tp = (
                take_profit is not None
                and float(day.ret_high) >= take_profit - RET_EPS
            )
            hit_sl = (
                stop_loss is not None
                and float(day.ret_low) <= -stop_loss + RET_EPS
            )
            if hit_tp and hit_sl:
                stats["needed"] += 1
                bars = minute_loader.load(str(row.ts_code), str(day.trade_date))
                first, minute_time = first_touch_from_minutes(
                    bars, float(entry_adj), take_profit, stop_loss,
                    float(day.adj_factor) if pd.notna(day.adj_factor) else np.nan,
                )
                if first == "tp":
                    stats["used"] += 1
                    exit_type = "盘中止盈"
                    exit_source = "分钟"
                    exit_adj = float(entry_adj) * (1.0 + take_profit)
                    exit_raw = _adj_to_raw(exit_adj, day.adj_factor)
                    exit_minute_time = minute_time
                elif first == "sl":
                    stats["used"] += 1
                    exit_type = "盘中止损"
                    exit_source = "分钟"
                    exit_adj = float(entry_adj) * (1.0 - stop_loss)
                    exit_raw = _adj_to_raw(exit_adj, day.adj_factor)
                    exit_minute_time = minute_time
                elif minute_fallback == "skip":
                    exit_error = "同日双触发且无分钟数据"
                    break
                else:
                    stats["failed"] += 1
                    first = "tp" if minute_fallback == "take_profit_first" else "sl"
                    exit_source = (
                        "分钟缺失-乐观" if first == "tp" else "分钟缺失-保守"
                    )
                    if first == "tp":
                        exit_type = "盘中止盈"
                        exit_adj = float(entry_adj) * (1.0 + take_profit)
                        exit_raw = _adj_to_raw(exit_adj, day.adj_factor)
                    else:
                        exit_type = "盘中止损"
                        exit_adj = float(entry_adj) * (1.0 - stop_loss)
                        exit_raw = _adj_to_raw(exit_adj, day.adj_factor)
                if exit_type is not None:
                    exit_day_index = int(day.day_index)
                    exit_date = str(day.trade_date)
                break
            if hit_tp:
                exit_type = "盘中止盈"
                exit_source = "日线"
                exit_adj = float(entry_adj) * (1.0 + take_profit)
                exit_raw = _adj_to_raw(exit_adj, day.adj_factor)
                exit_day_index = int(day.day_index)
                exit_date = str(day.trade_date)
                break
            if hit_sl:
                exit_type = "盘中止损"
                exit_source = "日线"
                exit_adj = float(entry_adj) * (1.0 - stop_loss)
                exit_raw = _adj_to_raw(exit_adj, day.adj_factor)
                exit_day_index = int(day.day_index)
                exit_date = str(day.trade_date)
                break

        if exit_error:
            rec["exit_error"] = exit_error
            records.append(rec)
            continue

        # 3) 都没触发，最迟 T+N 收盘卖出。
        if exit_type is None:
            last = group.iloc[-1]
            exit_type = f"T+{max_hold_days} 收盘"
            exit_source = "日线"
            exit_adj = float(last["adj_close"])
            exit_raw = float(last["close"])
            exit_day_index = int(last["day_index"])
            exit_date = str(last["trade_date"])

        if exit_adj is None or exit_raw is None or not np.isfinite(exit_adj) or not np.isfinite(exit_raw):
            rec["exit_error"] = "卖出价缺失"
            records.append(rec)
            continue

        # 阈值卖出需要把后复权价换回不复权价，才能按真实价格计算费用。
        if (
            exit_type in ("盘中止盈", "盘中止损")
            and (pd.isna(exit_raw) or not np.isfinite(exit_raw))
        ):
            rec["exit_error"] = "缺少复权因子，无法换算止盈止损价"
            records.append(rec)
            continue

        buy_price_raw = float(entry_raw) * (1.0 + SLIPPAGE)
        shares = int(math.floor(position / buy_price_raw / LOT) * LOT)
        if shares < LOT:
            rec["exit_error"] = f"每笔资金不足{LOT}股"
            records.append(rec)
            continue

        sell_price_raw = float(exit_raw) * (1.0 - SLIPPAGE)
        buy_gross_raw = shares * buy_price_raw
        buy_fee = (
            max(buy_gross_raw * COMMISSION, MIN_COMMISSION)
            + buy_gross_raw * TRANSFER
        )
        sell_gross_raw = shares * sell_price_raw
        sell_fee = (
            max(sell_gross_raw * COMMISSION, MIN_COMMISSION)
            + sell_gross_raw * STAMP
            + sell_gross_raw * TRANSFER
        )
        net_pnl_raw = sell_gross_raw - sell_fee - buy_gross_raw - buy_fee
        capital_raw = buy_gross_raw + buy_fee
        return_net_raw = net_pnl_raw / capital_raw

        buy_gross_adj = shares * float(entry_adj) * (1.0 + SLIPPAGE)
        sell_gross_adj = shares * float(exit_adj) * (1.0 - SLIPPAGE)
        entry_adj_factor = float(entry_adj) / float(entry_raw)
        buy_fee_adj = buy_fee * entry_adj_factor
        sell_fee_adj = sell_fee * entry_adj_factor
        net_pnl_adj = sell_gross_adj - buy_gross_adj - buy_fee_adj - sell_fee_adj
        capital_adj = buy_gross_adj + buy_fee_adj
        return_net = net_pnl_adj / capital_adj

        rec.update({
            "exit_date": exit_date,
            "exit_day_index": exit_day_index,
            "holding_days": exit_day_index,
            "exit_type": exit_type,
            "exit_source": exit_source,
            "exit_minute_time": exit_minute_time,
            "exit_price_raw": float(exit_raw),
            "exit_price_adj": float(exit_adj),
            "buy_price_raw": buy_price_raw,
            "sell_price_raw": sell_price_raw,
            "shares": shares,
            "buy_gross_raw": buy_gross_raw,
            "buy_fee": buy_fee,
            "sell_gross_raw": sell_gross_raw,
            "sell_fee": sell_fee,
            "net_pnl_raw": net_pnl_raw,
            "capital_raw": capital_raw,
            "net_pnl_adj": net_pnl_adj,
            "capital_adj": capital_adj,
            "return_adj": float(exit_adj) / float(entry_adj) - 1.0,
            "return_raw": float(exit_raw) / float(entry_raw) - 1.0,
            "return_net": return_net,
            "return_net_raw": return_net_raw,
        })
        records.append(rec)

    exit_df = pd.DataFrame(records)
    samples = samples.merge(exit_df, on="sample_id", how="left")

    # 卖出日（实际 exit_date）收盘是否跌停；只做统计备注，不改变收益计算。
    if not paths.empty:
        exit_ref = paths[
            ["sample_id", "day_index", "close", "down_limit"]
        ].rename(columns={
            "day_index": "exit_day_index",
            "close": "sell_close_raw",
            "down_limit": "sell_down_limit",
        })
        samples = samples.merge(
            exit_ref, on=["sample_id", "exit_day_index"], how="left"
        )
    else:
        samples["sell_close_raw"] = np.nan
        samples["sell_down_limit"] = np.nan
    samples["sell_down_limit_missing"] = (
        samples["sell_down_limit"].isna() | samples["sell_down_limit"].le(0)
    )
    samples["sell_limit_down"] = (
        samples["sell_down_limit"].gt(0)
        & samples["sell_close_raw"].le(samples["sell_down_limit"] + LIMIT_EPS)
    ).fillna(False)

    samples["included"] = (
        samples["passed_filters"]
        & samples["return_adj"].notna()
        & samples["return_net"].notna()
        & samples["return_net_raw"].notna()
    )
    samples["exclusion_reason"] = samples["base_exclusion_reason"].fillna("")
    mask = samples["exit_error"].fillna("").ne("")
    samples.loc[mask, "exclusion_reason"] = samples.loc[mask].apply(
        lambda r: "；".join(dict.fromkeys(filter(None, [
            str(r["base_exclusion_reason"] or ""),
            str(r["exit_error"] or ""),
        ]))),
        axis=1,
    )
    samples.loc[samples["included"], "exclusion_reason"] = ""

    for src, dst in (
        ("return_adj", "return_pct"),
        ("return_raw", "return_raw_pct"),
        ("return_net", "return_net_pct"),
        ("return_net_raw", "return_net_raw_pct"),
    ):
        samples[dst] = samples[src] * 100.0
    return samples, stats


def _adj_to_raw(adj_price: float, adj_factor: float) -> float:
    if pd.isna(adj_factor) or not np.isfinite(adj_factor) or adj_factor <= 0:
        return np.nan
    return float(adj_price) / float(adj_factor)


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


def grouped_stats(df: pd.DataFrame, group_col: str,
                  return_col: str = "return_net") -> pd.DataFrame:
    rows = []
    for key, group in df.groupby(group_col, sort=True, observed=True):
        rets = pd.to_numeric(group[return_col], errors="coerce").dropna()
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


def exit_stats_table(included: pd.DataFrame, max_hold_days: int) -> pd.DataFrame:
    rows = []
    order = [
        "开盘止盈", "开盘止损", "盘中止盈", "盘中止损",
        *[f"T+{d} 收盘" for d in range(2, max_hold_days + 1)],
    ]
    known = set(order)
    extra = [x for x in included["exit_type"].dropna().unique() if x not in known]
    for exit_type in [*order, *sorted(extra)]:
        group = included[included["exit_type"] == exit_type]
        rets = pd.to_numeric(group["return_net"], errors="coerce").dropna()
        if rets.empty:
            continue
        rows.append({
            "卖出方式": exit_type,
            "样本数": len(rets),
            "占比": len(rets) / len(included) if len(included) else None,
            "平均净收益": float(rets.mean()),
            "中位数": float(rets.median()),
            "胜率": float((rets > 0).mean()),
            "平均持有天数": float(group["holding_days"].mean()),
        })
    return pd.DataFrame(rows)


def holding_stats_table(included: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for days, group in included.groupby("holding_days", sort=True):
        rets = pd.to_numeric(group["return_net"], errors="coerce").dropna()
        if rets.empty:
            continue
        rows.append({
            "持有天数": int(days),
            "样本数": len(rets),
            "平均净收益": float(rets.mean()),
            "中位数": float(rets.median()),
            "胜率": float((rets > 0).mean()),
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


def fmt_optional_pct(value: float | None) -> str:
    return "关闭" if value is None else fmt_pct(value, signed=False)


def hold_window_text(max_hold_days: int) -> str:
    return "T+2" if max_hold_days == 2 else f"T+2 ~ T+{max_hold_days}"


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

    rets = included["return_net"] * 100.0
    years = sorted(included["year"].unique())
    data = [included.loc[included["year"] == y, "return_net"] * 100.0 for y in years]

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].hist(rets, bins=30, color="#4C78A8", edgecolor="white")
    axes[0].axvline(0, color="black", linewidth=1)
    axes[0].axvline(rets.mean(), color="#E45756", linewidth=1.5,
                    label=f"Mean {rets.mean():.2f}%")
    axes[0].axvline(rets.median(), color="#F58518", linewidth=1.5,
                    linestyle="--", label=f"Median {rets.median():.2f}%")
    axes[0].set_title("Net return distribution")
    axes[0].set_xlabel("Net return (%)")
    axes[0].set_ylabel("Samples")
    axes[0].legend()

    box = axes[1].boxplot(data, showmeans=True, patch_artist=True)
    for patch in box["boxes"]:
        patch.set_facecolor("#A5C8E1")
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set_xticks(range(1, len(years) + 1))
    axes[1].set_xticklabels(years)
    axes[1].set_title("Net return by hit year")
    axes[1].set_xlabel("Year")
    axes[1].set_ylabel("Net return (%)")

    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def build_report(
    samples: pd.DataFrame,
    included: pd.DataFrame,
    stats: dict[str, float | int | None],
    net_stats: dict[str, float | int | None],
    yearly: pd.DataFrame,
    gap_stats: pd.DataFrame,
    exit_stats: pd.DataFrame,
    holding_stats: pd.DataFrame,
    minute_stats: dict[str, int],
    args: argparse.Namespace,
    start: str,
    end: str,
    missing_adj: int,
    plot_created: bool,
    generated_at: str,
) -> str:
    total = len(samples)
    gap_ok = int(samples["passed_gap"].sum())
    rise_ok = int(samples["passed_rise"].sum())
    both_ok = int((samples["passed_gap"] & samples["passed_rise"]).sum())
    entry_ok = int(samples["passed_entry_gap"].sum())
    all_ok = int(samples["passed_filters"].sum())
    n_included = int(samples["included"].sum())
    n_sell_limit_down = int(included["sell_limit_down"].sum()) if n_included else 0
    n_sell_limit_down_missing = (
        int(included["sell_down_limit_missing"].sum()) if n_included else 0
    )
    missing_exit = int((samples["passed_filters"] & samples["return_adj"].isna()).sum())
    missing_rise = int(samples["ret_rise"].isna().sum())
    missing_gap = int(samples["gap_open"].isna().sum())
    missing_entry = int(samples["entry_gap"].isna().sum())
    missing_future = int(samples[f"t{args.max_hold_days}_date"].isna().sum())

    funnel = [
        {"阶段": "命中样本总数", "样本数": total, "说明": "hit_uptrend_limit_down"},
        {"阶段": f"T日低开 ∈ {fmt_range(args.gap)}", "样本数": gap_ok, "说明": "raw_open / raw_pre_close - 1"},
        {"阶段": f"近{args.rise_days}日涨幅 ∈ {fmt_range(args.rise)}", "样本数": rise_ok, "说明": "后复权收盘价"},
        {"阶段": "同时满足低开+涨幅", "样本数": both_ok, "说明": "尚未要求T+1买入低开"},
        {"阶段": f"T+1开盘 ∈ {fmt_range(args.entry_gap)}", "样本数": entry_ok, "说明": "raw_open(T+1)/raw_pre_close(T+1)-1"},
        {"阶段": "三项同时满足", "样本数": all_ok, "说明": "尚未要求T+1~T+N行情完整"},
        {"阶段": "最终纳入收益统计", "样本数": n_included, "说明": "且卖出窗口行情完整、可计算费用"},
        {"阶段": "三项满足但卖出窗口数据不足", "样本数": missing_exit, "说明": "停牌、行情未产生或资金不足100股"},
    ]
    coverage = [
        {"项目": "缺T日开盘/昨收", "样本数": missing_gap, "说明": "无法计算低开"},
        {"项目": f"缺T-{args.rise_days}行情", "样本数": missing_rise, "说明": "无法计算近N日涨幅"},
        {"项目": "缺T+1开盘/昨收", "样本数": missing_entry, "说明": "无法计算T+1低开"},
        {"项目": f"缺T+{args.max_hold_days}交易日", "样本数": missing_future, "说明": "T+N超出交易日历"},
        {"项目": "同日双触发", "样本数": minute_stats["needed"], "说明": "需要1分钟行情判断先后"},
        {"项目": "分钟数据成功判断", "样本数": minute_stats["used"], "说明": "按分钟首触方向成交"},
        {"项目": "分钟数据缺失", "样本数": minute_stats["failed"], "说明": f"按 {args.minute_fallback} 降级"},
        {"项目": "分钟请求限额跳过", "样本数": minute_stats.get("capped", 0), "说明": "达到 --max-minute-fetches 后降级"},
    ]

    def summary_rows_for(values: dict[str, float | int | None]) -> list[dict[str, str]]:
        ci = (
            f"[{fmt_pct(values['均值95%置信区间下限'])}, "
            f"{fmt_pct(values['均值95%置信区间上限'])}]"
        )
        return [
            {"指标": "样本数", "数值": str(values["样本数"])},
            {"指标": "平均收益率", "数值": fmt_pct(values["平均收益率"])},
            {"指标": "中位数", "数值": fmt_pct(values["中位数"])},
            {"指标": "标准差", "数值": fmt_pct(values["标准差"], signed=False)},
            {"指标": "胜率", "数值": fmt_pct(values["胜率"], signed=False)},
            {"指标": "平均盈利", "数值": fmt_pct(values["平均盈利"])},
            {"指标": "平均亏损", "数值": fmt_pct(values["平均亏损"])},
            {"指标": "盈亏比", "数值": fmt_num(values["盈亏比"])},
            {"指标": "最小收益率", "数值": fmt_pct(values["最小收益率"])},
            {"指标": "最大收益率", "数值": fmt_pct(values["最大收益率"])},
            {"指标": "均值95%置信区间", "数值": ci},
        ]

    gross_rows = summary_rows_for(stats)
    net_rows = summary_rows_for(net_stats)

    quantile_rows = [
        {
            "分位": f"P{int(q * 100):02d}",
            "毛收益": fmt_pct(stats.get(f"P{int(q * 100):02d}")),
            "净收益": fmt_pct(net_stats.get(f"P{int(q * 100):02d}")),
        }
        for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
    ]

    yearly_rows = []
    for row in yearly.itertuples(index=False):
        yearly_rows.append({
            "年份": row.分组,
            "样本数": row.样本数,
            "平均净收益": fmt_pct(row.平均收益率),
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
            "平均净收益": fmt_pct(row.平均收益率),
            "中位数": fmt_pct(row.中位数),
            "胜率": fmt_pct(row.胜率, signed=False),
            "最小": fmt_pct(row.最小收益率),
            "最大": fmt_pct(row.最大收益率),
        })

    exit_rows = []
    for row in exit_stats.itertuples(index=False):
        exit_rows.append({
            "卖出方式": row.卖出方式,
            "样本数": row.样本数,
            "占比": fmt_pct(row.占比, signed=False),
            "平均净收益": fmt_pct(row.平均净收益),
            "中位数": fmt_pct(row.中位数),
            "胜率": fmt_pct(row.胜率, signed=False),
            "平均持有天数": f"{row.平均持有天数:.2f}",
        })

    source_rows = []
    if n_included:
        for source, group in included.groupby("exit_source", sort=True):
            rets = pd.to_numeric(group["return_net"], errors="coerce").dropna()
            source_rows.append({
                "来源": str(source),
                "样本数": len(rets),
                "占比": fmt_pct(len(rets) / n_included, signed=False),
                "平均净收益": fmt_pct(rets.mean()),
            })

    holding_rows = []
    for row in holding_stats.itertuples(index=False):
        holding_rows.append({
            "持有天数": row.持有天数,
            "样本数": row.样本数,
            "平均净收益": fmt_pct(row.平均净收益),
            "中位数": fmt_pct(row.中位数),
            "胜率": fmt_pct(row.胜率, signed=False),
        })

    def sample_rows(df: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for row in df.itertuples(index=False):
            minute = "-" if pd.isna(row.exit_minute_time) else str(row.exit_minute_time)
            rows.append({
                "命中日": row.hit_date,
                "代码": row.ts_code,
                "名称": row.name,
                "低开": fmt_pct(row.gap_open),
                f"{args.rise_days}日涨幅": fmt_pct(row.ret_rise),
                "次日低开": fmt_pct(row.entry_gap),
                "卖出日": row.exit_date,
                "持有": f"{int(row.holding_days)}日",
                "卖出方式": row.exit_type,
                "净收益率": fmt_pct(row.return_net),
                "分钟首触": minute,
            })
        return rows

    top = included.nlargest(5, "return_net") if not included.empty else included
    bottom = included.nsmallest(5, "return_net") if not included.empty else included

    lines = [
        "# uptrend_limit_down 筛选样本多日持有收益分布",
        "",
        f"- 生成时间：{generated_at}",
        f"- 命中表：`hit_uptrend_limit_down`",
        f"- 命中区间：`{start}` ~ `{end}`",
        f"- T 日低开区间：`{fmt_range(args.gap)}`（闭区间；`raw_open / raw_pre_close - 1`）",
        f"- 近 {args.rise_days} 日涨幅区间：`{fmt_range(args.rise)}`（闭区间，窗口含 T 日；`后复权 close(T) / close(T-{args.rise_days}) - 1`）",
        f"- T+1 买入低开区间：`{fmt_range(args.entry_gap)}`（闭区间；`raw_open(T+1) / raw_pre_close(T+1) - 1`；普通交易日等于 `open(T+1)/close(T) - 1`）",
        "- 买卖规则（后复权）：",
        f"  - T+1 开盘买入；",
        f"  - {hold_window_text(args.max_hold_days)} 为可卖窗口，T+{args.max_hold_days} 是最迟卖出日；",
    ]
    if args.take_profit is not None:
        lines.append(
            f"  - 每个可卖日开盘收益 >= `{fmt_pct(args.take_profit, signed=False)}`：按开盘价卖出；"
        )
    if args.stop_loss is not None:
        lines.append(
            f"  - 每个可卖日开盘收益 <= `-{fmt_pct(args.stop_loss, signed=False)}`：按开盘价卖出；"
        )
    lines.extend([
        f"  - 盘中最高/最低触及已启用的止盈/止损：按对应阈值卖出；",
    ])
    if args.take_profit is not None and args.stop_loss is not None:
        lines.append(
            f"  - 同日同时触及止盈和止损：用 1 分钟行情判断先后；分钟缺失时 `{args.minute_fallback}`；"
        )
    if args.take_profit is None and args.stop_loss is None:
        lines.append("  - 止盈和止损均关闭，持有到最迟卖出日收盘。")
    else:
        lines.append(f"  - 都没触发：持有到 T+{args.max_hold_days} 收盘卖出。")
    lines.extend([
        f"- 止盈：`{fmt_optional_pct(args.take_profit)}`；止损：`{fmt_optional_pct(args.stop_loss)}`",
        f"- 每笔最多 `{args.position:,.0f}` 元、100 股整手；佣金 0.025%（最低 5 元）、印花税 0.05%（卖出）、过户费 0.001%（双边）、滑点 0.1%（双边）",
        "- 假设：T+1 开盘涨停也能买入、跌停也能卖出、忽略停牌；窗口内任一日缺少行情则剔除该样本。",
        (
            f"- 卖出日跌停备注：最终样本中 {n_sell_limit_down} 条"
            f"（{fmt_pct(n_sell_limit_down / n_included if n_included else 0.0, signed=False)}）"
            "的卖出日收盘跌停；收益计算未做卖出限制，仍假设跌停也能卖出。"
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
        "> 前三项为单项条件，之后为逐级筛选；最后一行是三项都满足但无法计算完整卖出窗口的样本。区间条件均为闭区间，恰好等于边界也算满足。",
        "",
        "## 数据覆盖（全部命中样本）",
        "",
        md_table(coverage, ["项目", "样本数", "说明"]),
        "",
        "> 缺失项可能重叠，不能相加。同日双触发是进入卖出窗口后实际遇到的次数，不是全部命中样本数。",
        "",
        "## 毛收益分布（后复权，未扣费用）",
        "",
        md_table(gross_rows, ["指标", "数值"]),
        "",
        "## 净收益分布（后复权，扣佣金/印花税/过户费/滑点）",
        "",
        md_table(net_rows, ["指标", "数值"]),
        "",
        "## 卖出方式分布",
        "",
        md_table(exit_rows, ["卖出方式", "样本数", "占比", "平均净收益", "中位数", "胜率", "平均持有天数"]),
        "",
        "## 卖出判断来源",
        "",
        md_table(source_rows, ["来源", "样本数", "占比", "平均净收益"]),
        "",
        "## 实际持有天数分布",
        "",
        md_table(holding_rows, ["持有天数", "样本数", "平均净收益", "中位数", "胜率"]),
        "",
        "## 分位数",
        "",
        md_table(quantile_rows, ["分位", "毛收益", "净收益"]),
        "",
        "## 分年统计（净收益）",
        "",
        md_table(yearly_rows, ["年份", "样本数", "平均净收益", "中位数", "胜率", "最小", "最大", "标准差"]),
        "",
        "## 按低开幅度分组（净收益）",
        "",
        md_table(gap_rows, ["低开区间", "样本数", "平均净收益", "中位数", "胜率", "最小", "最大"]),
        "",
        "## 最佳 / 最差样本（净收益）",
        "",
        "### 净收益最高 5 个",
        "",
        md_table(sample_rows(top), ["命中日", "代码", "名称", "低开", f"{args.rise_days}日涨幅", "次日低开", "卖出日", "持有", "卖出方式", "净收益率", "分钟首触"]),
        "",
        "### 净收益最低 5 个",
        "",
        md_table(sample_rows(bottom), ["命中日", "代码", "名称", "低开", f"{args.rise_days}日涨幅", "次日低开", "卖出日", "持有", "卖出方式", "净收益率", "分钟首触"]),
        "",
    ])

    if plot_created:
        lines.extend([
            "## 净收益分布图",
            "",
            "![净收益分布](return_distribution.png)",
            "",
        ])

    lines.extend([
        "## 口径说明",
        "",
        "- 低开用不复权 `raw_open / raw_pre_close - 1`。`raw_pre_close` 是交易所除权后的昨收，避免分红/送转造成假低开；普通交易日它与前一日原始收盘价相同。",
        "- T+1 买入低开用 `raw_open(T+1) / raw_pre_close(T+1) - 1`。`raw_pre_close(T+1)` 是交易所除权后的 T 日收盘，普通交易日等于 `close(T)`；CSV 同时保留直接对 T 日原始收盘的 `entry_gap_close_raw` 供核对。",
        "- 近 N 日涨幅和毛收益用后复权价，等价于分红再投资；CSV 里同时保留不复权收益列供核对。",
        f"- 持有天数：T+1 买入，T+2 卖出记为 2 日；T+{args.max_hold_days} 卖出记为 {args.max_hold_days} 日。",
        "- 净收益按每笔最多 5 万元、100 股整手计算：先按 T+1 不复权开盘价加 0.1% 滑点买入，再按卖出价减 0.1% 滑点卖出，扣佣金（最低 5 元）、印花税（卖出）和过户费。",
        "- 净收益分母是该笔后复权口径的占用资金（买入金额 + 买入费用），不是 5 万元上限；不同样本的占用资金不同。费用按不复权成交金额计算，再按 T+1 复权因子换算到后复权口径。",
        "- CSV 中“后复权口径”的净盈亏/占用资金会按复权因子放大，只用于计算净收益率，不是实际现金；实际现金看“净盈亏(不复权)”和“占用资金”。",
        "- 同日双触发时，1 分钟行情按时间升序逐根检查：先看 bar 开盘是否已越过阈值，再看 bar 最高/最低。若同一根 1 分钟 bar 内双触发，阳线假设先低后高（先止损），阴线假设先高后低（先止盈）。这是分钟级路径假设，仍不是逐笔成交。",
        "- 分钟行情是不复权价，乘以当日 `adj_factor` 后与后复权日线对齐；分钟数据缺失时按 `--minute-fallback` 降级，报告会单独统计。",
        f"- 分钟接口单次最多 8000 行；1 分钟单股票单日约 240 行。接口上限 500 次/分钟、30 万次/天；本次实际按 `{minute_stats.get('effective_rate_limit', args.minute_rate_limit)}` 次/分钟限频。",
        f"- 分钟接口本次{'出现限频，后续按 fallback 处理' if minute_stats.get('rate_limited') else '未出现限频'}。",
        "- T+1 开盘涨停买不进、跌停卖不出、停牌无法交易均未建模；按用户要求假设都能成交，只剔除行情缺失的样本。",
        "- 卖出日跌停统计按实际 `exit_date` 收盘价对 `stk_limit.down_limit` 判断，只做备注，不改变任何收益计算。",
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
        "- `samples.csv`：全部命中样本、筛选标记、卖出结果和排除原因",
        "- `included_samples.csv`：最终纳入收益统计的样本",
        "- `paths.csv`：每个样本 T+2~T+N 的逐日收益路径和触发标记",
        "- `summary.csv`：毛收益/净收益分布指标",
        "- `yearly_stats.csv`：分年净收益统计",
        "- `gap_bucket_stats.csv`：按低开幅度分组净收益统计",
        "- `exit_stats.csv`：卖出方式统计",
        "- `holding_stats.csv`：实际持有天数统计",
        "- `run_meta.json`：运行参数和样本数",
        "- `return_distribution.png`：净收益分布图（有样本时生成）",
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
    if args.max_hold_days < 2:
        raise SystemExit("--max-hold-days 必须 >= 2（T+1 买入当天不能卖）")
    if args.take_profit is not None and args.take_profit < 0:
        raise SystemExit("--take-profit 必须 >= 0；传 0 表示关闭止盈")
    if args.take_profit == 0:
        args.take_profit = None
    if args.stop_loss is not None and args.stop_loss < 0:
        raise SystemExit("--stop-loss 必须 >= 0；传 0 表示关闭止损")
    if args.stop_loss == 0:
        args.stop_loss = None
    if args.stop_loss is not None and args.stop_loss >= 1:
        raise SystemExit("--stop-loss 必须小于 1（100%）")
    if args.position < LOT:
        raise SystemExit(f"--position 必须至少 {LOT} 元")
    if args.max_minute_fetches < 0:
        raise SystemExit("--max-minute-fetches 必须 >= 0；0 表示不限制")
    if args.minute_rate_limit <= 0:
        raise SystemExit("--minute-rate-limit 必须为正整数")
    if args.minute_retries < 0:
        raise SystemExit("--minute-retries 必须 >= 0")
    if args.minute_retry_wait < 0:
        raise SystemExit("--minute-retry-wait 必须 >= 0")
    if args.minute_rate_limit > 500:
        print("提示：分钟接口文档上限为 500 次/分钟，当前设置更高，可能被限频")
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
        f"  持有上限：T+1 买入，{hold_window_text(args.max_hold_days)} 可卖，"
        f"最迟 T+{args.max_hold_days} 收盘卖出"
    )
    if args.take_profit is None and args.stop_loss is None:
        print("  止盈/止损：均关闭（持有到最迟卖出日收盘）")
    else:
        both_enabled = args.take_profit is not None and args.stop_loss is not None
        suffix = "；同日双触发用1分钟数据判断先后" if both_enabled else ""
        if args.take_profit is not None:
            print(
                f"  止盈：{fmt_pct(args.take_profit, signed=False)}"
                f"（开盘越过按开盘；盘中触及按阈值{suffix}）"
            )
        else:
            print("  止盈：关闭")
        if args.stop_loss is not None:
            print(
                f"  止损：{fmt_pct(args.stop_loss, signed=False)}"
                f"（开盘越过按开盘；盘中触及按阈值{suffix}）"
            )
        else:
            print("  止损：关闭")
        if both_enabled:
            print(
                f"  同日双触发降级：{args.minute_fallback}；"
                f"分钟数据：{'关闭（仍读取本地缓存）' if args.no_minute else '启用'}"
            )
    print(
        f"  费用口径：每笔最多 {args.position:,.0f} 元、100股整手；"
        f"佣金0.025%(最低5元)、印花税0.05%(卖出)、过户费0.001%、滑点0.1%双边"
    )
    print(f"  生成报告：{'是' if args.generate_report else '否'}")
    print("-" * 90)
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = load_hits(start, end)
    if hits.empty:
        raise SystemExit("指定区间没有 uptrend_limit_down 命中样本")
    print(f"命中样本：{len(hits)} 条")

    calendar = load_calendar()
    hits = attach_dates(hits, calendar, args.rise_days, args.max_hold_days)

    date_columns = [
        "hit_date", "prev_date", "rise_ref_date", "t1_date",
        *[f"t{d}_date" for d in range(2, args.max_hold_days + 1)],
    ]
    needed_dates = pd.concat(
        [hits[col] for col in date_columns if col in hits.columns],
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

    samples, paths = build_samples(
        hits, prices, args.gap, args.rise, args.rise_days, args.entry_gap,
        args.max_hold_days, args.take_profit, args.stop_loss,
    )
    candidate_both = int(paths["both_hit"].sum()) if not paths.empty else 0
    if candidate_both:
        print(f"同日双触发候选（未按卖出先后过滤）：{candidate_both} 个")
        if not args.no_minute and args.max_minute_fetches == 0:
            print(
                "提示：分钟接口单次最多 8000 行、上限 500 次/分钟、30 万次/天；"
                "候选很多时可用 --max-minute-fetches 限制，或用 --no-minute 跳过。"
            )

    minute_cache_root = (
        Path(args.minute_cache_dir)
        if args.minute_cache_dir
        else cache_dir() / "stk_mins_1min"
    )
    minute_loader = MinuteLoader(
        minute_cache_root, enabled=not args.no_minute,
        max_fetches=args.max_minute_fetches,
        rate_limit=args.minute_rate_limit,
        retries=args.minute_retries,
        retry_wait=args.minute_retry_wait,
    )
    if not args.no_minute:
        print(
            f"分钟数据：单次最多 8000 行；接口上限 500 次/分钟、30 万次/天；"
            f"本次按 {args.minute_rate_limit} 次/分钟限频；缓存目录 {minute_cache_root}"
        )
        if args.minute_retries > 0 and args.minute_retry_wait > 0:
            print(
                f"限频后最多重试 {args.minute_retries} 次，每次等待 "
                f"{args.minute_retry_wait:g} 秒"
            )
        else:
            print("限频后不等待、不重试，直接按 --minute-fallback 降级")
        if args.max_minute_fetches > 0:
            print(f"本次最多请求 {args.max_minute_fetches} 次分钟数据，超出后按 fallback 降级")
    samples, minute_stats = resolve_exits(
        samples, paths, args.max_hold_days, args.take_profit, args.stop_loss,
        minute_loader, args.minute_fallback, args.position,
    )
    minute_stats["capped"] = minute_loader.capped
    minute_stats["effective_rate_limit"] = minute_loader.rate_limit
    minute_stats["rate_limited"] = minute_loader.rate_limited

    included = samples[samples["included"]].copy()
    n_sell_limit_down = int(included["sell_limit_down"].sum()) if len(included) else 0
    n_sell_limit_down_missing = (
        int(included["sell_down_limit_missing"].sum()) if len(included) else 0
    )
    stats = distribution_stats(included["return_adj"])
    net_stats = distribution_stats(included["return_net"])
    yearly = grouped_stats(included, "year", "return_net")
    gap_stats = grouped_stats(included, "gap_bucket", "return_net")
    exit_stats = exit_stats_table(included, args.max_hold_days)
    holding_stats = holding_stats_table(included)

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
        csv_frame(paths, args.rise_days).to_csv(
            run_dir / "paths.csv", index=False, encoding="utf-8-sig"
        )

        summary_rows = []
        for key in stats:
            if key in ("P01", "P05", "P10", "P25", "P50", "P75", "P90", "P95", "P99"):
                continue
            if key == "样本数":
                gross_text = str(int(stats[key])) if stats[key] is not None else "-"
                net_text = str(int(net_stats[key])) if net_stats[key] is not None else "-"
            elif key == "盈亏比":
                gross_text = fmt_num(stats[key])
                net_text = fmt_num(net_stats[key])
            elif key in ("标准差", "胜率"):
                gross_text = fmt_pct(stats[key], signed=False)
                net_text = fmt_pct(net_stats[key], signed=False)
            elif key.startswith("均值95%置信区间"):
                gross_text = fmt_pct(stats[key])
                net_text = fmt_pct(net_stats[key])
            else:
                gross_text = fmt_pct(stats[key])
                net_text = fmt_pct(net_stats[key])
            summary_rows.append({
                "指标": key, "毛收益": gross_text, "净收益": net_text,
            })
        pd.DataFrame(summary_rows).to_csv(
            run_dir / "summary.csv", index=False, encoding="utf-8-sig"
        )
        yearly.rename(columns={"分组": "年份"}).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )
        gap_stats.rename(columns={"分组": "低开区间"}).to_csv(
            run_dir / "gap_bucket_stats.csv", index=False, encoding="utf-8-sig"
        )
        exit_stats.rename(columns={"占比": "占比(小数)"}).to_csv(
            run_dir / "exit_stats.csv", index=False, encoding="utf-8-sig"
        )
        holding_stats.to_csv(
            run_dir / "holding_stats.csv", index=False, encoding="utf-8-sig"
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
            "entry_gap_range": list(args.entry_gap),
            "max_hold_days": args.max_hold_days,
            "take_profit": args.take_profit,
            "stop_loss": args.stop_loss,
            "minute_fallback": args.minute_fallback,
            "minute_rate_limit": args.minute_rate_limit,
            "minute_effective_rate_limit": minute_loader.rate_limit,
            "minute_retries": args.minute_retries,
            "minute_retry_wait": args.minute_retry_wait,
            "minute_rate_limited": minute_loader.rate_limited,
            "minute_enabled": not args.no_minute,
            "minute_cache_dir": str(minute_cache_root),
            "position": args.position,
            "commission": COMMISSION,
            "min_commission": MIN_COMMISSION,
            "stamp": STAMP,
            "transfer": TRANSFER,
            "slippage": SLIPPAGE,
            "hit_samples": len(samples),
            "passed_gap": int(samples["passed_gap"].sum()),
            "passed_rise": int(samples["passed_rise"].sum()),
            "passed_entry_gap": int(samples["passed_entry_gap"].sum()),
            "passed_filters": int(samples["passed_filters"].sum()),
            "included_samples": len(included),
            "exit_type_counts": (
                included["exit_type"].value_counts().to_dict()
                if len(included) else {}
            ),
            "exit_source_counts": (
                included["exit_source"].value_counts().to_dict()
                if len(included) else {}
            ),
            "sell_limit_down_samples": n_sell_limit_down,
            "sell_limit_down_missing_samples": n_sell_limit_down_missing,
            "minute_stats": minute_stats,
            "minute_fetched": minute_loader.fetched,
            "minute_cache_hits": minute_loader.cache_hits,
            "minute_failed": minute_loader.failed,
            "missing_adj_factor_rows": missing_adj,
            "prices_rows": len(prices),
            "paths_rows": len(paths),
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        report = build_report(
            samples, included, stats, net_stats, yearly, gap_stats,
            exit_stats, holding_stats, minute_stats, args,
            start, end, missing_adj, plot_created, generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    print("")
    print(f"最终纳入：{len(included)} 条 / 命中 {len(samples)} 条")
    print(f"平均毛收益率（后复权）：{fmt_pct(stats['平均收益率'])}")
    print(f"平均净收益率（扣费+滑点）：{fmt_pct(net_stats['平均收益率'])}")
    print(f"净收益中位数：{fmt_pct(net_stats['中位数'])}")
    print(f"净收益胜率：{fmt_pct(net_stats['胜率'], signed=False)}")
    if len(included):
        counts = included["exit_type"].value_counts()
        parts = [f"{name} {int(count)} 条" for name, count in counts.items()]
        print("卖出方式：" + "，".join(parts))
        print(f"平均持有天数：{included['holding_days'].mean():.2f}")
        print(
            f"备注：收益计算假设卖出日跌停也能卖出；最终样本中卖出日收盘跌停 "
            f"{n_sell_limit_down} 条（{n_sell_limit_down / len(included):.2%}）"
        )
        if n_sell_limit_down_missing:
            print(f"      其中 {n_sell_limit_down_missing} 条缺少卖出日跌停价，无法判断")
        print(
            f"分钟数据：需要 {minute_stats['needed']} 次，"
            f"成功判断 {minute_stats['used']} 次，"
            f"缺失降级 {minute_stats['failed']} 次，"
            f"限额跳过 {minute_stats.get('capped', 0)} 次，"
            f"实际限频 {minute_stats.get('effective_rate_limit', args.minute_rate_limit)} 次/分钟，"
            f"本次{'出现限频' if minute_stats.get('rate_limited') else '未出现限频'}"
        )
    else:
        print("备注：收益计算假设卖出日跌停也能卖出；最终样本 0 条，卖出日跌停 0 条")
    if run_dir is not None:
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（默认关闭；加 --report 可生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
