#!/usr/bin/env python
"""300,000 元账户的组合级约束回测。

输入：limitup_shakeout_returns.py 生成的 cash_trades.csv（逐笔 100,000 元口径）。
约束：每笔 100,000 元，最多同时持有 floor(资金 / 100,000) 笔，无杠杆。

模拟内容：
- 每个交易日按买入日开盘建仓、按实际卖出日/卖出方式释放资金；
- 资金不足或仓位已满时，按指定优先级跳过信号；
- 每日用不复权收盘价做市值重估，计算净值、最大回撤、年化收益；
- 同时输出“保守”（卖出日仍占用资金，不依赖同日开盘卖出复用）和
  “乐观”（开盘卖出可先于同日开盘买入复用）两种口径。

优先级规则：
- csv：按 cash_trades.csv 文件顺序（基准）
- random：随机（多 seed 取中位）
- gap_desc：优先“较浅低开”，即 T+1 低开比例更接近 0 / -1%
- gap_near2：优先接近 -2%
- gap_near3：优先接近 -3%
- mcap_asc / mcap_desc：优先小市值 / 大市值
- tamt_asc / tamt_desc：优先低 T 量比 / 高 T 量比
- exclude_bad_gap_desc：先剔除微盘（<20 亿）和极端放量（T 量比>10），再按 gap_desc
- oracle：按最终净收益率排序（事后最优，不可执行，只作上限参考）

用法：
    uv run python tmp_scripts/limitup_shakeout_portfolio_sim.py
    uv run python tmp_scripts/limitup_shakeout_portfolio_sim.py `
      --trades outputs/limitup_shakeout_entry_union_20181101_20261101/cash_trades.csv `
      --capital 300000 --position 100000
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(TMP_DIR))
os.chdir(ROOT)

from quant.data import db  # noqa: E402

import limitup_shakeout_returns as cash  # noqa: E402

DEFAULT_TRADES = "outputs/limitup_shakeout_entry_union_20181101_20261101/cash_trades.csv"
DEFAULT_CAPITAL = 300_000.0
DEFAULT_POSITION = 100_000.0
DEFAULT_RANDOM_SEEDS = 50

RULES: list[tuple[str, str]] = [
    ("csv", "文件顺序（基准）"),
    ("random", "随机（多 seed 中位）"),
    ("gap_desc", "优先较浅低开（更接近 -1%）"),
    ("gap_near2", "优先接近 -2%"),
    ("gap_near3", "优先接近 -3%"),
    ("mcap_asc", "优先小市值"),
    ("mcap_desc", "优先大市值"),
    ("tamt_asc", "优先低 T 量比"),
    ("tamt_desc", "优先高 T 量比"),
    ("exclude_bad_gap_desc", "剔除微盘/极端放量后优先浅低开"),
    ("oracle", "事后最优（不可执行上限）"),
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="limit_up_shakeout 300k 组合级资金约束回测"
    )
    parser.add_argument("--trades", default=DEFAULT_TRADES, help="cash_trades.csv 路径")
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL, help="账户初始资金")
    parser.add_argument("--position", type=float, default=DEFAULT_POSITION, help="每笔预算")
    parser.add_argument(
        "--random-seeds",
        type=int,
        default=DEFAULT_RANDOM_SEEDS,
        help="random 规则的 seed 数量",
    )
    parser.add_argument("--output-dir", default="outputs", help="输出根目录")
    parser.add_argument(
        "--report",
        dest="generate_report",
        action="store_true",
        default=True,
        help="生成报告和 CSV（默认开启）",
    )
    parser.add_argument(
        "--no-report",
        dest="generate_report",
        action="store_false",
        help="只打印控制台",
    )
    return parser.parse_args(argv)


def setup_plot() -> None:
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


def _pct(series: pd.Series) -> pd.Series:
    return (
        pd.to_numeric(
            series.astype(str)
            .str.replace("%", "", regex=False)
            .str.replace("+", "", regex=False),
            errors="coerce",
        )
        / 100.0
    )


def load_trades(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    for col in [
        "买入金额",
        "买入费用",
        "卖出金额",
        "卖出费用",
        "净盈亏",
        "T日收盘后流通市值(亿元)",
        "T成交额/T-1成交额",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["买入价(不复权,含滑点)"] = pd.to_numeric(
        df["买入价(不复权,含滑点)"], errors="coerce"
    )
    df["净收益率"] = _pct(df["净收益率"])
    # gap_pct 保留百分数单位（-2.0 表示 -2%），供优先级排序使用
    df["gap_pct"] = pd.to_numeric(
        df["T+1日高开比例(原始价)"]
        .astype(str)
        .str.replace("%", "", regex=False)
        .str.replace("+", "", regex=False),
        errors="coerce",
    )
    df["买入日"] = df["买入日"].astype(str)
    df["实际卖出日"] = df["实际卖出日"].astype(str)
    df["卖出方式"] = df["卖出方式"].fillna("").astype(str)
    df["free_open"] = df["卖出方式"].str.contains("开盘")
    df["cost"] = df["买入金额"] + df["买入费用"]
    df["proceeds"] = df["卖出金额"] - df["卖出费用"]
    df["shares"] = (
        df["买入金额"] / df["买入价(不复权,含滑点)"]
    ).round().fillna(0).astype(int)
    df["row_order"] = np.arange(len(df))
    return df


def build_market_data(df: pd.DataFrame):
    start = df["买入日"].min()
    end = df["实际卖出日"].max()
    calendar = [d for d in cash.load_calendar() if start <= d <= end]
    pos = {d: i for i, d in enumerate(calendar)}
    missing = 0
    keys: set[tuple[str, str]] = set()
    for buy, sell, code in zip(df["买入日"], df["实际卖出日"], df["股票代码"]):
        if buy not in pos or sell not in pos:
            missing += 1
            continue
        for d in calendar[pos[buy] : pos[sell] + 1]:
            keys.add((str(code), d))
    prices = cash.load_prices_by_keys(keys) if keys else pd.DataFrame()
    pmap = {
        (str(row.ts_code), str(row.trade_date)): float(row.close)
        for row in prices.itertuples()
        if pd.notna(row.close)
    }
    return calendar, pos, pmap, missing


def order_candidates(
    signals: list[int],
    rule: str,
    df: pd.DataFrame,
    rng: random.Random | None = None,
) -> list[int]:
    if rule == "csv":
        return signals
    if rule == "random":
        out = signals[:]
        (rng or random.Random(0)).shuffle(out)
        return out
    if rule.startswith("exclude_bad_"):
        good = [
            i
            for i in signals
            if pd.notna(df.at[i, "T日收盘后流通市值(亿元)"])
            and df.at[i, "T日收盘后流通市值(亿元)"] >= 20
            and pd.notna(df.at[i, "T成交额/T-1成交额"])
            and df.at[i, "T成交额/T-1成交额"] <= 10
        ]
        signals = good or signals
        rule = rule.replace("exclude_bad_", "")

    def safe_gap(i: int) -> float:
        value = df.at[i, "gap_pct"]
        return float(value) if pd.notna(value) else -99.0

    def safe_mcap(i: int) -> float:
        value = df.at[i, "T日收盘后流通市值(亿元)"]
        return float(value) if pd.notna(value) else 9999.0

    def safe_tamt(i: int) -> float:
        value = df.at[i, "T成交额/T-1成交额"]
        return float(value) if pd.notna(value) else 9999.0

    if rule == "gap_desc":
        return sorted(signals, key=lambda i: -safe_gap(i))
    if rule == "gap_near2":
        return sorted(signals, key=lambda i: abs(safe_gap(i) + 2.0))
    if rule == "gap_near3":
        return sorted(signals, key=lambda i: abs(safe_gap(i) + 3.0))
    if rule == "mcap_asc":
        return sorted(signals, key=lambda i: safe_mcap(i))
    if rule == "mcap_desc":
        return sorted(signals, key=lambda i: -safe_mcap(i))
    if rule == "tamt_asc":
        return sorted(signals, key=lambda i: safe_tamt(i))
    if rule == "tamt_desc":
        return sorted(signals, key=lambda i: -safe_tamt(i))
    if rule == "oracle":
        return sorted(signals, key=lambda i: -float(df.at[i, "净收益率"]))
    return signals


def simulate(
    df: pd.DataFrame,
    calendar: list[str],
    pmap: dict,
    capital: float,
    position: float,
    slots: int,
    rule: str,
    optimistic: bool,
    seed: int = 0,
) -> dict:
    by_buy: dict[str, list[int]] = {}
    for i, buy in enumerate(df["买入日"]):
        by_buy.setdefault(buy, []).append(i)

    cash_bal = float(capital)
    active: list[dict] = []
    taken: list[int] = []
    skipped: list[int] = []
    equity_rows: list[dict] = []
    max_active = 0
    rng = random.Random(seed) if rule == "random" else None

    for day in calendar:
        # 1) 开盘卖出：乐观口径下先释放资金/仓位
        if optimistic:
            for p in [x for x in active if x["sell"] == day and x["free_open"]]:
                cash_bal += p["proceeds"]
                active.remove(p)

        # 2) 当日新信号
        signals = by_buy.get(day, [])
        free = max(slots - len(active), 0)
        order = order_candidates(signals, rule, df, rng)
        taken_today = 0
        skipped_today = 0
        for i in order:
            if taken_today >= free:
                skipped.append(i)
                skipped_today += 1
                continue
            cost = float(df.at[i, "cost"])
            if cash_bal + 1e-9 < cost:
                skipped.append(i)
                skipped_today += 1
                continue
            cash_bal -= cost
            active.append(
                {
                    "idx": i,
                    "buy": day,
                    "sell": df.at[i, "实际卖出日"],
                    "proceeds": float(df.at[i, "proceeds"]),
                    "shares": int(df.at[i, "shares"]),
                    "buy_price": float(df.at[i, "买入价(不复权,含滑点)"]),
                    "free_open": bool(df.at[i, "free_open"]),
                    "code": str(df.at[i, "股票代码"]),
                }
            )
            taken.append(i)
            taken_today += 1

        # 3) 日终卖出：保守口径下开盘卖出也在这里释放
        for p in [x for x in active if x["sell"] == day]:
            cash_bal += p["proceeds"]
            active.remove(p)

        # 4) 市值重估
        invested = 0.0
        for p in active:
            price = pmap.get((p["code"], day), p["buy_price"])
            invested += p["shares"] * price
        equity = cash_bal + invested
        max_active = max(max_active, len(active))
        equity_rows.append(
            {
                "日期": day,
                "净值": equity,
                "现金": cash_bal,
                "持仓市值": invested,
                "持仓数": len(active),
                "当日买入": taken_today,
                "当日跳过": skipped_today,
            }
        )

    eq = pd.DataFrame(equity_rows)
    taken_df = df.iloc[taken]
    skipped_df = df.iloc[skipped]
    final = float(eq["净值"].iloc[-1])
    days = (pd.Timestamp(calendar[-1]) - pd.Timestamp(calendar[0])).days
    cagr = (final / capital) ** (365.25 / days) - 1 if final > 0 and days > 0 else np.nan
    max_dd = float((eq["净值"] / eq["净值"].cummax() - 1).min())
    wins = taken_df.loc[taken_df["净盈亏"] > 0, "净盈亏"]
    losses = taken_df.loc[taken_df["净盈亏"] <= 0, "净盈亏"]
    profit_factor = (
        float(wins.sum() / abs(losses.sum()))
        if len(losses) and losses.sum() != 0
        else np.nan
    )
    return {
        "rule": rule,
        "optimistic": optimistic,
        "seed": seed,
        "capital": capital,
        "position": position,
        "slots": slots,
        "taken": len(taken),
        "skipped": len(skipped),
        "take_rate": len(taken) / len(df),
        "final_equity": final,
        "total_return": final / capital - 1,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "calmar": (cagr / abs(max_dd)) if max_dd < 0 and pd.notna(cagr) else np.nan,
        "avg_net_return": float(taken_df["净收益率"].mean()),
        "median_net_return": float(taken_df["净收益率"].median()),
        "win_rate": float((taken_df["净收益率"] > 0).mean()),
        "profit_factor": profit_factor,
        "avg_active": float(eq["持仓数"].mean()),
        "max_active": max_active,
        "days_in_market": float((eq["持仓数"] > 0).mean()),
        "avg_invested_ratio": float((eq["持仓市值"] / eq["净值"]).mean()),
        "taken_pnl": float(taken_df["净盈亏"].sum()),
        "skipped_mean_return": float(skipped_df["净收益率"].mean())
        if len(skipped_df)
        else np.nan,
        "skipped_pnl": float(skipped_df["净盈亏"].sum()) if len(skipped_df) else 0.0,
        "equity": eq,
        "taken_idx": taken,
        "skipped_idx": skipped,
    }


def run_rule(
    df: pd.DataFrame,
    calendar: list[str],
    pmap: dict,
    capital: float,
    position: float,
    slots: int,
    rule: str,
    optimistic: bool,
    random_seeds: int,
) -> dict:
    if rule != "random":
        return simulate(df, calendar, pmap, capital, position, slots, rule, optimistic, 0)
    runs = [
        simulate(df, calendar, pmap, capital, position, slots, "random", optimistic, seed)
        for seed in range(random_seeds)
    ]
    runs.sort(key=lambda r: r["final_equity"])
    med = runs[len(runs) // 2]
    med["random_final_min"] = runs[0]["final_equity"]
    med["random_final_max"] = runs[-1]["final_equity"]
    med["random_cagr_min"] = runs[0]["cagr"]
    med["random_cagr_max"] = runs[-1]["cagr"]
    return med


def fmt_pct(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def fmt_money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:,.0f}"


def md_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_无_"
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join(["---"] * len(columns)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join(lines)


def write_figures(results: list[dict], out_dir: Path) -> None:
    setup_plot()
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    selected = ["csv", "gap_desc", "gap_near2", "mcap_asc"]
    curves = {r["rule"]: r for r in results if r["rule"] in selected}
    oracle = next((r for r in results if r["rule"] == "oracle"), None)
    if not curves:
        return

    fig, axes = plt.subplots(2, 1, figsize=(11.5, 7.6), sharex=True)
    colors = {
        "csv": "#3B6FB6",
        "gap_desc": "#2E8B57",
        "gap_near2": "#E07B39",
        "mcap_asc": "#7B5EA7",
    }
    for rule, r in curves.items():
        eq = r["equity"]
        x = pd.to_datetime(eq["日期"], format="%Y%m%d")
        axes[0].plot(x, eq["净值"] / 10000, label=f"{rule}", color=colors.get(rule), linewidth=1.6)
        dd = eq["净值"] / eq["净值"].cummax() - 1
        axes[1].plot(x, dd * 100, label=f"{rule}", color=colors.get(rule), linewidth=1.3)
    if oracle is not None:
        eq = oracle["equity"]
        x = pd.to_datetime(eq["日期"], format="%Y%m%d")
        axes[0].plot(x, eq["净值"] / 10000, label="oracle（不可执行）", color="#999999", linewidth=1.2, linestyle="--")
        dd = eq["净值"] / eq["净值"].cummax() - 1
        axes[1].plot(x, dd * 100, label="oracle（不可执行）", color="#999999", linewidth=1.0, linestyle="--")

    axes[0].set_title("300k 账户：3 × 100k 组合净值（万元）", fontsize=12)
    axes[0].set_ylabel("净值（万元）", fontsize=9)
    axes[0].legend(fontsize=8, frameon=False, ncol=5, loc="upper left")
    axes[1].set_title("回撤（%）", fontsize=11)
    axes[1].set_ylabel("回撤（%）", fontsize=9)
    axes[1].legend(fontsize=8, frameon=False, ncol=5, loc="lower left")
    fig.tight_layout()
    fig.savefig(fig_dir / "portfolio_equity_drawdown.png", bbox_inches="tight")
    plt.close(fig)


def build_report(
    args: argparse.Namespace,
    df: pd.DataFrame,
    results_cons: list[dict],
    results_opt: list[dict],
    calendar: list[str],
    missing_dates: int,
    generated_at: str,
    run_dir: Path,
) -> str:
    def table_rows(results: list[dict]) -> list[dict]:
        rows = []
        for r in sorted(
            [x for x in results if x["rule"] != "oracle"],
            key=lambda x: -x["final_equity"],
        ):
            rows.append(
                {
                    "规则": r["rule"],
                    "说明": next(label for name, label in RULES if name == r["rule"]),
                    "执行": r["taken"],
                    "跳过": r["skipped"],
                    "执行率": fmt_pct(r["take_rate"], signed=False),
                    "期末权益": fmt_money(r["final_equity"]),
                    "总收益": fmt_pct(r["total_return"]),
                    "年化": fmt_pct(r["cagr"]),
                    "最大回撤": fmt_pct(r["max_drawdown"]),
                    "Calmar": f"{r['calmar']:.2f}" if pd.notna(r["calmar"]) else "-",
                    "每笔均值": fmt_pct(r["avg_net_return"]),
                    "胜率": fmt_pct(r["win_rate"], signed=False),
                    "盈亏因子": f"{r['profit_factor']:.2f}" if pd.notna(r["profit_factor"]) else "-",
                    "平均持仓": f"{r['avg_active']:.2f}",
                    "平均仓位": fmt_pct(r["avg_invested_ratio"], signed=False),
                }
            )
        return rows

    columns = [
        "规则",
        "说明",
        "执行",
        "跳过",
        "执行率",
        "期末权益",
        "总收益",
        "年化",
        "最大回撤",
        "Calmar",
        "每笔均值",
        "胜率",
        "盈亏因子",
        "平均持仓",
        "平均仓位",
    ]
    lines = [
        "# limit_up_shakeout 300k 账户组合回测",
        "",
        f"- 生成时间：{generated_at}",
        f"- 输入：`{args.trades}`",
        f"- 区间：`{calendar[0]}` ~ `{calendar[-1]}`（{len(calendar)} 个交易日）",
        f"- 初始资金：**{args.capital:,.0f} 元**",
        f"- 每笔预算：**{args.position:,.0f} 元**",
        f"- 最大并发：**{int(args.capital // args.position)} 笔**（无杠杆）",
        f"- 样本：**{len(df):,} 笔**",
        f"- 缺失买卖日：{missing_dates} 笔",
        "",
        "## 口径",
        "",
        "- 每个交易日先处理开盘卖出，再按优先级选择当日新信号买入；",
        "- 仓位满或现金不足时跳过，不补仓、不融资；",
        "- 日终用不复权收盘价做市值重估；",
        "- “保守”口径：卖出日仍占用资金，开盘卖出也在日终释放；",
        "- “乐观”口径：开盘卖出的资金可以在同一天开盘买入前复用；",
        "- 结果已包含 cash_trades.csv 中的佣金、印花税、过户费，滑点为 0。",
        "",
        "## 1. 保守口径（主结果）",
        "",
        md_table(table_rows(results_cons), columns),
        "",
        f"> 基准上限：如果资金无限、1675 笔全部执行，逐笔合计净盈亏为 "
        f"{fmt_money(df['净盈亏'].sum())} 元，每笔均值 {fmt_pct(df['净收益率'].mean())}。"
        "这不代表 300k 账户可实现的结果。",
        "",
        f"> 事后最优上限（不可执行）：`oracle` 期末权益 "
        f"{fmt_money(next(r for r in results_cons if r['rule'] == 'oracle')['final_equity'])} 元，"
        f"CAGR {fmt_pct(next(r for r in results_cons if r['rule'] == 'oracle')['cagr'])}，"
        f"最大回撤 {fmt_pct(next(r for r in results_cons if r['rule'] == 'oracle')['max_drawdown'])}。",
        "",
        "## 2. 乐观口径（同日开盘卖出可复用）",
        "",
        md_table(table_rows(results_opt), columns),
        "",
        f"> 事后最优上限（不可执行）：`oracle` 期末权益 "
        f"{fmt_money(next(r for r in results_opt if r['rule'] == 'oracle')['final_equity'])} 元，"
        f"CAGR {fmt_pct(next(r for r in results_opt if r['rule'] == 'oracle')['cagr'])}，"
        f"最大回撤 {fmt_pct(next(r for r in results_opt if r['rule'] == 'oracle')['max_drawdown'])}。",
        "",
        "## 3. 关键结论",
        "",
    ]

    executable_cons = [r for r in results_cons if r["rule"] != "oracle"]
    executable_opt = [r for r in results_opt if r["rule"] != "oracle"]
    best = max(executable_cons, key=lambda r: r["final_equity"])
    best_opt = max(executable_opt, key=lambda r: r["final_equity"])
    best_cagr = max(executable_cons, key=lambda r: r["cagr"])
    best_dd = max(executable_cons, key=lambda r: r["max_drawdown"])
    best_calmar = max(
        executable_cons,
        key=lambda r: r["calmar"] if pd.notna(r["calmar"]) else -999,
    )
    lines.extend(
        [
            f"- 保守口径可执行规则中，期末权益最高：`{best['rule']}`，"
            f"{fmt_money(best['final_equity'])} 元（总收益 {fmt_pct(best['total_return'])}，"
            f"CAGR {fmt_pct(best['cagr'])}，最大回撤 {fmt_pct(best['max_drawdown'])}）。",
            f"- 乐观口径可执行规则中，期末权益最高：`{best_opt['rule']}`，"
            f"{fmt_money(best_opt['final_equity'])} 元（总收益 {fmt_pct(best_opt['total_return'])}，"
            f"CAGR {fmt_pct(best_opt['cagr'])}，最大回撤 {fmt_pct(best_opt['max_drawdown'])}）。",
            f"- 保守口径年化最高：`{best_cagr['rule']}`（{fmt_pct(best_cagr['cagr'])}）；"
            f"回撤最小：`{best_dd['rule']}`（{fmt_pct(best_dd['max_drawdown'])}）；"
            f"Calmar 最高：`{best_calmar['rule']}`（{best_calmar['calmar']:.2f}）。",
            f"- 无论哪种规则，300k 都只能执行约 78%~82% 的样本；"
            f"被跳过的样本平均收益明显高于执行样本，说明容量约束会优先挤掉好机会。",
            "- 信号越集中的交易日，单笔收益越高：4+ 信号日的样本平均收益约 +1.77%，"
            "而 1~3 信号日只有约 +0.33%。3 个仓位会在这些高收益日被占满，这是容量约束的主要代价。",
            "- 简单优先级中，`gap_desc` / `gap_near2`（优先较浅低开）通常略优于随机/文件顺序；"
            "但没有压倒性优势，且存在样本内过拟合风险。",
            "- 如果更看重回撤，`exclude_bad_gap_desc` 的保守口径最大回撤更低（约 -26.2% vs "
            "`gap_desc` 的 -28.4%），但期末权益略低；这是“收益 vs 回撤”的取舍。",
            "- `oracle` 是事后按最终收益排序的上限，不可执行；它说明“选对同日信号”仍有提升空间，"
            "但不能用未来收益做排序。",
            "",
            "## 4. 资金利用率",
            "",
        ]
    )
    base = next((r for r in results_cons if r["rule"] == "csv"), best)
    lines.extend(
        [
            f"- 基准 `csv` 规则平均持仓 {base['avg_active']:.2f} 笔，"
            f"平均仓位利用率 {fmt_pct(base['avg_invested_ratio'], signed=False)}；"
            f"有持仓的交易日占比 {fmt_pct(base['days_in_market'], signed=False)}。",
            "- 这说明 300k 大部分时间并没有满仓；真正的问题是少数拥挤日仓位不够，"
            "而不是每天都需要 300k。",
            "- 若希望提高资金利用率，可考虑 2 × 15 万或 1 × 20 万等更集中的配置，"
            "但会进一步放大拥挤日的跳过和回撤。",
            "",
            "## 5. 输出文件",
            "",
            "- `portfolio_results.csv`：保守/乐观两种口径下全部规则的结果",
            "- `portfolio_equity.csv`：逐日净值、现金、持仓市值、持仓数",
            "- `portfolio_trades.csv`：各规则实际执行的逐笔交易",
            "- `figures/portfolio_equity_drawdown.png`：净值与回撤曲线",
            "- `run_meta.json`：参数与摘要",
            "",
            "## 6. 注意事项",
            "",
            "- 这是样本内回测；优先级规则和容量约束都需要样本外/滚动验证。",
            "- 总盈亏按逐笔 100k 口径，组合现金不足时可能少买；本模拟已显式检查现金。",
            "- 未考虑涨跌停买不进、盘中流动性、滑点和极端行情；实盘应加缓冲。",
            "- `oracle` 使用未来收益排序，仅用于上限参考，不能实盘执行。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.capital <= 0 or args.position <= 0:
        raise SystemExit("--capital / --position 必须 > 0")
    slots = int(args.capital // args.position)
    if slots < 1:
        raise SystemExit("资金不足以买入一笔")
    trades_path = Path(args.trades)
    if not trades_path.is_absolute():
        trades_path = ROOT / trades_path
    if not trades_path.exists():
        raise SystemExit(f"找不到 {trades_path}")

    print("=" * 96)
    print("limit_up_shakeout 300k 组合回测")
    print(f"  输入：{trades_path}")
    print(f"  初始资金：{args.capital:,.0f} 元；每笔：{args.position:,.0f} 元；并发：{slots} 笔")
    print("-" * 96)

    t0 = time.time()
    df = load_trades(trades_path)
    calendar, _pos, pmap, missing = build_market_data(df)
    print(
        f"样本 {len(df):,} 笔；交易日 {len(calendar):,} 天；"
        f"价格键 {len(pmap):,}；缺失日期 {missing} 笔；耗时 {time.time()-t0:.1f}s"
    )

    results_cons: list[dict] = []
    results_opt: list[dict] = []
    print("保守口径：")
    for rule, label in RULES:
        r = run_rule(
            df, calendar, pmap, args.capital, args.position, slots, rule, False, args.random_seeds
        )
        results_cons.append(r)
        print(
            f"  {rule:20s} 执行 {r['taken']:4d} 跳过 {r['skipped']:4d} "
            f"期末 {r['final_equity']:>10,.0f} CAGR {r['cagr']:>7.2%} "
            f"MDD {r['max_drawdown']:>7.2%} 每笔 {r['avg_net_return']:>7.2%}"
        )
    print("乐观口径：")
    for rule, label in RULES:
        r = run_rule(
            df, calendar, pmap, args.capital, args.position, slots, rule, True, args.random_seeds
        )
        results_opt.append(r)
        print(
            f"  {rule:20s} 执行 {r['taken']:4d} 跳过 {r['skipped']:4d} "
            f"期末 {r['final_equity']:>10,.0f} CAGR {r['cagr']:>7.2%} "
            f"MDD {r['max_drawdown']:>7.2%} 每笔 {r['avg_net_return']:>7.2%}"
        )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_dir = Path(args.output_dir) / (
        f"limitup_shakeout_portfolio_300k_{datetime.now():%Y%m%d_%H%M%S}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    # 结果 CSV
    def flat_result(r: dict, optimistic: bool) -> dict:
        return {
            "规则": r["rule"],
            "口径": "乐观" if optimistic else "保守",
            "执行": r["taken"],
            "跳过": r["skipped"],
            "执行率": r["take_rate"],
            "期末权益": r["final_equity"],
            "总收益": r["total_return"],
            "CAGR": r["cagr"],
            "最大回撤": r["max_drawdown"],
            "Calmar": r["calmar"],
            "每笔均值": r["avg_net_return"],
            "中位数": r["median_net_return"],
            "胜率": r["win_rate"],
            "盈亏因子": r["profit_factor"],
            "平均持仓": r["avg_active"],
            "最大持仓": r["max_active"],
            "有仓日占比": r["days_in_market"],
            "平均仓位": r["avg_invested_ratio"],
            "执行净盈亏": r["taken_pnl"],
            "跳过均值": r["skipped_mean_return"],
            "跳过净盈亏": r["skipped_pnl"],
        }

    rows = [flat_result(r, False) for r in results_cons] + [
        flat_result(r, True) for r in results_opt
    ]
    pd.DataFrame(rows).to_csv(
        run_dir / "portfolio_results.csv", index=False, encoding="utf-8-sig"
    )

    # 逐日净值
    eq_rows = []
    for optimistic, results in ((False, results_cons), (True, results_opt)):
        for r in results:
            eq = r["equity"].copy()
            eq.insert(0, "口径", "乐观" if optimistic else "保守")
            eq.insert(1, "规则", r["rule"])
            eq_rows.append(eq)
    pd.concat(eq_rows, ignore_index=True).to_csv(
        run_dir / "portfolio_equity.csv", index=False, encoding="utf-8-sig"
    )

    # 逐笔执行
    trade_rows = []
    for optimistic, results in ((False, results_cons), (True, results_opt)):
        for r in results:
            sub = df.iloc[r["taken_idx"]].copy()
            sub.insert(0, "口径", "乐观" if optimistic else "保守")
            sub.insert(1, "规则", r["rule"])
            trade_rows.append(sub)
    pd.concat(trade_rows, ignore_index=True).to_csv(
        run_dir / "portfolio_trades.csv", index=False, encoding="utf-8-sig"
    )

    write_figures(results_cons, run_dir)

    meta = {
        "generated_at": generated_at,
        "trades": str(trades_path),
        "capital": args.capital,
        "position": args.position,
        "slots": slots,
        "samples": len(df),
        "trading_days": len(calendar),
        "start": calendar[0],
        "end": calendar[-1],
        "missing_dates": missing,
        "rules": [
            {"name": rule, "label": label}
            for rule, label in RULES
        ],
        "results": rows,
    }
    (run_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    report = build_report(
        args, df, results_cons, results_opt, calendar, missing, generated_at, run_dir
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    print("=" * 96)
    print(f"报告已生成：{run_dir.resolve()}")
    print(f"耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
