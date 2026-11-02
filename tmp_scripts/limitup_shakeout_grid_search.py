"""对 limit_up_shakeout 二次筛选参数做网格搜索并生成报告。

搜索维度：
- T 日高开区间 gap
- T 日收盘涨跌幅区间 close_chg
- 近 N 日涨幅区间 rise
- 回看交易日数 rise_days
- T+1 买入低开区间 entry_gap
- T+2 止盈阈值 open_exit

收益口径与 limitup_shakeout_filtered_returns.py 一致：
- T+1 开盘买入；
- T+2 开盘涨幅 > open_exit 时按 T+2 开盘卖出；
- 否则 T+2 最高价触及 open_exit 时按 open_exit 止盈；
- 否则 T+2 收盘卖出；
- 使用后复权价计算收益，未扣手续费、印花税和滑点；
- 不改变“卖出日即使跌停也能卖出”的假设，只在报告中统计卖出日跌停样本数。

用法：
    uv run python tmp_scripts/limitup_shakeout_grid_search.py
    uv run python tmp_scripts/limitup_shakeout_grid_search.py --report
    uv run python tmp_scripts/limitup_shakeout_grid_search.py --start 2020-01-01 --min-samples 40 --top 50

默认生成报告到 outputs/limitup_shakeout_grid_search_<timestamp>/。
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

DEFAULT_START = "20200101"
DEFAULT_END = "latest"
DEFAULT_MIN_SAMPLES = 30
DEFAULT_MIN_YEARS = 2
DEFAULT_ROBUST_SAMPLES = 100
DEFAULT_TOP_N = 30
DEFAULT_GENERATE_REPORT = True

# 当前参数基准，用于报告中的“当前参数 vs 网格最优”对比。
DEFAULT_GAP = (0.05, 0.11)
DEFAULT_CLOSE_CHG = (0.02, 0.11)
DEFAULT_RISE_DAYS = 10
DEFAULT_RISE = (-0.90, 0.90)
DEFAULT_OPEN_EXIT = 0.03
DEFAULT_ENTRY_GAP = (-0.11, 0.11)

# 网格候选。区间均为闭区间，单位是小数。
GAP_RANGES = [
    (0.00, 0.11),
    (0.00, 0.03),
    (0.03, 0.05),
    (0.05, 0.07),
    (0.07, 0.09),
    (0.09, 0.11),
    (0.00, 0.05),
    (0.05, 0.11),
]

CLOSE_CHG_RANGES = [
    (0.00, 0.11),
    (0.00, 0.02),
    (0.02, 0.04),
    (0.04, 0.06),
    (0.06, 0.08),
    (0.08, 0.11),
    (0.00, 0.04),
    (0.02, 0.06),
    (0.02, 0.11),
]

RISE_RANGES = [
    (-0.90, 0.90),
    (-0.20, 0.00),
    (0.00, 0.10),
    (0.10, 0.20),
    (0.20, 0.30),
    (0.30, 0.50),
    (0.00, 0.20),
    (0.10, 0.30),
    (0.50, 1.50),
]

RISE_DAYS_GRID = [5, 10, 20]

ENTRY_GAP_RANGES = [
    (-0.11, 0.11),
    (-0.11, 0.00),
    (-0.05, 0.00),
    (-0.03, 0.00),
    (-0.02, 0.02),
    (0.00, 0.05),
    (-0.05, 0.05),
]

OPEN_EXIT_GRID = [0.02, 0.03, 0.04, 0.05]

PRICE_CHUNK = 500
GAP_EPS = 1e-9
LIMIT_EPS = 1e-6

GRID_CN = {
    "gap_lo": "高开区间下限",
    "gap_hi": "高开区间上限",
    "close_lo": "收盘涨跌幅区间下限",
    "close_hi": "收盘涨跌幅区间上限",
    "rise_lo": "近N日涨幅区间下限",
    "rise_hi": "近N日涨幅区间上限",
    "rise_days": "回看交易日数",
    "entry_lo": "T+1低开区间下限",
    "entry_hi": "T+1低开区间上限",
    "open_exit": "止盈阈值",
    "n": "样本数",
    "n_years": "覆盖年份数",
    "min_year_n": "单年最少样本数",
    "mean_ret": "平均收益率",
    "median_ret": "中位数",
    "std_ret": "标准差",
    "win_rate": "胜率",
    "avg_win": "平均盈利",
    "avg_loss": "平均亏损",
    "profit_factor": "盈亏比",
    "min_ret": "最小收益率",
    "max_ret": "最大收益率",
    "score": "稳健评分",
    "total_return": "样本数×平均收益率",
    "qualified": "是否合格",
    "sell_limit_down_n": "卖出日跌停样本数",
    "sell_limit_down_pct": "卖出日跌停占比",
    "rank_score": "稳健评分排名",
    "rank_mean": "平均收益排名",
    "rank_total": "样本数×平均收益率排名",
}

TRADE_CN = {
    "hit_date": "命中日期",
    "ts_code": "股票代码",
    "name": "股票名称",
    "year": "年份",
    "gap_open": "高开幅度",
    "close_chg": "信号日收盘涨跌幅",
    "ret_rise": "近N日涨幅",
    "entry_gap": "T+1开盘低开幅度",
    "t2_date": "卖出日",
    "exit_type": "卖出方式",
    "return_adj": "收益率(后复权)",
    "return_raw": "收益率(不复权)",
    "sell_limit_down": "卖出日是否跌停",
    "return_pct": "收益率(%)",
    "return_raw_pct": "收益率(不复权,%)",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对 limit_up_shakeout 二次筛选参数做网格搜索并生成收益报告"
    )
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"命中日起始，默认 {DEFAULT_START}")
    parser.add_argument("--end", default=DEFAULT_END,
                        help=f"命中日结束，默认 {DEFAULT_END}，即命中表最新日期")
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES,
                        help=f"合格组合的最小样本数，默认 {DEFAULT_MIN_SAMPLES}")
    parser.add_argument("--min-years", type=int, default=DEFAULT_MIN_YEARS,
                        help=f"合格组合至少覆盖的年份数，默认 {DEFAULT_MIN_YEARS}")
    parser.add_argument("--robust-samples", type=int, default=DEFAULT_ROBUST_SAMPLES,
                        help=f"大样本候选的最小样本数，默认 {DEFAULT_ROBUST_SAMPLES}")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                        help=f"报告中展示的排名数量，默认 {DEFAULT_TOP_N}")
    parser.add_argument("--report", dest="generate_report", action="store_true",
                        help="生成 report.md / CSV 报告（默认开启）")
    parser.add_argument("--no-report", dest="generate_report", action="store_false",
                        help="只打印控制台结果，不写报告文件")
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument("--output-dir", default="outputs",
                        help="报告输出根目录，默认 outputs")
    return parser.parse_args(argv)


def compact_date(value: str) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


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
    return pd.DataFrame({
        "hit_date": df["trade_date"].astype(str),
        "ts_code": df["ts_code"].astype(str),
        "name": df["name"].fillna("").astype(str),
    })


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


def attach_dates(hits: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    date_pos = {d: i for i, d in enumerate(calendar)}
    out = hits.copy()
    out["cal_pos"] = out["hit_date"].map(date_pos)

    offsets: list[tuple[int, str]] = [
        (-1, "prev_date"),
        (1, "t1_date"),
        (2, "t2_date"),
    ]
    offsets.extend((-n, f"rise_ref_{n}") for n in RISE_DAYS_GRID)

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


def build_features(hits: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    samples = _merge_role(
        hits, prices, "hit_date",
        {
            "open": "t_open_raw",
            "close": "t_close_raw",
            "pre_close": "t_pre_close_raw",
            "adj_close": "t_adj_close",
        },
    )
    samples = _merge_role(
        samples, prices, "prev_date", {"close": "prev_close_raw"}
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
            "open": "t2_open_raw",
            "high": "t2_high_raw",
            "close": "t2_close_raw",
            "down_limit": "t2_down_limit",
            "adj_open": "t2_adj_open",
            "adj_high": "t2_adj_high",
            "adj_close": "t2_adj_close",
        },
    )
    for n in RISE_DAYS_GRID:
        samples = _merge_role(
            samples, prices, f"rise_ref_{n}",
            {"adj_close": f"rise_ref_adj_{n}"},
        )

    samples["gap_open"] = samples["t_open_raw"] / samples["t_pre_close_raw"] - 1.0
    samples["close_chg"] = samples["t_close_raw"] / samples["t_pre_close_raw"] - 1.0
    samples["entry_gap"] = samples["t1_open_raw"] / samples["t1_pre_close_raw"] - 1.0
    for n in RISE_DAYS_GRID:
        samples[f"ret_rise_{n}"] = (
            samples["t_adj_close"] / samples[f"rise_ref_adj_{n}"] - 1.0
        )

    entry_adj = pd.to_numeric(samples["t1_adj_open"], errors="coerce")
    entry_raw = pd.to_numeric(samples["t1_open_raw"], errors="coerce")
    t2_open_adj = pd.to_numeric(samples["t2_adj_open"], errors="coerce")
    t2_high_adj = pd.to_numeric(samples["t2_adj_high"], errors="coerce")
    t2_close_adj = pd.to_numeric(samples["t2_adj_close"], errors="coerce")
    t2_open_raw = pd.to_numeric(samples["t2_open_raw"], errors="coerce")
    t2_high_raw = pd.to_numeric(samples["t2_high_raw"], errors="coerce")
    t2_close_raw = pd.to_numeric(samples["t2_close_raw"], errors="coerce")

    open_ret = t2_open_adj / entry_adj - 1.0
    high_ret = t2_high_adj / entry_adj - 1.0
    close_ret = t2_close_adj / entry_adj - 1.0
    open_ret_raw = t2_open_raw / entry_raw - 1.0
    high_ret_raw = t2_high_raw / entry_raw - 1.0
    close_ret_raw = t2_close_raw / entry_raw - 1.0

    base_valid = (
        entry_adj.notna() & entry_adj.gt(0)
        & entry_raw.notna() & entry_raw.gt(0)
        & t2_open_adj.notna() & t2_high_adj.notna() & t2_close_adj.notna()
        & t2_open_raw.notna() & t2_high_raw.notna() & t2_close_raw.notna()
    )
    samples["base_valid"] = base_valid.fillna(False).astype(bool)

    for threshold in OPEN_EXIT_GRID:
        bp = int(round(threshold * 10000))
        ret_adj = np.where(
            open_ret.gt(threshold),
            open_ret,
            np.where(high_ret.ge(threshold), threshold, close_ret),
        )
        ret_raw = np.where(
            open_ret_raw.gt(threshold),
            open_ret_raw,
            np.where(high_ret_raw.ge(threshold), threshold, close_ret_raw),
        )
        exit_type = np.where(
            open_ret.gt(threshold),
            "T+2 开盘",
            np.where(high_ret.ge(threshold), "T+2 盘中", "T+2 收盘"),
        )
        samples[f"ret_adj_{bp}"] = np.where(samples["base_valid"], ret_adj, np.nan)
        samples[f"ret_raw_{bp}"] = np.where(samples["base_valid"], ret_raw, np.nan)
        samples[f"exit_type_{bp}"] = np.where(samples["base_valid"], exit_type, "")

    samples["sell_down_limit_missing"] = (
        samples["t2_down_limit"].isna() | samples["t2_down_limit"].le(0)
    )
    samples["sell_limit_down"] = (
        samples["t2_down_limit"].gt(0)
        & samples["t2_close_raw"].le(samples["t2_down_limit"] + LIMIT_EPS)
    ).fillna(False)
    samples["year"] = samples["hit_date"].str[:4]
    return samples


def prepare_arrays(samples: pd.DataFrame) -> dict[str, object]:
    arrays: dict[str, object] = {
        "gap": samples["gap_open"].to_numpy(dtype=float),
        "close": samples["close_chg"].to_numpy(dtype=float),
        "entry": samples["entry_gap"].to_numpy(dtype=float),
        "valid": samples["base_valid"].to_numpy(dtype=bool),
        "year": samples["year"].to_numpy(dtype=object),
        "sell_limit_down": samples["sell_limit_down"].to_numpy(dtype=bool),
    }
    for n in RISE_DAYS_GRID:
        arrays[f"rise_{n}"] = samples[f"ret_rise_{n}"].to_numpy(dtype=float)
    for threshold in OPEN_EXIT_GRID:
        bp = int(round(threshold * 10000))
        arrays[f"ret_{bp}"] = samples[f"ret_adj_{bp}"].to_numpy(dtype=float)
        arrays[f"exit_type_{bp}"] = samples[f"exit_type_{bp}"].to_numpy(dtype=object)
    return arrays


def build_mask(arrays: dict[str, object], gap_range: tuple[float, float],
               close_range: tuple[float, float],
               rise_range: tuple[float, float], rise_days: int,
               entry_range: tuple[float, float]) -> np.ndarray:
    gap = arrays["gap"]
    close = arrays["close"]
    entry = arrays["entry"]
    rise = arrays[f"rise_{rise_days}"]
    mask = arrays["valid"].copy()
    mask &= np.isfinite(gap) & (gap >= gap_range[0] - GAP_EPS) & (gap <= gap_range[1] + GAP_EPS)
    mask &= np.isfinite(close) & (close >= close_range[0] - GAP_EPS) & (close <= close_range[1] + GAP_EPS)
    mask &= np.isfinite(rise) & (rise >= rise_range[0] - GAP_EPS) & (rise <= rise_range[1] + GAP_EPS)
    mask &= np.isfinite(entry) & (entry >= entry_range[0] - GAP_EPS) & (entry <= entry_range[1] + GAP_EPS)
    return mask


def evaluate_mask(arrays: dict[str, object], mask: np.ndarray,
                  params: dict[str, object], open_exit: float,
                  min_samples: int, min_years: int) -> dict[str, object] | None:
    n = int(mask.sum())
    if n == 0:
        return None
    years_sub = arrays["year"][mask]
    year_values, year_counts = np.unique(years_sub, return_counts=True)
    n_years = int(len(year_values))
    min_year_n = int(year_counts.min()) if n_years else 0

    bp = int(round(open_exit * 10000))
    rets = np.asarray(arrays[f"ret_{bp}"][mask], dtype=float)
    rets = rets[np.isfinite(rets)]
    n_ret = len(rets)
    if n_ret < 2:
        return None

    mean_ret = float(rets.mean())
    std_ret = float(rets.std(ddof=1))
    wins = rets[rets > 0]
    losses = rets[rets < 0]
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    profit_factor = (
        avg_win / abs(avg_loss)
        if avg_win is not None and avg_loss not in (None, 0)
        else None
    )
    score = mean_ret - 0.5 * std_ret / np.sqrt(n_ret)
    sell_ld_n = int(arrays["sell_limit_down"][mask].sum())

    return {
        **params,
        "open_exit": open_exit,
        "n": n_ret,
        "n_years": n_years,
        "min_year_n": min_year_n,
        "mean_ret": mean_ret,
        "median_ret": float(np.median(rets)),
        "std_ret": std_ret,
        "win_rate": float((rets > 0).mean()),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "min_ret": float(rets.min()),
        "max_ret": float(rets.max()),
        "score": score,
        "qualified": bool(
            n_ret >= min_samples
            and n_years >= min_years
            and np.isfinite(score)
        ),
        "sell_limit_down_n": sell_ld_n,
        "sell_limit_down_pct": sell_ld_n / n_ret if n_ret else None,
    }


def run_grid(arrays: dict[str, object], args: argparse.Namespace) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    filter_combos = (
        len(GAP_RANGES) * len(CLOSE_CHG_RANGES) * len(RISE_RANGES)
        * len(RISE_DAYS_GRID) * len(ENTRY_GAP_RANGES)
    )
    done = 0
    for gap_range in GAP_RANGES:
        for close_range in CLOSE_CHG_RANGES:
            for rise_range in RISE_RANGES:
                for rise_days in RISE_DAYS_GRID:
                    for entry_range in ENTRY_GAP_RANGES:
                        done += 1
                        if done % 1000 == 0:
                            print(
                                f"  网格进度：{done:,}/{filter_combos:,} "
                                f"个筛选组合"
                            )
                        mask = build_mask(
                            arrays, gap_range, close_range, rise_range,
                            rise_days, entry_range,
                        )
                        if not mask.any():
                            continue
                        params = {
                            "gap_lo": gap_range[0],
                            "gap_hi": gap_range[1],
                            "close_lo": close_range[0],
                            "close_hi": close_range[1],
                            "rise_lo": rise_range[0],
                            "rise_hi": rise_range[1],
                            "rise_days": rise_days,
                            "entry_lo": entry_range[0],
                            "entry_hi": entry_range[1],
                        }
                        for open_exit in OPEN_EXIT_GRID:
                            row = evaluate_mask(
                                arrays, mask, params, open_exit,
                                args.min_samples, args.min_years,
                            )
                            if row is not None:
                                rows.append(row)
    results = pd.DataFrame(rows)
    if results.empty:
        raise SystemExit("没有产生任何有效组合，请检查命中表和网格参数")
    return results


def add_ranks(results: pd.DataFrame) -> pd.DataFrame:
    out = results.copy()
    out["total_return"] = out["n"] * out["mean_ret"]
    out["rank_score"] = np.nan
    out["rank_mean"] = np.nan
    out["rank_total"] = np.nan
    qualified = out["qualified"]
    out.loc[qualified, "rank_score"] = (
        out.loc[qualified, "score"].rank(ascending=False, method="min")
    )
    out.loc[qualified, "rank_mean"] = (
        out.loc[qualified, "mean_ret"].rank(ascending=False, method="min")
    )
    out.loc[qualified, "rank_total"] = (
        out.loc[qualified, "total_return"].rank(ascending=False, method="min")
    )
    return out


def find_baseline(arrays: dict[str, object],
                  args: argparse.Namespace) -> dict[str, object] | None:
    mask = build_mask(
        arrays, DEFAULT_GAP, DEFAULT_CLOSE_CHG, DEFAULT_RISE,
        DEFAULT_RISE_DAYS, DEFAULT_ENTRY_GAP,
    )
    params = {
        "gap_lo": DEFAULT_GAP[0],
        "gap_hi": DEFAULT_GAP[1],
        "close_lo": DEFAULT_CLOSE_CHG[0],
        "close_hi": DEFAULT_CLOSE_CHG[1],
        "rise_lo": DEFAULT_RISE[0],
        "rise_hi": DEFAULT_RISE[1],
        "rise_days": DEFAULT_RISE_DAYS,
        "entry_lo": DEFAULT_ENTRY_GAP[0],
        "entry_hi": DEFAULT_ENTRY_GAP[1],
    }
    return evaluate_mask(
        arrays, mask, params, DEFAULT_OPEN_EXIT,
        args.min_samples, args.min_years,
    )


def best_trades(samples: pd.DataFrame, arrays: dict[str, object],
                best: pd.Series) -> pd.DataFrame:
    gap_range = (float(best["gap_lo"]), float(best["gap_hi"]))
    close_range = (float(best["close_lo"]), float(best["close_hi"]))
    rise_range = (float(best["rise_lo"]), float(best["rise_hi"]))
    entry_range = (float(best["entry_lo"]), float(best["entry_hi"]))
    rise_days = int(best["rise_days"])
    open_exit = float(best["open_exit"])
    bp = int(round(open_exit * 10000))
    mask = build_mask(
        arrays, gap_range, close_range, rise_range, rise_days, entry_range,
    )
    cols = {
        "hit_date": "hit_date",
        "ts_code": "ts_code",
        "name": "name",
        "year": "year",
        "gap_open": "gap_open",
        "close_chg": "close_chg",
        f"ret_rise_{rise_days}": "ret_rise",
        "entry_gap": "entry_gap",
        "t2_date": "t2_date",
        f"exit_type_{bp}": "exit_type",
        f"ret_adj_{bp}": "return_adj",
        f"ret_raw_{bp}": "return_raw",
        "sell_limit_down": "sell_limit_down",
    }
    trades = samples.loc[mask, list(cols)].rename(columns=cols).copy()
    trades["return_pct"] = trades["return_adj"] * 100.0
    trades["return_raw_pct"] = trades["return_raw"] * 100.0
    trades = trades.sort_values(["hit_date", "ts_code"]).reset_index(drop=True)
    return trades


def yearly_table(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, group in trades.groupby("year", sort=True):
        rets = pd.to_numeric(group["return_adj"], errors="coerce").dropna()
        if rets.empty:
            continue
        rows.append({
            "年份": str(year),
            "样本数": len(rets),
            "平均收益率": float(rets.mean()),
            "中位数": float(rets.median()),
            "胜率": float((rets > 0).mean()),
            "最小收益率": float(rets.min()),
            "最大收益率": float(rets.max()),
            "标准差": float(rets.std(ddof=1)) if len(rets) > 1 else None,
        })
    return pd.DataFrame(rows)


def dimension_table(qualified: pd.DataFrame, column: str) -> pd.DataFrame:
    if qualified.empty:
        return pd.DataFrame()
    rows = []
    for key, group in qualified.groupby(column, sort=True):
        rows.append({
            "取值": str(key),
            "组合数": len(group),
            "平均样本数": float(group["n"].mean()),
            "平均收益(组合均值)": float(group["mean_ret"].mean()),
            "中位收益(组合中位数)": float(group["mean_ret"].median()),
            "最佳组合平均收益": float(group["mean_ret"].max()),
            "最佳稳健评分": float(group["score"].max()),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("平均收益(组合均值)", ascending=False).reset_index(drop=True)
    return out


def add_dimension_labels(results: pd.DataFrame) -> pd.DataFrame:
    out = results.copy()
    out["高开区间"] = [
        fmt_range((r.gap_lo, r.gap_hi)) for r in out.itertuples(index=False)
    ]
    out["收盘涨跌幅区间"] = [
        fmt_range((r.close_lo, r.close_hi)) for r in out.itertuples(index=False)
    ]
    out["近N日涨幅区间"] = [
        fmt_range((r.rise_lo, r.rise_hi)) for r in out.itertuples(index=False)
    ]
    out["T+1低开区间"] = [
        fmt_range((r.entry_lo, r.entry_hi)) for r in out.itertuples(index=False)
    ]
    out["回看天数"] = out["rise_days"].astype(str)
    out["止盈阈值"] = out["open_exit"].map(lambda x: fmt_pct(x, signed=False))
    return out


def best_param_rows(best: pd.Series) -> list[dict[str, object]]:
    return [
        {"参数": "T 日高开区间", "取值": fmt_range((best["gap_lo"], best["gap_hi"]))},
        {"参数": "T 日收盘涨跌幅区间", "取值": fmt_range((best["close_lo"], best["close_hi"]))},
        {"参数": "近 N 日涨幅区间", "取值": fmt_range((best["rise_lo"], best["rise_hi"]))},
        {"参数": "回看交易日数 N", "取值": str(int(best["rise_days"]))},
        {"参数": "T+1 买入低开区间", "取值": fmt_range((best["entry_lo"], best["entry_hi"]))},
        {"参数": "T+2 止盈阈值", "取值": fmt_pct(best["open_exit"], signed=False)},
    ]


def best_metric_rows(best: pd.Series) -> list[dict[str, object]]:
    return [
        {"指标": "样本数", "数值": str(int(best["n"]))},
        {"指标": "覆盖年份数", "数值": str(int(best["n_years"]))},
        {"指标": "单年最少样本数", "数值": str(int(best["min_year_n"]))},
        {"指标": "平均收益率", "数值": fmt_pct(best["mean_ret"])},
        {"指标": "中位数", "数值": fmt_pct(best["median_ret"])},
        {"指标": "标准差", "数值": fmt_pct(best["std_ret"], signed=False)},
        {"指标": "胜率", "数值": fmt_pct(best["win_rate"], signed=False)},
        {"指标": "平均盈利", "数值": fmt_pct(best["avg_win"])},
        {"指标": "平均亏损", "数值": fmt_pct(best["avg_loss"])},
        {"指标": "盈亏比", "数值": fmt_num(best["profit_factor"])},
        {"指标": "最小收益率", "数值": fmt_pct(best["min_ret"])},
        {"指标": "最大收益率", "数值": fmt_pct(best["max_ret"])},
        {"指标": "稳健评分", "数值": fmt_pct(best["score"])},
        {"指标": "卖出日跌停样本数", "数值": (
            f"{int(best['sell_limit_down_n'])} 条"
            f"（{fmt_pct(best['sell_limit_down_pct'], signed=False)}）"
        )},
    ]


def top_param_summary(top: pd.DataFrame) -> list[dict[str, object]]:
    if top.empty:
        return []
    labels = {
        "T 日高开区间": top.apply(
            lambda r: fmt_range((r.gap_lo, r.gap_hi)), axis=1
        ),
        "T 日收盘涨跌幅区间": top.apply(
            lambda r: fmt_range((r.close_lo, r.close_hi)), axis=1
        ),
        "近 N 日涨幅区间": top.apply(
            lambda r: fmt_range((r.rise_lo, r.rise_hi)), axis=1
        ),
        "回看交易日数 N": top["rise_days"].astype(int).astype(str),
        "T+1 买入低开区间": top.apply(
            lambda r: fmt_range((r.entry_lo, r.entry_hi)), axis=1
        ),
        "T+2 止盈阈值": top["open_exit"].map(
            lambda x: fmt_pct(x, signed=False)
        ),
    }
    rows = []
    for label, values in labels.items():
        counts = values.value_counts()
        if counts.empty:
            continue
        rows.append({
            "维度": label,
            "前10组合最常见取值": str(counts.index[0]),
            "出现次数": int(counts.iloc[0]),
        })
    return rows


def top_rows(df: pd.DataFrame, top_n: int) -> list[dict[str, object]]:
    rows = []
    for rank, row in enumerate(df.head(top_n).itertuples(index=False), start=1):
        rows.append({
            "排名": rank,
            "高开": fmt_range((row.gap_lo, row.gap_hi)),
            "收盘涨跌": fmt_range((row.close_lo, row.close_hi)),
            "N日涨幅": fmt_range((row.rise_lo, row.rise_hi)),
            "N": int(row.rise_days),
            "T+1低开": fmt_range((row.entry_lo, row.entry_hi)),
            "止盈": fmt_pct(row.open_exit, signed=False),
            "样本数": int(row.n),
            "年份数": int(row.n_years),
            "平均收益": fmt_pct(row.mean_ret),
            "中位数": fmt_pct(row.median_ret),
            "胜率": fmt_pct(row.win_rate, signed=False),
            "标准差": fmt_pct(row.std_ret, signed=False),
            "稳健评分": fmt_pct(row.score),
        })
    return rows


def total_rows(df: pd.DataFrame, top_n: int) -> list[dict[str, object]]:
    rows = []
    for rank, row in enumerate(df.head(top_n).itertuples(index=False), start=1):
        rows.append({
            "排名": rank,
            "高开": fmt_range((row.gap_lo, row.gap_hi)),
            "收盘涨跌": fmt_range((row.close_lo, row.close_hi)),
            "N日涨幅": fmt_range((row.rise_lo, row.rise_hi)),
            "N": int(row.rise_days),
            "T+1低开": fmt_range((row.entry_lo, row.entry_hi)),
            "止盈": fmt_pct(row.open_exit, signed=False),
            "样本数": int(row.n),
            "平均收益": fmt_pct(row.mean_ret),
            "样本收益合计(单利)": fmt_pct(row.total_return),
            "中位数": fmt_pct(row.median_ret),
            "胜率": fmt_pct(row.win_rate, signed=False),
            "稳健评分": fmt_pct(row.score),
        })
    return rows


def build_report(results: pd.DataFrame, qualified: pd.DataFrame,
                 robust: pd.DataFrame, best: pd.Series,
                 baseline: dict[str, object] | None,
                 trades: pd.DataFrame, yearly: pd.DataFrame,
                 dimension_tables: dict[str, pd.DataFrame],
                 args: argparse.Namespace, start: str, end: str,
                 generated_at: str) -> str:
    total_filters = (
        len(GAP_RANGES) * len(CLOSE_CHG_RANGES) * len(RISE_RANGES)
        * len(RISE_DAYS_GRID) * len(ENTRY_GAP_RANGES)
    )
    total_combos = total_filters * len(OPEN_EXIT_GRID)
    exit_counts = trades["exit_type"].value_counts()
    exit_rows = [
        {
            "卖出方式": str(name),
            "样本数": int(count),
            "占比": fmt_pct(count / len(trades), signed=False),
        }
        for name, count in exit_counts.items()
    ]

    def sample_rows(df: pd.DataFrame) -> list[dict[str, object]]:
        rows = []
        for row in df.itertuples(index=False):
            rows.append({
                "命中日": row.hit_date,
                "代码": row.ts_code,
                "名称": row.name,
                "高开": fmt_pct(row.gap_open),
                "收盘涨跌": fmt_pct(row.close_chg),
                "N日涨幅": fmt_pct(row.ret_rise),
                "T+1低开": fmt_pct(row.entry_gap),
                "卖出方式": row.exit_type,
                "收益率": fmt_pct(row.return_adj),
            })
        return rows

    top_score = qualified.sort_values(
        ["score", "mean_ret", "n"], ascending=[False, False, False]
    )
    top_mean = qualified.sort_values(
        ["mean_ret", "score", "n"], ascending=[False, False, False]
    )
    top_total = qualified.sort_values(
        ["total_return", "mean_ret", "n"], ascending=[False, False, False]
    )
    robust_top = robust.sort_values(
        ["score", "mean_ret", "n"], ascending=[False, False, False]
    ) if not robust.empty else robust
    top_summary_rows = top_param_summary(top_score.head(10))
    top_trades = trades.nlargest(5, "return_adj") if not trades.empty else trades
    bottom_trades = trades.nsmallest(5, "return_adj") if not trades.empty else trades

    baseline_rows = []
    if baseline is not None:
        for label, key, formatter in (
            ("样本数", "n", lambda x: str(int(x))),
            ("覆盖年份数", "n_years", lambda x: str(int(x))),
            ("平均收益率", "mean_ret", fmt_pct),
            ("中位数", "median_ret", fmt_pct),
            ("胜率", "win_rate", lambda x: fmt_pct(x, signed=False)),
            ("标准差", "std_ret", lambda x: fmt_pct(x, signed=False)),
            ("稳健评分", "score", fmt_pct),
        ):
            base_val = baseline.get(key)
            best_val = best.get(key)
            diff = (
                float(best_val) - float(base_val)
                if base_val is not None and best_val is not None
                and np.isfinite(float(base_val)) and np.isfinite(float(best_val))
                else None
            )
            if key in ("n", "n_years"):
                diff_text = "-" if diff is None else f"{int(diff):+d}"
            else:
                diff_text = "-" if diff is None else fmt_pct(diff)
            baseline_rows.append({
                "指标": label,
                "当前参数": formatter(base_val) if base_val is not None else "-",
                "网格最优": formatter(best_val) if best_val is not None else "-",
                "差异": diff_text,
            })

    lines = [
        "# limit_up_shakeout 参数网格搜索报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 命中表：`hit_limit_up_shakeout`",
        f"- 命中区间：`{start}` ~ `{end}`",
        f"- 网格组合数：{total_combos:,}（筛选组合 {total_filters:,} × 止盈阈值 {len(OPEN_EXIT_GRID)}）",
        f"- 合格标准：样本数 >= {args.min_samples}，覆盖年份数 >= {args.min_years}",
        "- 主排序：`稳健评分 = 平均收益率 - 0.5 × 标准差 / sqrt(样本数)`",
        "- 收益口径：T+1 开盘买入，T+2 开盘/盘中/收盘卖出；后复权毛收益，未扣手续费、印花税和滑点",
        "- 卖出日跌停：只做统计备注，不改变收益计算，仍假设跌停也能卖出",
        "",
        "## 结论摘要",
        "",
        "### 网格最优组合（按稳健评分）",
        "",
        md_table(best_param_rows(best), ["参数", "取值"]),
        "",
        md_table(best_metric_rows(best), ["指标", "数值"]),
        "",
        (
            f"> 提示：最优组合共有 {int(best['n'])} 个样本，覆盖 {int(best['n_years'])} 年，"
            f"单年最少 {int(best['min_year_n'])} 个样本。样本量偏小，结果对个别交易较敏感，"
            "需要先做样本外验证再使用。"
        ),
        "",
        "### 前 10 组合的参数聚集",
        "",
        md_table(
            top_summary_rows,
            ["维度", "前10组合最常见取值", "出现次数"],
        ),
        "",
        "### 当前参数基准对比",
        "",
        md_table(baseline_rows, ["指标", "当前参数", "网格最优", "差异"]),
        "",
        "> 当前参数基准：高开 `[5.00%, 11.00%]`、收盘涨跌幅 `[2.00%, 11.00%]`、近 10 日涨幅不限制、T+1 低开不限制、止盈 `3.00%`。",
        "",
        f"## 按稳健评分排名（前 {min(args.top, len(top_score))}）",
        "",
        md_table(
            top_rows(top_score, args.top),
            ["排名", "高开", "收盘涨跌", "N日涨幅", "N", "T+1低开", "止盈",
             "样本数", "年份数", "平均收益", "中位数", "胜率", "标准差", "稳健评分"],
        ),
        "",
        f"## 按平均收益率排名（前 {min(args.top, len(top_mean))}）",
        "",
        md_table(
            top_rows(top_mean, args.top),
            ["排名", "高开", "收盘涨跌", "N日涨幅", "N", "T+1低开", "止盈",
             "样本数", "年份数", "平均收益", "中位数", "胜率", "标准差", "稳健评分"],
        ),
        "",
        "## 按“样本数 × 平均收益率”排名（前 10）",
        "",
        "> 该指标等于把纳入样本的收益率直接相加（单利），会强烈偏向样本数多的宽泛参数；它回答的是“总收益体量最大”，不是“单笔收益最高”。",
        "",
        md_table(
            total_rows(top_total, min(10, len(top_total))),
            ["排名", "高开", "收盘涨跌", "N日涨幅", "N", "T+1低开", "止盈",
             "样本数", "平均收益", "样本收益合计(单利)", "中位数", "胜率", "稳健评分"],
        ),
        "",
        f"## 大样本候选（样本数 >= {args.robust_samples}，按稳健评分）",
        "",
        "> 如果最优组合样本量偏小，可优先观察这一组；样本更多，但收益通常低于最高评分组合。",
        "",
        md_table(
            top_rows(robust_top, min(10, len(robust_top))),
            ["排名", "高开", "收盘涨跌", "N日涨幅", "N", "T+1低开", "止盈",
             "样本数", "年份数", "平均收益", "中位数", "胜率", "标准差", "稳健评分"],
        ),
        "",
        "## 参数主效应（仅合格组合）",
        "",
        "> 每个取值下汇总所有合格组合；用于观察哪类参数区间整体更稳定，不等同于单组合最优。",
        "",
    ]

    for title, table in dimension_tables.items():
        table_rows = []
        for row in table.to_dict("records"):
            table_rows.append({
                "取值": row["取值"],
                "组合数": int(row["组合数"]),
                "平均样本数": f"{row['平均样本数']:.1f}",
                "平均收益(组合均值)": fmt_pct(row["平均收益(组合均值)"]),
                "中位收益": fmt_pct(row["中位收益(组合中位数)"]),
                "最佳组合平均收益": fmt_pct(row["最佳组合平均收益"]),
                "最佳稳健评分": fmt_pct(row["最佳稳健评分"]),
            })
        lines.extend([
            f"### {title}",
            "",
            md_table(
                table_rows,
                ["取值", "组合数", "平均样本数", "平均收益(组合均值)",
                 "中位收益", "最佳组合平均收益", "最佳稳健评分"],
            ),
            "",
        ])

    lines.extend([
        "## 最优组合分年统计",
        "",
        md_table(
            [
                {
                    "年份": row.年份,
                    "样本数": int(row.样本数),
                    "平均": fmt_pct(row.平均收益率),
                    "中位数": fmt_pct(row.中位数),
                    "胜率": fmt_pct(row.胜率, signed=False),
                    "最小": fmt_pct(row.最小收益率),
                    "最大": fmt_pct(row.最大收益率),
                    "标准差": fmt_pct(row.标准差, signed=False),
                }
                for row in yearly.itertuples(index=False)
            ],
            ["年份", "样本数", "平均", "中位数", "胜率", "最小", "最大", "标准差"],
        ),
        "",
        "## 最优组合卖出方式",
        "",
        md_table(exit_rows, ["卖出方式", "样本数", "占比"]),
        "",
        "### 收益最高 5 个样本",
        "",
        md_table(
            sample_rows(top_trades),
            ["命中日", "代码", "名称", "高开", "收盘涨跌", "N日涨幅",
             "T+1低开", "卖出方式", "收益率"],
        ),
        "",
        "### 收益最低 5 个样本",
        "",
        md_table(
            sample_rows(bottom_trades),
            ["命中日", "代码", "名称", "高开", "收盘涨跌", "N日涨幅",
             "T+1低开", "卖出方式", "收益率"],
        ),
        "",
        "## 口径与风险提示",
        "",
        "- 网格搜索是在同一段历史上反复比较参数，存在明显的数据挖掘和过拟合风险；本报告的最优组合应视为候选，而不是可直接实盘的参数。",
        "- 建议用滚动窗口或留出最近 1-2 年做样本外验证，并观察最优参数在相邻区间的稳定性。",
        "- 收益为后复权毛收益，未扣手续费、印花税、滑点和冲击成本。",
        "- 未建模 T+1 开盘涨停买不进、跌停卖不出、停牌无法交易；卖日跌停只做统计，不改变收益。",
        "- 同一天多个样本的收益可能重叠，本报告不是复利净值曲线，也不能直接当作策略年化收益。",
        "- 参数主效应表是按合格组合汇总的，组合之间共享样本，不能当作独立统计检验。",
        "",
        "## 复现",
        "",
        "```powershell",
        f"uv run python tmp_scripts/limitup_shakeout_grid_search.py --start {args.start} --end {args.end} --min-samples {args.min_samples} --min-years {args.min_years} --robust-samples {args.robust_samples} --top {args.top}",
        "```",
        "",
        "## 输出文件",
        "",
        "- `report.md`：本报告",
        "- `grid_results.csv`：全部有效网格组合及指标",
        "- `best_combo_trades.csv`：最优组合的逐笔样本",
        "- `best_combo_yearly.csv`：最优组合分年统计",
        "- `dimension_stats.csv`：参数主效应汇总",
        "- `run_meta.json`：运行参数、网格维度和样本数",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    if args.min_samples < 1:
        raise SystemExit("--min-samples 必须 >= 1")
    if args.min_years < 1:
        raise SystemExit("--min-years 必须 >= 1")
    if args.robust_samples < 1:
        raise SystemExit("--robust-samples 必须 >= 1")
    if args.top < 1:
        raise SystemExit("--top 必须 >= 1")

    print("=" * 90)
    print("limit_up_shakeout 参数网格搜索")
    print(f"  命中区间：{start} ~ {end}")
    print(f"  合格标准：样本数 >= {args.min_samples}，年份数 >= {args.min_years}")
    print(f"  网格维度：高开 {len(GAP_RANGES)} × 收盘 {len(CLOSE_CHG_RANGES)}"
          f" × 涨幅 {len(RISE_RANGES)} × 回看天数 {len(RISE_DAYS_GRID)}"
          f" × T+1低开 {len(ENTRY_GAP_RANGES)} × 止盈 {len(OPEN_EXIT_GRID)}")
    print("-" * 90)

    hits = load_hits(start, end)
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条")

    calendar = load_calendar()
    hits = attach_dates(hits, calendar)

    needed_dates = pd.concat(
        [hits["hit_date"], hits["prev_date"], hits["t1_date"], hits["t2_date"]]
        + [hits[f"rise_ref_{n}"] for n in RISE_DAYS_GRID],
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

    samples = build_features(hits, prices)
    arrays = prepare_arrays(samples)
    print(f"可计算收益样本：{int(samples['base_valid'].sum()):,} / {len(samples):,}")

    print("开始网格搜索 ...")
    results = run_grid(arrays, args)
    results = add_ranks(results)
    qualified = results[results["qualified"]].copy()
    if qualified.empty:
        raise SystemExit(
            "没有组合满足最小样本/年份要求，请降低 --min-samples 或 --min-years"
        )

    robust = qualified[qualified["n"] >= args.robust_samples].copy()
    print(f"有效网格组合：{len(results):,}，合格组合：{len(qualified):,}")
    print(f"大样本候选（样本数 >= {args.robust_samples}）：{len(robust):,}")
    best = qualified.sort_values(
        ["score", "mean_ret", "n"], ascending=[False, False, False]
    ).iloc[0]
    total_best = qualified.sort_values(
        ["total_return", "mean_ret", "n"], ascending=[False, False, False]
    ).iloc[0]
    baseline = find_baseline(arrays, args)
    trades = best_trades(samples, arrays, best)
    yearly = yearly_table(trades)

    dimension_tables: dict[str, pd.DataFrame] = {}
    labeled = add_dimension_labels(qualified)
    for title, column in (
        ("按高开区间", "高开区间"),
        ("按收盘涨跌幅区间", "收盘涨跌幅区间"),
        ("按近 N 日涨幅区间", "近N日涨幅区间"),
        ("按回看天数", "回看天数"),
        ("按 T+1 低开区间", "T+1低开区间"),
        ("按止盈阈值", "止盈阈值"),
    ):
        table = dimension_table(labeled, column)
        if not table.empty:
            dimension_tables[title] = table

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir: Path | None = None
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_grid_search_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        output_results = results.rename(columns=GRID_CN)
        output_results.to_csv(
            run_dir / "grid_results.csv", index=False, encoding="utf-8-sig"
        )
        trades.rename(columns=TRADE_CN).to_csv(
            run_dir / "best_combo_trades.csv", index=False, encoding="utf-8-sig"
        )
        yearly.to_csv(
            run_dir / "best_combo_yearly.csv", index=False, encoding="utf-8-sig"
        )
        if dimension_tables:
            dimension_out = pd.concat(
                [
                    table.assign(维度=title)
                    for title, table in dimension_tables.items()
                ],
                ignore_index=True,
            )
            dimension_out.to_csv(
                run_dir / "dimension_stats.csv", index=False, encoding="utf-8-sig"
            )

        meta = {
            "generated_at": generated_at,
            "strategy": "limit_up_shakeout",
            "hit_table": "hit_limit_up_shakeout",
            "start": start,
            "end": end,
            "min_samples": args.min_samples,
            "min_years": args.min_years,
            "robust_samples": args.robust_samples,
            "top_n": args.top,
            "objective": "mean_ret - 0.5 * std_ret / sqrt(n)",
            "grid": {
                "gap_ranges": GAP_RANGES,
                "close_chg_ranges": CLOSE_CHG_RANGES,
                "rise_ranges": RISE_RANGES,
                "rise_days": RISE_DAYS_GRID,
                "entry_gap_ranges": ENTRY_GAP_RANGES,
                "open_exit": OPEN_EXIT_GRID,
            },
            "total_filter_combos": (
                len(GAP_RANGES) * len(CLOSE_CHG_RANGES) * len(RISE_RANGES)
                * len(RISE_DAYS_GRID) * len(ENTRY_GAP_RANGES)
            ),
            "total_grid_combos": (
                len(GAP_RANGES) * len(CLOSE_CHG_RANGES) * len(RISE_RANGES)
                * len(RISE_DAYS_GRID) * len(ENTRY_GAP_RANGES)
                * len(OPEN_EXIT_GRID)
            ),
            "valid_grid_combos": len(results),
            "qualified_combos": len(qualified),
            "robust_combos": len(robust),
            "best": {
                key: (float(value) if isinstance(value, (np.floating, float)) else value)
                for key, value in best.to_dict().items()
            },
            "best_total_return": {
                key: (float(value) if isinstance(value, (np.floating, float)) else value)
                for key, value in total_best.to_dict().items()
            },
            "baseline": baseline,
            "hit_samples": len(samples),
            "valid_samples": int(samples["base_valid"].sum()),
            "missing_adj_factor_rows": missing_adj,
            "prices_rows": len(prices),
            "best_combo_trades": len(trades),
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        report = build_report(
            results, qualified, robust, best, baseline, trades, yearly,
            dimension_tables, args, start, end, generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")

    print("")
    print("最佳组合（按稳健评分）：")
    print(
        f"  高开 {fmt_range((best['gap_lo'], best['gap_hi']))}，"
        f"收盘涨跌 {fmt_range((best['close_lo'], best['close_hi']))}，"
        f"近{int(best['rise_days'])}日涨幅 {fmt_range((best['rise_lo'], best['rise_hi']))}"
    )
    print(
        f"  T+1低开 {fmt_range((best['entry_lo'], best['entry_hi']))}，"
        f"止盈 {fmt_pct(best['open_exit'], signed=False)}"
    )
    print(
        f"  样本数 {int(best['n'])}，年份数 {int(best['n_years'])}，"
        f"平均收益 {fmt_pct(best['mean_ret'])}，中位数 {fmt_pct(best['median_ret'])}，"
        f"胜率 {fmt_pct(best['win_rate'], signed=False)}，评分 {fmt_pct(best['score'])}"
    )
    print(
        f"  卖出日跌停 {int(best['sell_limit_down_n'])} 条"
        f"（{fmt_pct(best['sell_limit_down_pct'], signed=False)}）"
    )
    print("")
    print("样本数 × 平均收益率最高：")
    print(
        f"  高开 {fmt_range((total_best['gap_lo'], total_best['gap_hi']))}，"
        f"收盘涨跌 {fmt_range((total_best['close_lo'], total_best['close_hi']))}，"
        f"近{int(total_best['rise_days'])}日涨幅 "
        f"{fmt_range((total_best['rise_lo'], total_best['rise_hi']))}"
    )
    print(
        f"  T+1低开 {fmt_range((total_best['entry_lo'], total_best['entry_hi']))}，"
        f"止盈 {fmt_pct(total_best['open_exit'], signed=False)}，"
        f"样本数 {int(total_best['n'])}，平均收益 {fmt_pct(total_best['mean_ret'])}，"
        f"样本收益合计 {fmt_pct(total_best['total_return'])}"
    )
    if not robust.empty:
        robust_best = robust.sort_values(
            ["score", "mean_ret", "n"], ascending=[False, False, False]
        ).iloc[0]
        print("")
        print(f"大样本最佳候选（样本数 >= {args.robust_samples}）：")
        print(
            f"  高开 {fmt_range((robust_best['gap_lo'], robust_best['gap_hi']))}，"
            f"收盘涨跌 {fmt_range((robust_best['close_lo'], robust_best['close_hi']))}，"
            f"近{int(robust_best['rise_days'])}日涨幅 "
            f"{fmt_range((robust_best['rise_lo'], robust_best['rise_hi']))}"
        )
        print(
            f"  T+1低开 {fmt_range((robust_best['entry_lo'], robust_best['entry_hi']))}，"
            f"止盈 {fmt_pct(robust_best['open_exit'], signed=False)}，"
            f"样本数 {int(robust_best['n'])}，平均收益 {fmt_pct(robust_best['mean_ret'])}，"
            f"评分 {fmt_pct(robust_best['score'])}"
        )
    if run_dir is not None:
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（--no-report）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
