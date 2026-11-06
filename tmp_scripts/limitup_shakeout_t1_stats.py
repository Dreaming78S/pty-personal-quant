"""统计 limit_up_shakeout 命中样本（满足最低价支撑条件）的 T+1 开盘/最低价分布。

样本口径：全部命中（不做高开/收盘涨跌幅/近 N 日涨幅/T+1 低开等二次筛选），
且满足 DEFAULT_REQUIRE_LOW_SUPPORT（信号日最低价不破前一交易日收盘，后复权
`low(T) >= close(T-1)`）。

统计指标（单位：百分点）：
1. 高开比例 = [open(T+1) - close(T)] / close(T) × 100，为负即低开；
   同时在明细中保留对“除权后昨收”的低开口径供核对（除权日两者有差异）；
2. 最低价低于开盘比例 = [open(T+1) - low(T+1)] / close(T) × 100（分母为 T 日
   不复权收盘，值恒 >= 0，0 表示开盘即全天最低）；另附参考口径
   (low(T+1) - open(T+1)) / open(T+1) × 100（分母为开盘价，值恒 <= 0，
   即常见的“最低价回撤”说法）。

输出：分布汇总、分位数、区间分布、分年统计、逐笔明细 CSV，默认写到
outputs/limitup_shakeout_t1_stats_<时间戳>/。

用法：
    uv run python tmp_scripts/limitup_shakeout_t1_stats.py
    uv run python tmp_scripts/limitup_shakeout_t1_stats.py --start 2020-01-01
    uv run python tmp_scripts/limitup_shakeout_t1_stats.py --no-report
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
from quant.engine.exit_tiers import DEFAULT_EXIT_TIERS  # noqa: E402

import limitup_shakeout_filtered_returns as filtered  # noqa: E402

# ==============================================================================
# 统计区间：默认覆盖命中表全部历史
DEFAULT_START = "20180101"
DEFAULT_END = "latest"

# build_samples 需要的固定参数（本统计不做这些筛选，仅沿用默认值计算出基础列）
DEFAULT_RISE_DAYS = 5
FIXED_GAP = (0.00, 0.11)
FIXED_CLOSE_CHG = (0.0, 0.11)
FIXED_RISE = (-10, 10)
FIXED_ENTRY_GAP = (-0.11, 0.0)
# ==============================================================================

DEFAULT_GENERATE_REPORT = True

# 高开比例分桶（百分点，左开右闭，None 表示 ±∞）
GAP_BUCKETS = (
    ("(-∞, -11%]", None, -11.0),
    ("(-11%, -8%]", -11.0, -8.0),
    ("(-8%, -5%]", -8.0, -5.0),
    ("(-5%, -3%]", -5.0, -3.0),
    ("(-3%, -1%]", -3.0, -1.0),
    ("(-1%, 0%]", -1.0, 0.0),
    ("(0%, 1%]", 0.0, 1.0),
    ("(1%, 3%]", 1.0, 3.0),
    ("(3%, 5%]", 3.0, 5.0),
    ("(5%, 8%]", 5.0, 8.0),
    ("(8%, +∞)", 8.0, None),
)

# 最低价低于开盘比例分桶（对 T 收盘口径，值恒 >= 0；0 即开盘价 = 全天最低价）
LOW_BUCKETS = (
    ("0%（开盘即最低）", None, 0.0),
    ("(0%, 0.5%]", 0.0, 0.5),
    ("(0.5%, 1%]", 0.5, 1.0),
    ("(1%, 2%]", 1.0, 2.0),
    ("(2%, 3%]", 2.0, 3.0),
    ("(3%, 5%]", 3.0, 5.0),
    ("(5%, 8%]", 5.0, 8.0),
    ("(8%, +∞)", 8.0, None),
)

# 参考口径（最低较开盘回撤，分母为开盘价，值恒 <= 0）
LOW_REF_BUCKETS = (
    ("(-∞, -8%]", None, -8.0),
    ("(-8%, -5%]", -8.0, -5.0),
    ("(-5%, -3%]", -5.0, -3.0),
    ("(-3%, -2%]", -3.0, -2.0),
    ("(-2%, -1%]", -2.0, -1.0),
    ("(-1%, 0%]", -1.0, 0.0),
)

GAP_COL = "高开比例(%)"
LOW_COL = "最低价低于开盘(对T收盘,%)"
LOW_REF_COL = "最低价回撤(对开盘,%)"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="limit_up_shakeout 命中（含最低价支撑）样本的 T+1 开盘/最低价分布统计"
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
        help="生成 report.md / CSV（默认开启）",
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
    for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        out[f"P{int(q * 100):02d}"] = float(v.quantile(q))
    return out


def distribution_rows(
    values: pd.Series,
    buckets: tuple[tuple[str, float | None, float | None], ...],
) -> list[dict]:
    """按 (标签, 下界, 上界) 分桶（左开右闭，None 为 ±∞），输出数量/占比/累计占比。"""
    v = pd.to_numeric(values, errors="coerce").dropna()
    total = len(v)
    rows: list[dict] = []
    cumulative = 0
    for label, lo, hi in buckets:
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


def yearly_rows(sel: pd.DataFrame) -> list[dict]:
    rows = []
    for year, group in sel.groupby(sel["hit_date"].str[:4], sort=True):
        rows.append(
            {
                "年份": year,
                "样本数": len(group),
                "高开均值": group[GAP_COL].mean(),
                "高开中位": group[GAP_COL].median(),
                "低于开盘均值": group[LOW_COL].mean(),
                "低于开盘中位": group[LOW_COL].median(),
            }
        )
    return rows


def build_report(
    sel: pd.DataFrame,
    funnel_rows: list[dict],
    missing_rows: list[dict],
    gap_stats: dict,
    low_stats: dict,
    low_ref_stats: dict,
    gap_dist: list[dict],
    low_dist: list[dict],
    low_ref_dist: list[dict],
    yearly: list[dict],
    start: str,
    end: str,
    generated_at: str,
) -> str:
    def summary_table(stats: dict) -> str:
        rows = [
            {"指标": k, "数值": fmt_pts(v) if k != "样本数" else str(int(v) if v else 0)}
            for k, v in stats.items()
            if k not in {f"P{int(q * 100):02d}" for q in
                         (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)}
        ]
        return md_table(rows, ["指标", "数值"])

    def quantile_table(stats: dict) -> str:
        rows = [
            {"分位": f"P{int(q * 100):02d}", "数值": fmt_pts(stats.get(f"P{int(q * 100):02d}"))}
            for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)
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

    yearly_table = md_table(
        [
            {
                "年份": r["年份"],
                "样本数": r["样本数"],
                "高开均值": fmt_pts(r["高开均值"]),
                "高开中位": fmt_pts(r["高开中位"]),
                "低于开盘均值": fmt_pts(r["低于开盘均值"]),
                "低于开盘中位": fmt_pts(r["低于开盘中位"]),
            }
            for r in yearly
        ],
        ["年份", "样本数", "高开均值", "高开中位", "低于开盘均值", "低于开盘中位"],
    )

    return "\n".join(
        [
            "# limit_up_shakeout 命中样本 T+1 开盘/最低价分布",
            "",
            f"- 生成时间：{generated_at}",
            f"- 命中表：`hit_limit_up_shakeout`，区间 `{start}` ~ `{end}`",
            "- 样本口径：全部命中，且满足最低价支撑条件（后复权 `low(T) >= close(T-1)`）；"
            "不做高开/收盘涨跌幅/近 N 日涨幅/T+1 低开等二次筛选",
            "",
            "## 样本漏斗",
            "",
            md_table(funnel_rows, ["阶段", "样本数", "说明"]),
            "",
            "## 数据缺失（满足支撑条件的样本内）",
            "",
            md_table(missing_rows, ["项目", "样本数", "说明"]),
            "",
            "## 一、高开比例分布",
            "",
            f"公式：`[open(T+1) - close(T)] / close(T) × 100`（对 T 日不复权收盘，"
            "为负即低开）",
            "",
            summary_table(gap_stats),
            "",
            quantile_table(gap_stats),
            "",
            dist_table(gap_dist),
            "",
            "## 二、最低价低于开盘比例分布",
            "",
            f"公式：`[open(T+1) - low(T+1)] / close(T) × 100`（分母为 T 日不复权收盘，"
            "值恒 >= 0；0 表示开盘即全天最低）",
            "",
            summary_table(low_stats),
            "",
            quantile_table(low_stats),
            "",
            dist_table(low_dist),
            "",
            "## 三、参考口径：最低价较开盘的回撤",
            "",
            "公式：`[low(T+1) - open(T+1)] / open(T+1) × 100`（分母为开盘价，"
            "值恒 <= 0；与指标二同向单调，仅基准不同，供习惯口径对照）",
            "",
            summary_table(low_ref_stats),
            "",
            dist_table(low_ref_dist),
            "",
            "## 分年统计",
            "",
            yearly_table,
            "",
            "## 口径说明",
            "",
            "- 高开比例对 **T 日不复权收盘** 计算；除权日它与对“除权后昨收”的低开有差异，"
            "明细 CSV 同时保留 `低开幅度(对除权昨收,%)` 一列供核对（普通交易日两者相同）。",
            "- 最低价低于开盘比例以 **T 日不复权收盘为分母**（分子 open(T+1)-low(T+1) "
            "恒 >= 0）；参考口径以开盘价为分母，两者同向单调、数值不同。",
            "- T+1 缺行情（停牌/未产生）的样本不计入分布，数量见“数据缺失”。",
            "- 满足支撑但 T+1 为非交易日（命中日之后无交易日，如最新一天命中）同样不计入。",
            "",
            "## 输出文件",
            "",
            "- `report.md`：本报告",
            "- `samples.csv`：逐笔明细（含两个指标与参考口径、价格明细）",
            "- `summary.csv`：两口径汇总指标（长表）",
            "- `gap_bucket_stats.csv` / `low_bucket_stats.csv` / `low_ref_bucket_stats.csv`：区间分布",
            "- `yearly_stats.csv`：分年统计",
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
    print("limit_up_shakeout 命中样本 T+1 开盘/最低价分布统计")
    print(f"  统计区间：{start} ~ {end}")
    print("  样本口径：全部命中 + 最低价支撑（low(T) >= close(T-1)，后复权）")
    print("-" * 90)

    t0 = time.time()
    print(f"读取命中样本 {start} ~ {end} ...")
    hits = with_retry(lambda: filtered.load_hits(start, end), "读取命中样本")
    if hits.empty:
        raise SystemExit("指定区间没有 limit_up_shakeout 命中样本")
    print(f"命中样本：{len(hits):,} 条（{hits['hit_date'].min()} ~ {hits['hit_date'].max()}）")

    calendar = with_retry(filtered.load_calendar, "读取交易日历")
    hits = filtered.attach_dates(hits, calendar, DEFAULT_RISE_DAYS)
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
    min_date, max_date = str(needed_dates.min()), str(needed_dates.max())

    print(f"读取行情 {min_date} ~ {max_date} ...")
    prices = with_retry(
        lambda: filtered.load_prices(
            sorted(hits["ts_code"].unique()), min_date, max_date
        ),
        "读取行情",
    )
    if prices.empty:
        raise SystemExit("没有读取到行情数据")
    print(f"行情行数：{len(prices):,}（耗时 {time.time() - t0:.1f}s）")

    # 基础列复用 filtered.build_samples（含 entry_gap_close_raw / passed_low_support）
    samples = filtered.build_samples(
        hits,
        prices,
        FIXED_GAP,
        FIXED_CLOSE_CHG,
        FIXED_RISE,
        DEFAULT_RISE_DAYS,
        DEFAULT_EXIT_TIERS,
        FIXED_ENTRY_GAP,
        require_low_support=True,
    )
    sel = samples[samples["passed_low_support"]].copy()
    print(f"满足最低价支撑：{len(sel):,} / {len(samples):,} 条")

    # T+1 最低价已由 filtered.build_samples 提供（t1_low_raw 列），无需本地合并

    # 行业快照（命中表自身的 industry）
    industry_df = with_retry(
        lambda: db.read_df(
            "SELECT trade_date, ts_code, industry FROM hit_limit_up_shakeout "
            "WHERE trade_date BETWEEN %s AND %s",
            (start, end),
        ),
        "读取行业快照",
    ).rename(columns={"trade_date": "hit_date"})
    industry_df["hit_date"] = industry_df["hit_date"].astype(str)
    industry_df["ts_code"] = industry_df["ts_code"].astype(str)

    # 三个指标（百分点）
    sel[GAP_COL] = (
        pd.to_numeric(sel["entry_gap_close_raw"], errors="coerce") * 100.0
    )
    sel[LOW_COL] = (
        (pd.to_numeric(sel["t1_open_raw"], errors="coerce")
         - pd.to_numeric(sel["t1_low_raw"], errors="coerce"))
        / pd.to_numeric(sel["t_close_raw"], errors="coerce") * 100.0
    )
    sel[LOW_REF_COL] = (
        (pd.to_numeric(sel["t1_low_raw"], errors="coerce")
         - pd.to_numeric(sel["t1_open_raw"], errors="coerce"))
        / pd.to_numeric(sel["t1_open_raw"], errors="coerce") * 100.0
    )
    for col in (GAP_COL, LOW_COL, LOW_REF_COL):
        sel[col] = sel[col].replace([np.inf, -np.inf], np.nan)
    sel["低开幅度(对除权昨收,%)"] = (
        pd.to_numeric(sel["entry_gap"], errors="coerce") * 100.0
    )

    n_total = len(samples)
    n_support = len(sel)
    n_gap = int(sel[GAP_COL].notna().sum())
    n_low = int(sel[LOW_COL].notna().sum())
    missing_t1_date = int(sel["t1_date"].isna().sum())
    missing_t1_open = int(
        (sel["t1_date"].notna() & pd.to_numeric(sel["t1_open_raw"], errors="coerce")
         .isna()).sum()
    )
    missing_t1_low = int(
        (pd.to_numeric(sel["t1_open_raw"], errors="coerce").notna()
         & pd.to_numeric(sel["t1_low_raw"], errors="coerce").isna()).sum()
    )

    funnel_rows = [
        {"阶段": "命中样本总数", "样本数": n_total,
         "说明": f"hit_limit_up_shakeout {start} ~ {end}"},
        {"阶段": "满足最低价支撑", "样本数": n_support,
         "说明": "后复权 low(T) >= close(T-1)"},
        {"阶段": "高开比例可计算", "样本数": n_gap,
         "说明": "且 T+1 开盘价、T 日收盘价可用"},
        {"阶段": "最低价比例可计算", "样本数": n_low,
         "说明": "且 T+1 开盘价与最低价可用"},
    ]
    missing_rows = [
        {"项目": "缺 T+1 交易日", "样本数": missing_t1_date,
         "说明": "命中日之后无交易日（如最新一天命中）"},
        {"项目": "缺 T+1 开盘价", "样本数": missing_t1_open,
         "说明": "T+1 停牌或行情未产生"},
        {"项目": "缺 T+1 最低价", "样本数": missing_t1_low,
         "说明": "有开盘价但缺最低价（数据异常）"},
    ]

    gap_stats = stats_of(sel[GAP_COL])
    low_stats = stats_of(sel[LOW_COL])
    low_ref_stats = stats_of(sel[LOW_REF_COL])
    gap_dist = distribution_rows(sel[GAP_COL], GAP_BUCKETS)
    low_dist = distribution_rows(sel[LOW_COL], LOW_BUCKETS)
    low_ref_dist = distribution_rows(sel[LOW_REF_COL], LOW_REF_BUCKETS)
    yearly = yearly_rows(sel)

    print("-" * 90)
    print(f"高开比例：N={gap_stats.get('样本数', 0):,}，"
          f"均值 {fmt_pts(gap_stats.get('均值'))}，中位 {fmt_pts(gap_stats.get('中位数'))}，"
          f"P05 {fmt_pts(gap_stats.get('P05'))}，P95 {fmt_pts(gap_stats.get('P95'))}")
    print(f"最低价低于开盘（对T收盘）：N={low_stats.get('样本数', 0):,}，"
          f"均值 {fmt_pts(low_stats.get('均值'))}，中位 {fmt_pts(low_stats.get('中位数'))}，"
          f"P95 {fmt_pts(low_stats.get('P95'))}")
    low_zero = next((r for r in low_dist if r["区间"].startswith("0%")), None)
    if low_zero:
        print(f"开盘即全天最低：{low_zero['样本数']:,} 条"
              f"（{low_zero['占比'] * 100:.2f}%）")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.generate_report:
        run_dir = Path(args.output_dir) / (
            f"limitup_shakeout_t1_stats_{datetime.now():%Y%m%d_%H%M%S}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)

        detail = sel.merge(
            industry_df[["hit_date", "ts_code", "industry"]],
            on=["hit_date", "ts_code"], how="left",
        ).rename(columns={"industry": "行业"})
        detail_cols = [
            "hit_date", "ts_code", "name", "行业",
            "t_close_raw", "t1_open_raw", "t1_low_raw", "t1_close_raw",
            GAP_COL, "低开幅度(对除权昨收,%)", LOW_COL, LOW_REF_COL,
        ]
        detail_cn = {
            "hit_date": "命中日期", "ts_code": "股票代码", "name": "股票名称",
            "t_close_raw": "T日收盘价(不复权)", "t1_open_raw": "T+1开盘价(不复权)",
            "t1_low_raw": "T+1最低价(不复权)", "t1_close_raw": "T+1收盘价(不复权)",
        }
        out = detail[detail_cols].rename(columns=detail_cn)
        out.to_csv(run_dir / "samples.csv", index=False, encoding="utf-8-sig")

        summary_rows = [
            {"指标": name, **{k: v for k, v in stats.items()}}
            for name, stats in (
                ("高开比例", gap_stats), ("最低价低于开盘(对T收盘)", low_stats),
                ("最低价回撤(对开盘)", low_ref_stats),
            )
        ]
        pd.DataFrame(summary_rows).to_csv(
            run_dir / "summary.csv", index=False, encoding="utf-8-sig"
        )
        for filename, rows in (
            ("gap_bucket_stats.csv", gap_dist),
            ("low_bucket_stats.csv", low_dist),
            ("low_ref_bucket_stats.csv", low_ref_dist),
        ):
            pd.DataFrame(rows).to_csv(
                run_dir / filename, index=False, encoding="utf-8-sig"
            )
        pd.DataFrame(yearly).to_csv(
            run_dir / "yearly_stats.csv", index=False, encoding="utf-8-sig"
        )
        meta = {
            "generated_at": generated_at,
            "start": start,
            "end": end,
            "scope": "全部命中 + 最低价支撑（low(T) >= close(T-1)，后复权），无其他筛选",
            "metrics": {
                GAP_COL: "[open(T+1) - close(T)] / close(T) * 100",
                LOW_COL: "[open(T+1) - low(T+1)] / close(T) * 100",
                LOW_REF_COL: "[low(T+1) - open(T+1)] / open(T+1) * 100",
            },
            "hit_samples": n_total,
            "support_samples": n_support,
            "gap_valid_samples": n_gap,
            "low_valid_samples": n_low,
            "missing": {r["项目"]: r["样本数"] for r in missing_rows},
        }
        (run_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report = build_report(
            sel, funnel_rows, missing_rows, gap_stats, low_stats, low_ref_stats,
            gap_dist, low_dist, low_ref_dist, yearly, start, end, generated_at,
        )
        (run_dir / "report.md").write_text(report, encoding="utf-8")
        print(f"报告目录：{run_dir.resolve()}")
    else:
        print("报告目录：未生成（--no-report）")
    print(f"总耗时：{time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
