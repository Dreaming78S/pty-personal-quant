"""逐行统计 limit_up_shakeout 命中样本的 T+1“开盘 - 最低价”幅度分布。

统计量（百分点）：
    stat(T) = [open(T+1) - low(T+1)] / close(T) × 100

- T 为命中日（信号日），T+1 为下一交易日；
- 分母 close(T) 为 T 日不复权收盘价，分子为 T+1 不复权开盘价与最低价；
- 值恒 >= 0（0 表示开盘即全天最低，> 0 表示盘中曾低于开盘）。

样本口径：hit_limit_up_shakeout 全部命中，逐行计算，不做任何二次筛选
（不要求最低价支撑、不要求高开/收盘涨跌幅/近 N 日涨幅等）。

参考口径：同一公式改用后复权价计算，剔除 T+1 除权造成的原始价跳空；
明细中标记 T+1 是否为除权日，便于核对两者差异。

输出：分布汇总、分位数、区间分布、阈值覆盖、分年统计、逐笔明细 CSV、
直方图/累计分布/分年箱线图，默认写到
outputs/limitup_shakeout_t1_open_low_<时间戳>/。

用法：
    uv run python tmp_scripts/limitup_shakeout_t1_open_low_dist.py
    uv run python tmp_scripts/limitup_shakeout_t1_open_low_dist.py --start 2020-01-01
    uv run python tmp_scripts/limitup_shakeout_t1_open_low_dist.py --no-report
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

import limitup_shakeout_returns as filtered  # noqa: E402

# ==============================================================================
# 统计区间：默认覆盖命中表全部历史
DEFAULT_START = "20180101"
DEFAULT_END = "latest"
# ==============================================================================

DEFAULT_GENERATE_REPORT = True

STAT_COL = "开盘最低幅度(对T收盘,%)"
STAT_ADJ_COL = "开盘最低幅度(复权参考,%)"
EX_DIV_COL = "T+1除权日"

# 区间分桶（百分点，左开右闭；None 表示 ±∞）
BUCKETS = (
    ("<= 0（开盘即最低/异常）", None, 0.0),
    ("(0%, 0.5%]", 0.0, 0.5),
    ("(0.5%, 1%]", 0.5, 1.0),
    ("(1%, 1.5%]", 1.0, 1.5),
    ("(1.5%, 2%]", 1.5, 2.0),
    ("(2%, 2.5%]", 2.0, 2.5),
    ("(2.5%, 3%]", 2.5, 3.0),
    ("(3%, 4%]", 3.0, 4.0),
    ("(4%, 5%]", 4.0, 5.0),
    ("(5%, 6%]", 5.0, 6.0),
    ("(6%, 8%]", 6.0, 8.0),
    ("(8%, +∞)", 8.0, None),
)

# 阈值覆盖：stat <= 阈值 的样本占比
THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0)

PERCENTILES = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="limit_up_shakeout 全部命中的 T+1 开盘-最低价幅度分布统计"
    )
    parser.add_argument(
        "--start", default=DEFAULT_START,
        help=f"命中日起始，默认 {DEFAULT_START}（即命中表全部历史）",
    )
    parser.add_argument(
        "--end", default=DEFAULT_END,
        help=f"命中日结束，默认 {DEFAULT_END}，即命中表最新日期",
    )
    parser.add_argument(
        "--report", dest="generate_report", action="store_true",
        help="生成 report.md / CSV / 图（默认开启）",
    )
    parser.add_argument(
        "--no-report", dest="generate_report", action="store_false",
        help="只打印控制台，不写报告文件",
    )
    parser.set_defaults(generate_report=DEFAULT_GENERATE_REPORT)
    parser.add_argument(
        "--output-dir", default="outputs", help="报告输出根目录，默认 outputs"
    )
    return parser.parse_args(argv)


def with_retry(fn, desc: str, attempts: int = 3, wait: float = 8.0):
    """带重试的调用包装：本地到 RDS 的链路偶发超时。"""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络/连接类错误统一重试
            if i == attempts:
                raise
            print(f"  {desc} 第 {i}/{attempts} 次尝试失败：{exc}；{wait:g}s 后重试 ...")
            time.sleep(wait)


def fmt_pts(value: float | None, signed: bool = True) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:+.2f}" if signed else f"{value:.2f}"


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


def stats_of(values: pd.Series) -> dict:
    v = pd.to_numeric(values, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if v.empty:
        return {"样本数": 0}
    out: dict = {
        "样本数": len(v),
        "均值": float(v.mean()),
        "中位数": float(v.median()),
        "标准差": float(v.std(ddof=1)) if len(v) > 1 else None,
        "最小": float(v.min()),
        "最大": float(v.max()),
    }
    for q in PERCENTILES:
        out[f"P{int(q * 100):02d}"] = float(v.quantile(q))
    return out


def distribution_rows(values: pd.Series) -> list[dict]:
    v = pd.to_numeric(values, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    total = len(v)
    rows: list[dict] = []
    cumulative = 0
    for label, lo, hi in BUCKETS:
        mask = pd.Series(True, index=v.index)
        if lo is not None:
            mask &= v > lo
        if hi is not None:
            mask &= v <= hi
        n = int(mask.sum())
        cumulative += n
        rows.append(
            {
                "区间": label,
                "样本数": n,
                "占比": (n / total) if total else None,
                "累计占比": (cumulative / total) if total else None,
            }
        )
    return rows


def threshold_rows(values: pd.Series) -> list[dict]:
    v = pd.to_numeric(values, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    total = len(v)
    rows = []
    for t in THRESHOLDS:
        n = int((v <= t).sum())
        rows.append(
            {
                "阈值": f"<= {t:.1f}%",
                "样本数": n,
                "占比": (n / total) if total else None,
            }
        )
    n = int((v > THRESHOLDS[-1]).sum())
    rows.append(
        {
            "阈值": f"> {THRESHOLDS[-1]:.1f}%",
            "样本数": n,
            "占比": (n / total) if total else None,
        }
    )
    return rows


def yearly_rows(sel: pd.DataFrame) -> list[dict]:
    rows = []
    for year, group in sel.groupby(sel["hit_date"].str[:4], sort=True):
        v = group[STAT_COL]
        rows.append(
            {
                "年份": year,
                "样本数": len(group),
                "均值": v.mean(),
                "中位数": v.median(),
                "P25": v.quantile(0.25),
                "P75": v.quantile(0.75),
                "开盘即最低占比": float((v <= 0).mean()),
            }
        )
    return rows


def write_plots(sel: pd.DataFrame, path: Path) -> bool:
    if sel.empty:
        return False

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    values = sel[STAT_COL].dropna()
    if values.empty:
        return False

    years = sorted(sel["hit_date"].str[:4].unique())
    data = [
        sel.loc[sel["hit_date"].str[:4] == y, STAT_COL].dropna() for y in years
    ]

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))

    axes[0].hist(values, bins=np.arange(0, 8.25, 0.25), color="#4C78A8",
                 edgecolor="white")
    axes[0].axvline(values.mean(), color="#E45756", linewidth=1.5,
                    label=f"Mean {values.mean():.2f}%")
    axes[0].axvline(values.median(), color="#F58518", linewidth=1.5,
                    linestyle="--", label=f"Median {values.median():.2f}%")
    axes[0].set_title("(open(T+1)-low(T+1))/close(T), 0-8%")
    axes[0].set_xlabel("Percentage points")
    axes[0].set_ylabel("Samples")
    axes[0].legend()

    ordered = np.sort(values.to_numpy())
    cdf = np.arange(1, len(ordered) + 1) / len(ordered)
    axes[1].plot(ordered, cdf * 100, color="#4C78A8")
    axes[1].set_xlim(0, 8)
    axes[1].set_ylim(0, 101)
    axes[1].set_title("Cumulative distribution")
    axes[1].set_xlabel("Percentage points")
    axes[1].set_ylabel("Cumulative share (%)")
    axes[1].grid(alpha=0.3)

    box = axes[2].boxplot(data, showmeans=True, patch_artist=True)
    for patch in box["boxes"]:
        patch.set_facecolor("#A5C8E1")
    axes[2].axhline(0, color="black", linewidth=1)
    axes[2].set_xticks(range(1, len(years) + 1))
    axes[2].set_xticklabels(years, rotation=45)
    axes[2].set_title("Distribution by hit year")
    axes[2].set_xlabel("Year")
    axes[2].set_ylabel("Percentage points")

    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def build_report(
    funnel_rows: list[dict],
    missing_rows: list[dict],
    stats: dict,
    stats_adj: dict,
    dist: list[dict],
    thresholds: list[dict],
    yearly: list[dict],
    ex_div_count: int,
    start: str,
    end: str,
    generated_at: str,
) -> str:
    def summary_table(s: dict) -> str:
        rows = [
            {"指标": k, "数值": str(int(v)) if k == "样本数" else fmt_pts(v)}
            for k, v in s.items()
            if k not in {f"P{int(q * 100):02d}" for q in PERCENTILES}
        ]
        return md_table(rows, ["指标", "数值"])

    def quantile_table(s: dict) -> str:
        rows = [
            {"分位": f"P{int(q * 100):02d}",
             "数值": fmt_pts(s.get(f"P{int(q * 100):02d}"))}
            for q in PERCENTILES
        ]
        return md_table(rows, ["分位", "数值"])

    def dist_table(rows: list[dict]) -> str:
        return md_table(
            [
                {
                    "区间": r["区间"],
                    "样本数": r["样本数"],
                    "占比": fmt_pts(r["占比"] * 100, signed=False) + "%"
                    if r["占比"] is not None else "-",
                    "累计占比": fmt_pts(r["累计占比"] * 100, signed=False) + "%"
                    if r["累计占比"] is not None else "-",
                }
                for r in rows
            ],
            ["区间", "样本数", "占比", "累计占比"],
        )

    threshold_table = md_table(
        [
            {
                "阈值": r["阈值"],
                "样本数": r["样本数"],
                "占比": fmt_pts(r["占比"] * 100, signed=False) + "%"
                if r["占比"] is not None else "-",
            }
            for r in thresholds
        ],
        ["阈值", "样本数", "占比"],
    )

    yearly_table = md_table(
        [
            {
                "年份": r["年份"],
                "样本数": r["样本数"],
                "均值": fmt_pts(r["均值"]),
                "中位数": fmt_pts(r["中位数"]),
                "P25": fmt_pts(r["P25"]),
                "P75": fmt_pts(r["P75"]),
                "开盘即最低占比": fmt_pts(r["开盘即最低占比"] * 100, signed=False)
                + "%",
            }
            for r in yearly
        ],
        ["年份", "样本数", "均值", "中位数", "P25", "P75", "开盘即最低占比"],
    )

    return "\n".join(
        [
            "# limit_up_shakeout 命中样本 T+1 开盘-最低价幅度分布",
            "",
            f"- 生成时间：{generated_at}",
            f"- 命中表：`hit_limit_up_shakeout`，区间 `{start}` ~ `{end}`",
            "- 样本口径：**全部命中逐行计算**，不做二次筛选",
            "",
            "## 统计量",
            "",
            "```",
            "stat(T) = [open(T+1) - low(T+1)] / close(T) × 100",
            "```",
            "",
            "- T：命中日/信号日；T+1：下一交易日；",
            "- 价格为**不复权**价，分母为 T 日收盘价；",
            "- 值恒 >= 0：0 表示开盘即全天最低，> 0 表示盘中最低价低于开盘。",
            "",
            "## 样本漏斗",
            "",
            md_table(funnel_rows, ["阶段", "样本数", "说明"]),
            "",
            "## 数据缺失（全部命中内）",
            "",
            md_table(missing_rows, ["项目", "样本数", "说明"]),
            "",
            "## 一、总体分布",
            "",
            summary_table(stats),
            "",
            quantile_table(stats),
            "",
            "## 二、区间分布",
            "",
            dist_table(dist),
            "",
            "## 三、阈值覆盖（stat <= 阈值）",
            "",
            threshold_table,
            "",
            "## 四、分年统计",
            "",
            yearly_table,
            "",
            "## 五、参考口径（后复权，剔除 T+1 除权影响）",
            "",
            "同一公式改用后复权价：`[adj_open(T+1) - adj_low(T+1)] / adj_close(T) × 100`。",
            "",
            summary_table(stats_adj),
            "",
            f"- T+1 为除权日的样本：{ex_div_count:,} 条"
            "（复权因子在 T 与 T+1 之间发生变化）。",
            "- 非除权日两者完全一致；除权日原始价口径会包含除权跳空，"
            "复权口径更接近“真实盘中回撤”。",
            "",
            "## 口径说明",
            "",
            "- 命中表最新一天若尚无 T+1 行情（如当日命中），该行无法计算，"
            "数量见“数据缺失”。",
            "- T+1 停牌/行情未产生同样不计入分布。",
            "- 全样本逐笔明细见 `samples.csv`，含原始价、统计量、复权参考值与除权标记。",
            "",
            "## 输出文件",
            "",
            "- `report.md`：本报告",
            "- `samples.csv`：逐笔明细（全部命中，含统计量与价格）",
            "- `summary.csv`：总体指标（原始口径 + 复权参考口径）",
            "- `bucket_stats.csv`：区间分布",
            "- `threshold_stats.csv`：阈值覆盖",
            "- `yearly_stats.csv`：分年统计",
            "- `distribution.png`：直方图 / 累计分布 / 分年箱线图",
            "- `run_meta.json`：运行参数",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = filtered.compact_date(args.start)
    if args.end is None or str(args.end).strip().lower() in ("", "latest", "none"):
        max_hit = with_retry(
            lambda: db.scalar("SELECT MAX(trade_date) FROM hit_limit_up_shakeout"),
            "读取最新命中日",
        )
        if not max_hit:
            raise SystemExit("hit_limit_up_shakeout 为空，请先执行 hits update")
        end = str(max_hit)
    else:
        end = filtered.compact_date(args.end)
    if start > end:
        raise SystemExit(f"起始日期 {start} 晚于结束日期 {end}")

    print("=" * 90)
    print("limit_up_shakeout 全部命中 T+1 开盘-最低价幅度分布")
    print(f"  统计区间：{start} ~ {end}")
    print("  统计量：[open(T+1) - low(T+1)] / close(T) * 100")
    print("-" * 90)

    t0 = time.time()
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = with_retry(lambda: filtered.load_hits(start, end), "读取命中样本")
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条"
          f"（{hits['hit_date'].min()} ~ {hits['hit_date'].max()}）")

    calendar = with_retry(filtered.load_calendar, "读取交易日历")
    hits = filtered.attach_dates(hits, calendar, "unlimited")

    print("读取行情（按样本角色日期精确取数）...")
    price_keys = list(zip(hits["ts_code"], hits["hit_date"])) + list(
        zip(hits["ts_code"], hits["t1_date"])
    )
    prices = with_retry(
        lambda: filtered.load_prices_by_keys(price_keys),
        "读取行情",
    )
    if prices.empty:
        raise SystemExit("没有读取到行情数据")
    print(f"行情行数：{len(prices):,}（耗时 {time.time() - t0:.1f}s）")

    px = prices[
        [
            "ts_code", "trade_date", "open", "low", "close",
            "adj_factor", "adj_open", "adj_low", "adj_close",
        ]
    ]
    t_px = px.rename(
        columns={
            "trade_date": "hit_date",
            "open": "t_open_raw",
            "low": "t_low_raw",
            "close": "t_close_raw",
            "adj_factor": "t_adj_factor",
            "adj_open": "t_adj_open",
            "adj_low": "t_adj_low",
            "adj_close": "t_adj_close",
        }
    )
    t1_px = px.rename(
        columns={
            "trade_date": "t1_date",
            "open": "t1_open_raw",
            "low": "t1_low_raw",
            "close": "t1_close_raw",
            "adj_factor": "t1_adj_factor",
            "adj_open": "t1_adj_open",
            "adj_low": "t1_adj_low",
            "adj_close": "t1_adj_close",
        }
    )
    sel = hits.merge(t_px, on=["ts_code", "hit_date"], how="left")
    sel = sel.merge(t1_px, on=["ts_code", "t1_date"], how="left")

    sel[STAT_COL] = (
        (pd.to_numeric(sel["t1_open_raw"], errors="coerce")
         - pd.to_numeric(sel["t1_low_raw"], errors="coerce"))
        / pd.to_numeric(sel["t_close_raw"], errors="coerce") * 100.0
    )
    sel[STAT_ADJ_COL] = (
        (pd.to_numeric(sel["t1_adj_open"], errors="coerce")
         - pd.to_numeric(sel["t1_adj_low"], errors="coerce"))
        / pd.to_numeric(sel["t_adj_close"], errors="coerce") * 100.0
    )
    for col in (STAT_COL, STAT_ADJ_COL):
        sel[col] = sel[col].replace([np.inf, -np.inf], np.nan)

    factor_gap = (
        pd.to_numeric(sel["t1_adj_factor"], errors="coerce")
        - pd.to_numeric(sel["t_adj_factor"], errors="coerce")
    ).abs()
    sel[EX_DIV_COL] = (factor_gap > 1e-9).fillna(False)

    n_total = len(sel)
    missing_t1_date = int(sel["t1_date"].isna().sum())
    missing_t_close = int(pd.to_numeric(sel["t_close_raw"], errors="coerce").isna().sum())
    missing_t1_open = int(
        (sel["t1_date"].notna()
         & pd.to_numeric(sel["t1_open_raw"], errors="coerce").isna()).sum()
    )
    missing_t1_low = int(
        (pd.to_numeric(sel["t1_open_raw"], errors="coerce").notna()
         & pd.to_numeric(sel["t1_low_raw"], errors="coerce").isna()).sum()
    )
    valid = sel[STAT_COL].notna()
    n_valid = int(valid.sum())
    n_negative = int((sel[STAT_COL] < -1e-9).sum())
    n_zero = int((sel[STAT_COL].abs() <= 1e-9).sum())

    funnel_rows = [
        {"阶段": "命中样本总数", "样本数": n_total,
         "说明": f"hit_limit_up_shakeout {start} ~ {end}"},
        {"阶段": "统计量可计算", "样本数": n_valid,
         "说明": "T、T+1 价格齐全"},
        {"阶段": "开盘即最低（stat = 0）", "样本数": n_zero,
         "说明": "open(T+1) == low(T+1)"},
        {"阶段": "负值（数据异常）", "样本数": n_negative,
         "说明": "low(T+1) > open(T+1)，正常行情不应出现"},
    ]
    missing_rows = [
        {"项目": "缺 T+1 交易日", "样本数": missing_t1_date,
         "说明": "命中日之后无交易日（如最新一天命中）"},
        {"项目": "缺 T 日收盘价", "样本数": missing_t_close,
         "说明": "T 日行情缺失"},
        {"项目": "缺 T+1 开盘价", "样本数": missing_t1_open,
         "说明": "T+1 停牌或行情未产生"},
        {"项目": "缺 T+1 最低价", "样本数": missing_t1_low,
         "说明": "有开盘价但缺最低价（数据异常）"},
    ]

    stats = stats_of(sel[STAT_COL])
    stats_adj = stats_of(sel[STAT_ADJ_COL])
    dist = distribution_rows(sel[STAT_COL])
    thresholds = threshold_rows(sel[STAT_COL])
    yearly = yearly_rows(sel[valid].copy())
    ex_div_count = int(sel.loc[valid, EX_DIV_COL].sum())

    print("-" * 90)
    print(f"样本：命中 {n_total:,}，可计算 {n_valid:,}，"
          f"开盘即最低 {n_zero:,}（{n_zero / n_valid * 100 if n_valid else 0:.2f}%），"
          f"负值 {n_negative}")
    if n_valid:
        print(f"均值 {fmt_pts(stats['均值'])}，中位 {fmt_pts(stats['中位数'])}，"
              f"P05 {fmt_pts(stats['P05'])}，P25 {fmt_pts(stats['P25'])}，"
              f"P75 {fmt_pts(stats['P75'])}，P95 {fmt_pts(stats['P95'])}，"
              f"最大 {fmt_pts(stats['最大'])}")
        print(f"复权参考口径：均值 {fmt_pts(stats_adj.get('均值'))}，"
              f"中位 {fmt_pts(stats_adj.get('中位数'))}，"
              f"除权样本 {ex_div_count:,}")
        print("区间分布：")
        for r in dist:
            share = f"{r['占比'] * 100:5.2f}%" if r["占比"] is not None else "  -  "
            print(f"  {r['区间']:<24} {r['样本数']:>5}  {share}")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_t1_open_low_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        detail_cols = [
            "hit_date", "ts_code", "name",
            "t_close_raw", "t1_open_raw", "t1_low_raw", "t1_close_raw",
            STAT_COL, STAT_ADJ_COL, EX_DIV_COL,
        ]
        detail_cn = {
            "hit_date": "命中日期",
            "ts_code": "股票代码",
            "name": "股票名称",
            "t_close_raw": "T日收盘价(不复权)",
            "t1_open_raw": "T+1开盘价(不复权)",
            "t1_low_raw": "T+1最低价(不复权)",
            "t1_close_raw": "T+1收盘价(不复权)",
        }
        sel[detail_cols].rename(columns=detail_cn).to_csv(
            run_dir / "samples.csv", index=False, encoding="utf-8-sig"
        )

        summary_rows = [
            {"口径": name, **s}
            for name, s in (("原始价(对T收盘)", stats), ("后复权(参考)", stats_adj))
        ]
        pd.DataFrame(summary_rows).to_csv(
            run_dir / "summary.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(dist).to_csv(
            run_dir / "bucket_stats.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(thresholds).to_csv(
            run_dir / "threshold_stats.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(yearly).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )
        plotted = write_plots(sel[valid].copy(), run_dir / "distribution.png")

        meta = {
            "generated_at": generated_at,
            "start": start,
            "end": end,
            "scope": "全部命中逐行计算，无二次筛选",
            "formula": "[open(T+1) - low(T+1)] / close(T) * 100（不复权）",
            "formula_adj": "[adj_open(T+1) - adj_low(T+1)] / adj_close(T) * 100",
            "hit_samples": n_total,
            "valid_samples": n_valid,
            "zero_samples": n_zero,
            "negative_samples": n_negative,
            "ex_div_samples": ex_div_count,
            "missing": {r["项目"]: r["样本数"] for r in missing_rows},
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report = build_report(
            funnel_rows, missing_rows, stats, stats_adj, dist, thresholds,
            yearly, ex_div_count, start, end, generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"报告目录：{run_dir.resolve()}")
        print(f"分布图：{'已生成' if plotted else '无有效样本，未生成'}")
    else:
        print("报告目录：未生成（--no-report）")
    print(f"总耗时：{time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
