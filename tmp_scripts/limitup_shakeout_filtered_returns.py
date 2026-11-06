"""统计 limit_up_shakeout 命中样本经筛选后的收益分布。

筛选条件（T 为命中日）：
1. T 日高开落在闭区间：DEFAULT_GAP[0] <= raw_open(T) / raw_pre_close(T) - 1 <= DEFAULT_GAP[1]
2. T 日收盘涨跌幅落在闭区间：
   DEFAULT_CLOSE_CHG[0] <= raw_close(T) / raw_pre_close(T) - 1 <= DEFAULT_CLOSE_CHG[1]
3. 近 N 个交易日涨幅（含 T 日）落在闭区间：
   DEFAULT_RISE[0] <= close(T) / close(T-N) - 1 <= DEFAULT_RISE[1]
4. T+1 买入低开落在闭区间：DEFAULT_ENTRY_GAP[0] <= raw_open(T+1) / raw_pre_close(T+1) - 1 <= DEFAULT_ENTRY_GAP[1]
   （raw_pre_close(T+1) 是交易所除权后的 T 日收盘，普通日等于 T 日收盘）
5. （可选，默认不要求）T 日最低价不破前一交易日收盘价：
   low(T) >= close(T-1)（后复权；用 --require-low-support 开启）

买入规则（--delta，单位百分点，默认 DEFAULT_DELTA）：
    a = open(T+1) / close(T) - 1             # 高开比例（不复权，a<0 即低开）
    买入价 = (1 + a - delta/100) × close(T)   # 等价于 open(T+1) - (delta/100) × close(T)
- delta=0：买入价即 open(T+1)，完全等价于原「T+1 开盘买入」规则；
- delta>0：视为开盘下方 delta% 的限价单，T+1 最低价 <= 买入价才算成交（按买入价成交）；
  日内未触及买入价则买入失败，跳过该样本（不纳入收益统计）；
止盈率不再固定，按 T+1 收盘对买入价的涨幅分档确定（默认 DEFAULT_EXIT_TIERS，见
src/quant/engine/exit_tiers.py；可用 --exit-tiers 传 JSON 覆盖，--open-exit 退回固定阈值）：
    day1_ret = close(T+1) / 买入价 - 1        # delta=0 时即 close(T+1)/open(T+1)-1（原日内涨幅）
    (左开右闭区间)            止盈率
    ≤ -5%                    -2%（止损离场）
    -5% ~ 0%                 +1%
    0% ~ 5%                  +2%
    5% ~ 7%                  +3%
    > 7%                     +4%
每档执行方式与固定止盈一致，只是阈值逐样本不同（涨幅全部以买入价为基准）：
1. T+2 开盘相对买入价涨幅 > 该档止盈率：按 T+2 开盘卖出（开盘已高于目标价，以开盘价成交）；
2. 否则若 T+2 最高价相对买入价涨幅 >= 该档止盈率：按该止盈率卖出；
3. 否则持有到 T+2 收盘卖出：
    open_ret = open(T+2) / 买入价 - 1
    high_ret = high(T+2) / 买入价 - 1
    close_ret = close(T+2) / 买入价 - 1
    threshold = 按日涨幅分档的止盈率
    return = open_ret if open_ret > threshold
             else threshold if high_ret >= threshold
             else close_ret

价格口径：
- T 日高开、T 日收盘涨跌幅和 T+1 买入低开用不复权 raw_open/raw_close 对 raw_pre_close，raw_pre_close 是交易所除权后的昨收；
- 买入价与限价成交判定（low(T+1) 对买入价）用不复权口径；T+1 恰为除权日时 a 与买入价
  会受除权影响明显偏低，此类样本大概率因未触及买入价而跳过；
- 近 N 日涨幅和买卖收益用后复权价，等价于分红再投资；
- 可选的最低价条件用后复权 low(T) 对前一日后复权收盘价 close(T-1)；
- CSV 同时保留不复权收益，便于核对。

用法：
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --report
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --start 2024-01-01 --end 2026-10-23 --report
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --gap 0.02 0.08 --close-chg 0.00 0.05 --entry-gap -0.06 -0.01 --rise 0.05 0.30
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --delta 0        # 原规则：T+1 开盘价买入
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --delta 1.5 --report   # 开盘下方 1.5% 限价买入
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --require-low-support --report
    uv run python tmp_scripts/limitup_shakeout_filtered_returns.py --exit-tiers "[[null,-0.05,-0.02],[-0.05,0,0.01],[0,0.05,0.02],[0.05,0.07,0.03],[0.07,null,0.04]]"

DEFAULT_END = "latest" 表示自动使用命中表最新日期；DEFAULT_GENERATE_REPORT = False 表示默认只打印控制台，不生成报告文件。

样本来自 hit_limit_up_shakeout（策略名 limit_up_shakeout）。若策略逻辑或参数有调整，先执行：
    uv run quant hits update -s limit_up_shakeout --from-date 2018-01-01
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
# 统计区间
DEFAULT_START = "20241101"
DEFAULT_END = "latest"

# 信号日（T日）开盘/收盘价相对于昨收（T-1日收盘价）的变化比例范围限制
DEFAULT_GAP = (0.00, 0.11)
DEFAULT_CLOSE_CHG = (0.0, 0.11)

# 近N日收盘价涨幅范围限制
DEFAULT_RISE_DAYS = 5
DEFAULT_RISE = (-10, 10)

# 止盈：按 T+1 日内涨幅（close(T+1)/open(T+1)-1）分段的动态止盈，左开右闭；
# 默认分档是 quant.engine.exit_tiers.DEFAULT_EXIT_TIERS（一个变量概括整个分段函数），
# 想改默认直接改那个变量；也可用 --exit-tiers 传 JSON 覆盖，或 --open-exit 退回固定阈值（单段）

# 信号日次日开盘价相对于信号日收盘价的比例限制（T+1日进入的条件限制）
DEFAULT_ENTRY_GAP = (-0.11, 0)

# T+1 限价买入下探深度 delta（百分点）：a = open(T+1)/close(T)-1（不复权），
# 买入价 = (1 + a - delta/100) × close(T)；0 = 开盘价买入（原规则），正数即开盘下方 delta%
DEFAULT_DELTA = 1.0

# 是否要求信号日最低价 >= 前一交易日收盘价（后复权）；默认不要求
DEFAULT_REQUIRE_LOW_SUPPORT = True
# ==============================================================================


# 不用改
DEFAULT_GENERATE_REPORT = True
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
    "t_adj_low": "命中日最低价(后复权)",
    "t_adj_close": "命中日收盘价(后复权)",
    "prev_close_raw": "前一交易日收盘价(不复权)",
    "prev_adj_close": "前一交易日收盘价(后复权)",
    "rise_ref_adj_close": "涨幅基准日收盘价(后复权)",
    "t1_open_raw": "次日开盘价(不复权)",
    "t1_close_raw": "次日收盘价(不复权)",
    "t1_pre_close_raw": "次日昨收价(除权后)",
    "t1_low_raw": "次日最低价(不复权)",
    "t1_adj_open": "次日开盘价(后复权)",
    "t1_adj_close": "次日收盘价(后复权)",
    "t1_intraday_ret": "T+1收盘对买入价涨幅",
    "t1_intraday_ret_pct": "T+1收盘对买入价涨幅(%)",
    "buy_price_raw": "买入价(不复权)",
    "buy_adj": "买入价(后复权)",
    "buy_filled": "限价单是否成交",
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
    "gap_open": "高开幅度",
    "gap_open_prev_raw": "高开幅度(对前一日原始收盘)",
    "close_chg": "信号日收盘涨跌幅",
    "ret_rise": "近N日涨幅",
    "t2_open_ret": "次次日开盘收益",
    "t2_high_ret": "次次日最高收益",
    "t2_close_ret": "次次日收盘收益",
    "t2_open_ret_raw": "次次日开盘收益(不复权)",
    "t2_high_ret_raw": "次次日最高收益(不复权)",
    "t2_close_ret_raw": "次次日收盘收益(不复权)",
    "exit_threshold": "动态止盈阈值",
    "exit_threshold_pct": "动态止盈阈值(%)",
    "exit_tier": "止盈区间",
    "exit_at_open": "是否开盘卖出",
    "exit_intraday": "是否盘中止盈",
    "return_adj": "收益率(后复权)",
    "return_raw": "收益率(不复权)",
    "exit_type": "卖出方式",
    "sell_limit_down": "卖出日是否跌停",
    "sell_down_limit_missing": "卖出日跌停价是否缺失",
    "passed_gap": "是否满足高开条件",
    "passed_close": "是否满足收盘涨跌幅条件",
    "passed_rise": "是否满足涨幅条件",
    "passed_low_support": "是否满足最低价不破昨收",
    "included": "是否纳入统计",
    "gap_open_pct": "高开幅度(%)",
    "gap_open_prev_raw_pct": "高开幅度(对前一日原始收盘,%)",
    "close_chg_pct": "信号日收盘涨跌幅(%)",
    "entry_gap_pct": "次日开盘低开幅度(%)",
    "entry_gap_close_raw_pct": "次日开盘低开幅度(对T日收盘,%)",
    "ret_rise_pct": "近N日涨幅(%)",
    "t2_open_ret_pct": "次次日开盘收益(%)",
    "t2_high_ret_pct": "次次日最高收益(%)",
    "t2_close_ret_pct": "次次日收盘收益(%)",
    "return_pct": "收益率(%)",
    "return_raw_pct": "收益率(不复权,%)",
    "year": "年份",
    "gap_bucket": "高开区间",
    "exclusion_reason": "未纳入原因",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统计 limit_up_shakeout 二次筛选样本的 T+1 开盘买 / T+2 卖出收益分布"
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
        help=f"T 日高开闭区间（小数），默认 {DEFAULT_GAP}，要求 MIN <= gap <= MAX",
    )
    parser.add_argument(
        "--close-chg",
        nargs=2,
        type=float,
        default=DEFAULT_CLOSE_CHG,
        metavar=("MIN", "MAX"),
        help=f"T 日收盘涨跌幅闭区间（小数），默认 {DEFAULT_CLOSE_CHG}，要求 MIN <= 涨跌幅 <= MAX",
    )
    parser.add_argument(
        "--rise",
        nargs=2,
        type=float,
        default=DEFAULT_RISE,
        metavar=("MIN", "MAX"),
        help=f"近 N 日涨幅闭区间（小数），默认 {DEFAULT_RISE}，要求 MIN <= 涨幅 <= MAX",
    )
    parser.add_argument(
        "--rise-days",
        type=int,
        default=DEFAULT_RISE_DAYS,
        help=f"涨幅回看交易日数，默认 {DEFAULT_RISE_DAYS}",
    )
    parser.add_argument(
        "--exit-tiers",
        default=None,
        help=(
            "动态止盈分档 JSON：[[下界, 上界, 止盈率], ...]，区间左开右闭，"
            "下界/上界为 null 表示 -∞/+∞（首段下界、末段上界必须是 null）；"
            f"默认 {json.dumps(tiers_to_jsonable(DEFAULT_EXIT_TIERS), ensure_ascii=False)}"
        ),
    )
    parser.add_argument(
        "--open-exit",
        type=float,
        default=None,
        help="固定止盈阈值（退化为单段分档，覆盖全部日涨幅）；与 --exit-tiers 互斥；缺省用动态分档",
    )
    parser.add_argument(
        "--entry-gap",
        nargs=2,
        type=float,
        default=DEFAULT_ENTRY_GAP,
        metavar=("MIN", "MAX"),
        help=f"T+1 买入低开闭区间（小数），默认 {DEFAULT_ENTRY_GAP}，要求 MIN <= gap <= MAX",
    )
    parser.add_argument(
        "--delta",
        type=float,
        default=DEFAULT_DELTA,
        metavar="PCT",
        help=(
            "T+1 限价买入下探深度（百分点，1.0 即 1%%）：买入价 = (1 + a - delta%%) × close(T)，"
            "a = open(T+1)/close(T)-1（不复权）；T+1 最低价 <= 买入价才成交（按买入价成交），"
            "日内未触及则跳过该样本；0 = 开盘价买入（原规则）；"
            f"默认 {DEFAULT_DELTA:g}（即开盘下方 {DEFAULT_DELTA:g}%%）"
        ),
    )
    parser.add_argument(
        "--require-low-support",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_REQUIRE_LOW_SUPPORT,
        help=(
            "是否要求信号日最低价 >= 前一交易日收盘价（后复权 low(T) / close(T-1)），"
            f"默认 {'要求' if DEFAULT_REQUIRE_LOW_SUPPORT else '不要求'}；"
            "用 --no-require-low-support 显式关闭"
        ),
    )
    parser.add_argument(
        "--report",
        dest="generate_report",
        action="store_true",
        help="生成 report.md / CSV / 收益分布图",
    )
    parser.add_argument(
        "--no-report",
        dest="generate_report",
        action="store_false",
        help="只打印控制台，不生成报告文件（默认）",
    )
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument(
        "--output-dir", default="outputs", help="报告输出根目录，默认 outputs"
    )
    return parser.parse_args(argv)


def resolve_exit_tiers(args: argparse.Namespace) -> tuple[tuple, str]:
    """把 CLI 参数解析为止盈分档：--exit-tiers > --open-exit（固定单段） > 默认分档。"""
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
        FROM hit_limit_up_shakeout
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


def attach_dates(
    hits: pd.DataFrame, calendar: list[str], rise_days: int
) -> pd.DataFrame:
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
        chunk = codes[i : i + PRICE_CHUNK]
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
    for col in (
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "adj_factor",
        "down_limit",
    ):
        prices[col] = pd.to_numeric(prices[col], errors="coerce")
    prices["adj_open"] = prices["open"] * prices["adj_factor"]
    prices["adj_high"] = prices["high"] * prices["adj_factor"]
    prices["adj_low"] = prices["low"] * prices["adj_factor"]
    prices["adj_close"] = prices["close"] * prices["adj_factor"]
    prices["trade_date"] = prices["trade_date"].astype(str)
    prices["ts_code"] = prices["ts_code"].astype(str)
    return prices


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
    rise_range: tuple[float, float],
    rise_days: int,
    exit_tiers: tuple,
    entry_gap_range: tuple[float, float],
    delta_pct: float = 0.0,
    require_low_support: bool = False,
) -> pd.DataFrame:
    """delta_pct 为 T+1 限价买入下探深度（百分点）：0 = 开盘价买入（原规则）；
    >0 时买入价 = open(T+1) - (delta_pct/100)×close(T)，T+1 最低价触及才成交，
    未成交样本不纳入（included=False）。止盈分档涨幅与 T+2 涨幅全部以买入价为基准。
    """
    samples = _merge_role(
        hits,
        prices,
        "hit_date",
        {
            "open": "t_open_raw",
            "close": "t_close_raw",
            "pre_close": "t_pre_close_raw",
            "adj_open": "t_adj_open",
            "adj_low": "t_adj_low",
            "adj_close": "t_adj_close",
        },
    )
    samples = _merge_role(
        samples,
        prices,
        "prev_date",
        {"close": "prev_close_raw", "adj_close": "prev_adj_close"},
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
            "low": "t1_low_raw",
            "pre_close": "t1_pre_close_raw",
            "adj_open": "t1_adj_open",
            "adj_close": "t1_adj_close",
        },
    )
    samples = _merge_role(
        samples,
        prices,
        "t2_date",
        {
            "open": "t2_open_raw",
            "high": "t2_high_raw",
            "close": "t2_close_raw",
            "down_limit": "t2_down_limit",
            "adj_open": "t2_adj_open",
            "adj_high": "t2_adj_high",
            "adj_close": "t2_adj_close",
        },
    )

    samples["gap_open"] = samples["t_open_raw"] / samples["t_pre_close_raw"] - 1.0
    samples["gap_open_prev_raw"] = (
        samples["t_open_raw"] / samples["prev_close_raw"] - 1.0
    )
    # T 日收盘涨跌幅：同样用交易所除权后的昨收，避免分红/送转造成失真。
    samples["close_chg"] = samples["t_close_raw"] / samples["t_pre_close_raw"] - 1.0
    # T+1 买入低开：用 T+1 的交易所除权后昨收，普通日等于 T 日收盘。
    samples["entry_gap"] = samples["t1_open_raw"] / samples["t1_pre_close_raw"] - 1.0
    samples["entry_gap_close_raw"] = (
        samples["t1_open_raw"] / samples["t_close_raw"] - 1.0
    )
    samples["ret_rise"] = samples["t_adj_close"] / samples["rise_ref_adj_close"] - 1.0

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
    # T+2 各价对买入价的涨幅（后复权/不复权），是止盈判定与收益计算的唯一基准；
    # delta=0 时 buy_adj/buy_price_raw 即 T+1 开盘价，与原口径完全一致
    samples["t2_open_ret"] = samples["t2_adj_open"] / samples["buy_adj"] - 1.0
    samples["t2_high_ret"] = samples["t2_adj_high"] / samples["buy_adj"] - 1.0
    samples["t2_close_ret"] = samples["t2_adj_close"] / samples["buy_adj"] - 1.0
    samples["t2_open_ret_raw"] = samples["t2_open_raw"] / samples["buy_price_raw"] - 1.0
    samples["t2_high_ret_raw"] = samples["t2_high_raw"] / samples["buy_price_raw"] - 1.0
    samples["t2_close_ret_raw"] = samples["t2_close_raw"] / samples["buy_price_raw"] - 1.0
    samples["exit_at_open"] = samples["t2_open_ret"].gt(samples["exit_threshold"])
    samples["exit_intraday"] = ~samples["exit_at_open"] & samples["t2_high_ret"].ge(
        samples["exit_threshold"]
    )
    samples["return_adj"] = samples["t2_close_ret"]
    samples["return_raw"] = samples["t2_close_ret_raw"]
    samples.loc[samples["exit_intraday"], "return_adj"] = samples.loc[
        samples["exit_intraday"], "exit_threshold"
    ]
    samples.loc[samples["exit_intraday"], "return_raw"] = samples.loc[
        samples["exit_intraday"], "exit_threshold"
    ]
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
    # T+1 收盘价或买入价缺失（涨幅无效）时无法分档，止盈阈值不可确定，按缺失处理。
    no_threshold = samples["exit_threshold"].isna()
    samples.loc[no_threshold, "return_adj"] = np.nan
    samples.loc[no_threshold, "return_raw"] = np.nan

    samples["exit_type"] = "T+2 收盘"
    samples.loc[samples["exit_intraday"], "exit_type"] = "T+2 盘中"
    samples.loc[samples["exit_at_open"], "exit_type"] = "T+2 开盘"
    samples.loc[samples["return_adj"].isna(), "exit_type"] = None

    # 卖出日（T+2）收盘是否跌停；只做统计备注，不改变收益计算。
    samples["sell_down_limit_missing"] = samples["t2_down_limit"].isna() | samples[
        "t2_down_limit"
    ].le(0)
    samples["sell_limit_down"] = (
        samples["t2_down_limit"].gt(0)
        & samples["t2_close_raw"].le(samples["t2_down_limit"] + LIMIT_EPS)
    ).fillna(False)

    numeric_cols = [
        "gap_open",
        "gap_open_prev_raw",
        "close_chg",
        "entry_gap",
        "entry_gap_close_raw",
        "ret_rise",
        "t1_intraday_ret",
        "exit_threshold",
        "t2_open_ret",
        "t2_high_ret",
        "t2_close_ret",
        "t2_open_ret_raw",
        "t2_high_ret_raw",
        "t2_close_ret_raw",
        "return_adj",
        "return_raw",
    ]
    for col in numeric_cols:
        samples[col] = samples[col].replace([np.inf, -np.inf], np.nan)

    # 闭区间：两端都包含，允许恰好等于边界。
    gap_lo, gap_hi = gap_range
    close_lo, close_hi = close_chg_range
    entry_lo, entry_hi = entry_gap_range
    samples["passed_gap"] = samples["gap_open"].ge(gap_lo - GAP_EPS) & samples[
        "gap_open"
    ].le(gap_hi + GAP_EPS)
    samples["passed_close"] = samples["close_chg"].ge(close_lo - GAP_EPS) & samples[
        "close_chg"
    ].le(close_hi + GAP_EPS)
    rise_lo, rise_hi = rise_range
    samples["passed_rise"] = samples["ret_rise"].ge(rise_lo - GAP_EPS) & samples[
        "ret_rise"
    ].le(rise_hi + GAP_EPS)
    samples["passed_entry_gap"] = samples["entry_gap"].ge(entry_lo - GAP_EPS) & samples[
        "entry_gap"
    ].le(entry_hi + GAP_EPS)
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
    samples["exclusion_reason"] = samples.apply(
        lambda row: _exclusion_reason(row, rise_days, require_low_support), axis=1
    )
    return samples


def _exclusion_reason(
    row: pd.Series, rise_days: int, require_low_support: bool = False
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
    if pd.notna(row["buy_price_raw"]) and not bool(row["buy_filled"]):
        if pd.isna(row["t1_low_raw"]):
            reasons.append("缺少T+1最低价，无法判定限价成交")
        else:
            reasons.append("T+1日内未触及限价买入价")
    if pd.isna(row["t1_date"]) or pd.isna(row["t2_date"]):
        reasons.append("缺少T+1/T+2交易日")
    elif pd.isna(row["exit_threshold"]):
        if pd.isna(row["buy_price_raw"]):
            reasons.append("买入价缺失，无法确定止盈档位")
        else:
            reasons.append("缺少T+1收盘价，无法确定止盈档位")
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
        rows.append(
            {
                "分组": str(key),
                "样本数": len(rets),
                "平均收益率": float(rets.mean()),
                "中位数": float(rets.median()),
                "胜率": float((rets > 0).mean()),
                "最小收益率": float(rets.min()),
                "最大收益率": float(rets.max()),
                "标准差": float(rets.std(ddof=1)) if len(rets) > 1 else None,
            }
        )
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


def md_table(rows: list[dict[str, object]], columns: list[str] | None = None) -> str:
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
    axes[0].axvline(
        rets.mean(), color="#E45756", linewidth=1.5, label=f"Mean {rets.mean():.2f}%"
    )
    axes[0].axvline(
        rets.median(),
        color="#F58518",
        linewidth=1.5,
        linestyle="--",
        label=f"Median {rets.median():.2f}%",
    )
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


def build_report(
    samples: pd.DataFrame,
    included: pd.DataFrame,
    stats: dict[str, float | int | None],
    yearly: pd.DataFrame,
    gap_stats: pd.DataFrame,
    tier_stats: pd.DataFrame,
    exit_tiers: tuple,
    args: argparse.Namespace,
    start: str,
    end: str,
    missing_adj: int,
    plot_created: bool,
    generated_at: str,
) -> str:
    total = len(samples)
    gap_ok = int(samples["passed_gap"].sum())
    close_ok = int(samples["passed_close"].sum())
    low_ok = int(samples["passed_low_support"].sum())
    rise_ok = int(samples["passed_rise"].sum())
    t_day_mask = (
        samples["passed_gap"] & samples["passed_close"] & samples["passed_rise"]
    )
    if args.require_low_support:
        t_day_mask &= samples["passed_low_support"]
    t_day_ok = int(t_day_mask.sum())
    entry_ok = int(samples["passed_entry_gap"].sum())
    entry_fill_ok = int((samples["passed_entry_gap"] & samples["buy_filled"]).sum())
    all_ok = int(
        (t_day_mask & samples["passed_entry_gap"] & samples["buy_filled"]).sum()
    )
    t_conditions = 3 + int(args.require_low_support)
    n_conditions = t_conditions + 1 + int(args.delta > 0)
    n_included = int(samples["included"].sum())
    n_sell_limit_down = int(included["sell_limit_down"].sum()) if n_included else 0
    n_sell_limit_down_missing = (
        int(included["sell_down_limit_missing"].sum()) if n_included else 0
    )
    missing_forward = int(samples["return_adj"].isna().sum())
    missing_rise = int(samples["ret_rise"].isna().sum())
    missing_gap = int(samples["gap_open"].isna().sum())
    missing_close = int(samples["close_chg"].isna().sum())
    missing_t1_close = int(samples["t1_intraday_ret"].isna().sum())
    missing_t1_low = int(samples["t1_low_raw"].isna().sum())

    funnel = [
        {"阶段": "命中样本总数", "样本数": total, "说明": "hit_limit_up_shakeout"},
        {
            "阶段": f"T日高开 ∈ {fmt_range(args.gap)}",
            "样本数": gap_ok,
            "说明": "raw_open / raw_pre_close - 1",
        },
        {
            "阶段": f"T日收盘涨跌幅 ∈ {fmt_range(args.close_chg)}",
            "样本数": close_ok,
            "说明": "raw_close / raw_pre_close - 1",
        },
    ]
    if args.require_low_support:
        funnel.append(
            {
                "阶段": "T日最低价 >= 前一日收盘价",
                "样本数": low_ok,
                "说明": "后复权 low(T) / close(T-1)",
            }
        )
    funnel.extend(
        [
            {
                "阶段": f"近{args.rise_days}日涨幅 ∈ {fmt_range(args.rise)}",
                "样本数": rise_ok,
                "说明": "后复权收盘价",
            },
            {
                "阶段": f"T日{t_conditions}项同时满足",
                "样本数": t_day_ok,
                "说明": "高开+收盘涨跌幅+"
                + ("最低价+" if args.require_low_support else "")
                + "近N日涨幅",
            },
            {
                "阶段": f"T+1开盘 ∈ {fmt_range(args.entry_gap)}",
                "样本数": entry_ok,
                "说明": "raw_open(T+1)/raw_pre_close(T+1)-1",
            },
        ]
    )
    if args.delta > 0:
        funnel.append(
            {
                "阶段": f"T+1限价单成交（delta={args.delta:g}%）",
                "样本数": entry_fill_ok,
                "说明": (
                    "low(T+1) <= (1+a-delta%)×close(T)，"
                    "a=open(T+1)/close(T)-1；未成交则跳过"
                ),
            }
        )
    funnel.extend(
        [
            {
                "阶段": f"{n_conditions}项同时满足",
                "样本数": all_ok,
                "说明": (
                    "含限价成交；尚未要求T+1/T+2行情完整"
                    if args.delta > 0
                    else "尚未要求T+1/T+2行情完整"
                ),
            },
            {
                "阶段": "最终纳入收益统计",
                "样本数": n_included,
                "说明": "且T+1开盘/收盘、T+2行情可计算",
            },
            {
                "阶段": f"{n_conditions}项满足但缺T+1/T+2行情",
                "样本数": all_ok - n_included,
                "说明": "停牌或行情尚未产生",
            },
        ]
    )
    coverage = [
        {"项目": "缺T日开盘/昨收", "样本数": missing_gap, "说明": "无法计算高开"},
        {
            "项目": "缺T日收盘/昨收",
            "样本数": missing_close,
            "说明": "无法计算收盘涨跌幅",
        },
        {
            "项目": f"缺T-{args.rise_days}行情",
            "样本数": missing_rise,
            "说明": "无法计算近N日涨幅",
        },
        {
            "项目": "缺T+1收盘价",
            "样本数": missing_t1_close,
            "说明": "无法计算对买入价涨幅并分档",
        },
        {
            "项目": "缺T+1最低价",
            "样本数": missing_t1_low,
            "说明": "delta>0 时无法判定限价单是否成交",
        },
        {
            "项目": "缺T+1/T+2行情",
            "样本数": missing_forward,
            "说明": "无法计算前向收益",
        },
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
        {
            "指标": "均值95%置信区间",
            "数值": (
                f"[{fmt_pct(stats['均值95%置信区间下限'])}, "
                f"{fmt_pct(stats['均值95%置信区间上限'])}]"
            ),
        },
    ]

    quantile_rows = [
        {
            "分位": f"P{int(q * 100):02d}",
            "收益率": fmt_pct(stats.get(f"P{int(q * 100):02d}")),
        }
        for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
    ]

    yearly_rows = []
    for row in yearly.itertuples(index=False):
        yearly_rows.append(
            {
                "年份": row.分组,
                "样本数": row.样本数,
                "平均": fmt_pct(row.平均收益率),
                "中位数": fmt_pct(row.中位数),
                "胜率": fmt_pct(row.胜率, signed=False),
                "最小": fmt_pct(row.最小收益率),
                "最大": fmt_pct(row.最大收益率),
                "标准差": fmt_pct(row.标准差, signed=False),
            }
        )

    gap_rows = []
    for row in gap_stats.itertuples(index=False):
        gap_rows.append(
            {
                "高开区间": row.分组,
                "样本数": row.样本数,
                "平均": fmt_pct(row.平均收益率),
                "中位数": fmt_pct(row.中位数),
                "胜率": fmt_pct(row.胜率, signed=False),
                "最小": fmt_pct(row.最小收益率),
                "最大": fmt_pct(row.最大收益率),
            }
        )

    tier_rate_map = dict(zip(tier_labels(exit_tiers), (t[2] for t in exit_tiers)))
    tier_table_rows = [
        {
            "T+1 收盘对买入价涨幅": label,
            "止盈阈值": fmt_pct(rate),
        }
        for label, (_lo, _hi, rate) in zip(tier_labels(exit_tiers), exit_tiers)
    ]
    tier_rows = []
    for row in tier_stats.itertuples(index=False):
        tier_rows.append(
            {
                "止盈区间": row.分组,
                "止盈阈值": fmt_pct(tier_rate_map.get(row.分组)),
                "样本数": row.样本数,
                "平均": fmt_pct(row.平均收益率),
                "中位数": fmt_pct(row.中位数),
                "胜率": fmt_pct(row.胜率, signed=False),
                "最小": fmt_pct(row.最小收益率),
                "最大": fmt_pct(row.最大收益率),
            }
        )

    exit_rows = []
    if n_included:
        for exit_type in ("T+2 开盘", "T+2 盘中", "T+2 收盘"):
            group = included[included["exit_type"] == exit_type]
            rets = pd.to_numeric(group["return_adj"], errors="coerce").dropna()
            if rets.empty:
                continue
            exit_rows.append(
                {
                    "卖出方式": exit_type,
                    "样本数": len(rets),
                    "占比": fmt_pct(len(rets) / n_included, signed=False),
                    "平均": fmt_pct(rets.mean()),
                    "中位数": fmt_pct(rets.median()),
                    "胜率": fmt_pct((rets > 0).mean(), signed=False),
                }
            )

    def sample_rows(df: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for row in df.itertuples(index=False):
            rows.append(
                {
                    "命中日": row.hit_date,
                    "代码": row.ts_code,
                    "名称": row.name,
                    "高开": fmt_pct(row.gap_open),
                    "收盘涨跌幅": fmt_pct(row.close_chg),
                    f"{args.rise_days}日涨幅": fmt_pct(row.ret_rise),
                    "次日低开": fmt_pct(row.entry_gap),
                    "卖出方式": row.exit_type,
                    "收益率": fmt_pct(row.return_adj),
                }
            )
        return rows

    top = included.nlargest(5, "return_adj") if not included.empty else included
    bottom = included.nsmallest(5, "return_adj") if not included.empty else included

    lines = [
        "# limit_up_shakeout 筛选样本收益分布",
        "",
        f"- 生成时间：{generated_at}",
        f"- 命中表：`hit_limit_up_shakeout`",
        f"- 命中区间：`{start}` ~ `{end}`",
        f"- T 日高开区间：`{fmt_range(args.gap)}`（闭区间；`raw_open / raw_pre_close - 1`）",
        f"- T 日收盘涨跌幅区间：`{fmt_range(args.close_chg)}`（闭区间；`raw_close / raw_pre_close - 1`）",
        f"- 近 {args.rise_days} 日涨幅区间：`{fmt_range(args.rise)}`（闭区间，窗口含 T 日；`后复权 close(T) / close(T-{args.rise_days}) - 1`）",
        f"- T+1 买入低开区间：`{fmt_range(args.entry_gap)}`（闭区间；`raw_open(T+1) / raw_pre_close(T+1) - 1`；普通交易日等于 `open(T+1)/close(T) - 1`）",
        f"- 信号日最低价条件：{'要求' if args.require_low_support else '不要求'}（后复权 `low(T) >= close(T-1)`）",
        "- 买入（不复权）："
        + (
            f"限价单 `(1 + a - {args.delta:g}%) × close(T)`（`a = open(T+1)/close(T) - 1`，"
            f"即开盘下方 {args.delta:g}%）；`low(T+1) <= 买入价` 才成交（按买入价成交），"
            "未触及则买入失败、跳过该样本"
            if args.delta > 0
            else "`open(T+1)`（delta=0，开盘价买入）"
        ),
        "- 卖出规则（后复权；逐样本动态止盈，涨幅均以买入价为基准）：",
        "  - T+1 收盘对买入价涨幅 = `close(T+1) / 买入价 - 1`（delta=0 时即日内涨幅 `close/open`），"
        "按下表分档确定止盈阈值（区间左开右闭，止盈率可为负即止损）：",
        "",
        md_table(tier_table_rows, ["T+1 收盘对买入价涨幅", "止盈阈值"]),
        "",
        "  - T+2 开盘 / 买入价 - 1 > 该档止盈阈值：按 T+2 开盘卖出（开盘已高于目标价，以开盘价成交）；",
        "  - 否则 T+2 最高价 / 买入价 - 1 >= 该档止盈阈值：按该止盈阈值卖出；",
        "  - 否则持有到 T+2 收盘卖出。",
        "- 收益率：卖出价 / 买入价(后复权) - 1（盘中止盈按阈值计）",
        "- 交易成本：未扣除手续费、印花税和滑点",
        (
            f"- 卖出日跌停备注：最终样本中 {n_sell_limit_down} 条"
            f"（{fmt_pct(n_sell_limit_down / n_included if n_included else 0.0, signed=False)}）"
            "的卖出日（T+2）收盘跌停；收益计算未做卖出限制，仍假设跌停也能卖出。"
            + (
                f" 另有 {n_sell_limit_down_missing} 条缺少跌停价，无法判断。"
                if n_sell_limit_down_missing
                else ""
            )
        ),
        "",
        "## 样本漏斗",
        "",
        md_table(funnel, ["阶段", "样本数", "说明"]),
        "",
        "> 各单项条件为独立统计；其余为组合/逐级筛选（可选的最低价条件仅在开启时计入组合）。T+1 限价单成交（delta>0 时）为「T+1开盘 ∈ 区间」样本中限价成交的组合口径。最后一行是全部条件满足但无法计算前向收益的样本。区间条件均为闭区间，恰好等于边界也算满足。",
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
        md_table(
            yearly_rows,
            ["年份", "样本数", "平均", "中位数", "胜率", "最小", "最大", "标准差"],
        ),
        "",
        "## 按高开幅度分组",
        "",
        md_table(
            gap_rows, ["高开区间", "样本数", "平均", "中位数", "胜率", "最小", "最大"]
        ),
        "",
        "## 按止盈区间分组（按 T+1 收盘对买入价涨幅分档）",
        "",
        md_table(
            tier_rows,
            ["止盈区间", "止盈阈值", "样本数", "平均", "中位数", "胜率", "最小", "最大"],
        ),
        "",
        "## 最佳 / 最差样本",
        "",
        "### 收益最高 5 个",
        "",
        md_table(
            sample_rows(top),
            [
                "命中日",
                "代码",
                "名称",
                "高开",
                "收盘涨跌幅",
                f"{args.rise_days}日涨幅",
                "次日低开",
                "卖出方式",
                "收益率",
            ],
        ),
        "",
        "### 收益最低 5 个",
        "",
        md_table(
            sample_rows(bottom),
            [
                "命中日",
                "代码",
                "名称",
                "高开",
                "收盘涨跌幅",
                f"{args.rise_days}日涨幅",
                "次日低开",
                "卖出方式",
                "收益率",
            ],
        ),
        "",
    ]

    if plot_created:
        lines.extend(
            [
                "## 收益分布图",
                "",
                "![收益分布](return_distribution.png)",
                "",
            ]
        )

    lines.extend(
        [
            "## 口径说明",
            "",
            "- 高开用不复权 `raw_open / raw_pre_close - 1`。`raw_pre_close` 是交易所除权后的昨收，避免分红/送转造成假高开；普通交易日它与前一日原始收盘价相同。",
            "- T 日收盘涨跌幅用不复权 `raw_close / raw_pre_close - 1`，与高开使用同一昨收口径。",
            "- 可选的最低价条件用后复权 `low(T) >= close(T-1)`，默认不要求；开启后可复现早期“最低价不破昨收”口径。",
            "- T+1 买入低开用 `raw_open(T+1) / raw_pre_close(T+1) - 1`。`raw_pre_close(T+1)` 是交易所除权后的 T 日收盘，普通交易日等于 `close(T)`；CSV 同时保留直接对 T 日原始收盘的 `entry_gap_close_raw` 供核对。",
            "- 近 N 日涨幅和买卖收益用后复权价，等价于分红再投资；CSV 里同时保留“收益率(不复权)”列供核对。",
            "- T+1 买入为不复权限价口径：买入价 = `(1 + a - delta%) × close(T)`（`a = open(T+1)/close(T) - 1`）；`low(T+1) <= 买入价` 即视为按买入价全额成交，未考虑排队、部分成交和跌停价上的成交限制；日内未触及则跳过该样本。T+1 恰为除权日时 a 与买入价受除权影响明显偏低，大概率不成交。",
            "- 止盈分档涨幅用不复权 `close(T+1) / 买入价 - 1`（买入价与收盘价同为不复权，比值不受复权影响）；delta=0 时即原「日内涨幅 close/open」口径。",
            "- 卖出规则：先按 T+1 收盘对买入价涨幅分档得到逐样本止盈阈值（目标价 = 买入价 × (1+阈值)）；T+2 开盘涨幅（对买入价）超过阈值时按开盘卖；否则用 T+2 最高价判断盘中是否触及阈值，触及则按阈值止盈；否则持有到收盘。",
            "- 盘中止盈假设能在阈值价格精确成交，未考虑滑点、盘中路径和涨跌停无法成交的情况；负阈值（止损档）同理，按目标价精确成交估计。",
            "- 「T+2 开盘」「T+2 盘中」组的表现是规则触发的结果，不代表该组选股能力更强。",
            "- T+1、T+2 按 SSE 交易日历定位。个股在 T+1/T+2 停牌或行情尚未产生时，该样本不纳入收益统计。",
            "- delta=0 时未考虑 T+1 开盘涨停买不进；delta>0 时未考虑限价单排队与部分成交。全程未考虑跌停卖不出（仅备注）、涨跌停板成交限制、交易费用和滑点；这是理论价格下的样本级分布。",
            "- 卖出日跌停统计按 T+2 收盘价对 `stk_limit.down_limit` 判断，只做备注，不改变任何收益计算。",
            "- 同一天多个样本的收益可能重叠，本报告不是复利净值曲线，不能直接当作策略年化收益。",
            f"- 行情表中缺失 `adj_factor` 的行数：{missing_adj}。",
            "",
            "## 复现",
            "",
            "```powershell",
            "uv run quant hits update -s limit_up_shakeout --from-date 2018-01-01",
            "uv run python tmp_scripts/limitup_shakeout_filtered_returns.py",
            "```",
            "",
            "## 输出文件",
            "",
            "- `report.md`：本报告",
            "- `samples.csv`：全部命中样本、筛选标记和排除原因",
            "- `included_samples.csv`：最终纳入收益统计的样本",
            "- `summary.csv`：收益分布指标",
            "- `yearly_stats.csv`：分年统计",
            "- `gap_bucket_stats.csv`：按高开幅度分组统计",
            "- `exit_tier_stats.csv`：按止盈区间（T+1 收盘对买入价涨幅分档）分组统计",
            "- `run_meta.json`：运行参数和样本数",
            "- `return_distribution.png`：收益分布图（有样本时生成）",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 止盈分档先解析校验（错误早暴露，不等到查库之后）
    exit_tiers, tiers_source = resolve_exit_tiers(args)
    start = compact_date(args.start)
    if args.end is None or str(args.end).strip().lower() in ("", "latest", "none"):
        max_hit = db.scalar("SELECT MAX(trade_date) FROM hit_limit_up_shakeout")
        if not max_hit:
            raise SystemExit("hit_limit_up_shakeout 为空，请先执行 hits update")
        end = str(max_hit)
    else:
        end = compact_date(args.end)

    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")
    if args.rise_days <= 0:
        raise SystemExit("--rise-days 必须为正整数")
    if args.delta < 0:
        raise SystemExit("--delta 必须 >= 0（百分点；0 = 开盘价买入）")
    for name, rng in (
        ("--gap", args.gap),
        ("--close-chg", args.close_chg),
        ("--rise", args.rise),
        ("--entry-gap", args.entry_gap),
    ):
        if len(rng) != 2 or rng[0] > rng[1]:
            raise SystemExit(f"{name} 需要两个数，且 MIN <= MAX")

    print("=" * 90)
    print("limit_up_shakeout 筛选策略")
    print(f"  时间区间：{start} ~ {end}")
    print(f"  命中策略：limit_up_shakeout")
    print(f"  T日高开：{fmt_range(args.gap)}（闭区间）")
    print(f"  T日收盘涨跌幅：{fmt_range(args.close_chg)}（闭区间）")
    print(f"  近{args.rise_days}日涨幅：{fmt_range(args.rise)}（闭区间）")
    print(f"  T+1买入低开：{fmt_range(args.entry_gap)}（闭区间）")
    print(
        f"  T日最低价>=前一日收盘：{'要求' if args.require_low_support else '不要求'}"
    )
    if args.delta > 0:
        print(
            f"  买入规则：T+1 限价单，买入价 = (1 + a - {args.delta:g}%) × close(T)"
            "（a = open(T+1)/close(T)-1，不复权，即开盘下方 delta%）"
        )
        print("  成交条件：T+1 最低价 <= 买入价（按买入价成交）；未触及则跳过该样本")
    else:
        print("  买入规则：T+1 开盘价买入（delta=0，原规则）")
    print("  止盈规则：按 T+1 收盘对买入价涨幅（收盘/买入价-1，左开右闭）分档确定逐样本止盈阈值")
    print(f"    {format_tiers_summary(exit_tiers)}")
    print(f"    分档来源：{tiers_source}")
    print("    目标价 = 买入价×(1+阈值)；T+2 开盘高于目标价按开盘卖出；盘中触及目标价按目标价卖出；否则 T+2 收盘卖出")
    print("  费用口径：未扣手续费、印花税、滑点")
    print(f"  生成报告：{'是' if args.generate_report else '否'}")
    print("-" * 90)
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = load_hits(start, end)
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits)} 条")

    calendar = load_calendar()
    hits = attach_dates(hits, calendar, args.rise_days)

    needed_dates = pd.concat(
        [
            hits["hit_date"],
            hits["prev_date"],
            hits["rise_ref_date"],
            hits["t1_date"],
            hits["t2_date"],
        ],
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
        hits,
        prices,
        args.gap,
        args.close_chg,
        args.rise,
        args.rise_days,
        exit_tiers,
        args.entry_gap,
        delta_pct=args.delta,
        require_low_support=args.require_low_support,
    )
    included = samples[samples["included"]].copy()
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
    stats = distribution_stats(included["return_adj"])
    yearly = grouped_stats(included, "year")
    gap_stats = grouped_stats(included, "gap_bucket")
    tier_stats = grouped_stats(included, "exit_tier")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_filtered_returns_{datetime.now():%Y%m%d_%H%M%S}"
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
        gap_stats.rename(columns={"分组": "高开区间"}).to_csv(
            run_dir / "gap_bucket_stats.csv", index=False, encoding="utf-8-sig"
        )
        tier_stats.rename(columns={"分组": "止盈区间"}).to_csv(
            run_dir / "exit_tier_stats.csv", index=False, encoding="utf-8-sig"
        )

        plot_created = write_plot(included, run_dir / "return_distribution.png")

        meta = {
            "generated_at": generated_at,
            "strategy": "limit_up_shakeout",
            "hit_table": "hit_limit_up_shakeout",
            "start": start,
            "end": end,
            "gap_range": list(args.gap),
            "close_chg_range": list(args.close_chg),
            "rise_range": list(args.rise),
            "rise_days": args.rise_days,
            "exit_tiers": tiers_to_jsonable(exit_tiers),
            "exit_tiers_source": tiers_source,
            "entry_gap_range": list(args.entry_gap),
            "delta": args.delta,
            "buy_filled_samples": int(samples["buy_filled"].sum()),
            "buy_not_filled_samples": not_filled_n,
            "require_low_support": args.require_low_support,
            "hit_samples": len(samples),
            "passed_gap": int(samples["passed_gap"].sum()),
            "passed_close": int(samples["passed_close"].sum()),
            "passed_rise": int(samples["passed_rise"].sum()),
            "passed_low_support": int(samples["passed_low_support"].sum()),
            "included_samples": len(included),
            "open_exit_samples": int(included["exit_at_open"].sum())
            if len(included)
            else 0,
            "intraday_exit_samples": (
                int(included["exit_intraday"].sum()) if len(included) else 0
            ),
            "close_exit_samples": (
                int((~included["exit_at_open"] & ~included["exit_intraday"]).sum())
                if len(included)
                else 0
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
            samples,
            included,
            stats,
            yearly,
            gap_stats,
            tier_stats,
            exit_tiers,
            args,
            start,
            end,
            missing_adj,
            plot_created,
            generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    print("")
    print(f"最终纳入：{len(included)} 条 / 命中 {len(samples)} 条")
    if args.delta > 0:
        print(
            f"限价未成交跳过：{not_filled_n} 条"
            "（其余筛选满足但 T+1 未触及买入价）"
        )
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
