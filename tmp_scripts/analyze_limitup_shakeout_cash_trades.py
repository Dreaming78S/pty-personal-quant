#!/usr/bin/env python
"""分析 limit_up_shakeout 的 cash_trades.csv，重点解释净收益率。

用法：
    uv run python tmp_scripts/analyze_limitup_shakeout_cash_trades.py \
        outputs/limitup_shakeout_returns_20261107_000812/cash_trades.csv

默认在 CSV 同级目录生成：
    analysis_report.md
    analysis_tables.xlsx
    cash_trades_enriched.csv
    figures/fig1_*.png ... fig9_*.png
"""

from __future__ import annotations

import argparse
import math
from math import erf, exp, lgamma, log, sqrt
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import TwoSlopeNorm

# ---------------------------------------------------------------------------
# 列名与分档
# ---------------------------------------------------------------------------

PCT_COLS = [
    "T日开盘相对T-1收盘(后复权)",
    "T日收盘相对T-1收盘(后复权)",
    "T日最低相对T-1收盘(后复权)",
    "买入日毛收益率",
    "T+1日内涨幅(后复权)",
    "T+1开盘至最低下探比例(后复权)",
    "T+1开盘至最高上行比例(后复权)",
    "T+1开盘至收盘变化比例(后复权)",
    "T+2收盘对T+1开盘涨幅(后复权)",
    "T+2最高对T+1开盘涨幅(后复权)",
    "毛收益率",
    "净收益率",
]

TIER_ORDER = ["(-∞, -5%]", "(-5%, 0%]", "(0%, 5%]", "(5%, +∞)"]

BIN_SPECS = {
    "T日收盘后流通市值(亿元)": (
        [0, 20, 40, 80, 150, 300, np.inf],
        ["<20", "20-40", "40-80", "80-150", "150-300", "300+"],
        "流通市值(亿元)",
    ),
    "T成交额/T-1成交额": (
        [0, 2.5, 3, 4, 5, 7, 10, np.inf],
        ["<2.5", "2.5-3", "3-4", "4-5", "5-7", "7-10", "10+"],
        "T成交额/T-1成交额",
    ),
    "T日开盘相对T-1收盘(后复权)": (
        [0, 2, 4, 6, 8, 10, 11.1],
        ["0-2", "2-4", "4-6", "6-8", "8-10", "10-11"],
        "T日高开(%)",
    ),
    "T日最低相对T-1收盘(后复权)": (
        [0, 0.5, 1, 2, 3, 5, np.inf],
        ["0-0.5", "0.5-1", "1-2", "2-3", "3-5", "5+"],
        "T日最低相对前收(%)",
    ),
    "T+1成交额/T成交额": (
        [0, 0.4, 0.55, 0.65, 0.8, 1.0, np.inf],
        ["<0.4", "0.4-0.55", "0.55-0.65", "0.65-0.8", "0.8-1.0", "1.0+"],
        "T+1成交额/T成交额",
    ),
    "T+1日内涨幅(后复权)": (
        [-np.inf, -5, 0, 5, np.inf],
        ["<=-5", "(-5,0]", "(0,5]", "(5,+]"],
        "T+1日内涨幅(%)",
    ),
    "T+1开盘至最低下探比例(后复权)": (
        [0, 0.5, 1, 2, 3, 5, np.inf],
        ["0-0.5", "0.5-1", "1-2", "2-3", "3-5", "5+"],
        "T+1下探比例(%)",
    ),
    "T+1开盘至最高上行比例(后复权)": (
        [0, 2, 4, 6, 9, 12, np.inf],
        ["0-2", "2-4", "4-6", "6-9", "9-12", "12+"],
        "T+1上冲比例(%)",
    ),
    "T+1开盘至收盘变化比例(后复权)": (
        [-np.inf, -5, 0, 5, np.inf],
        ["<=-5", "(-5,0]", "(0,5]", "(5,+]"],
        "T+1开盘至收盘变化(%)",
    ),
    "t1_gap": (
        [-np.inf, -5, -2, 0, 2, 5, np.inf],
        ["<-5", "-5~-2", "-2~0", "0~2", "2~5", ">5"],
        "T+1隐含跳空(%)",
    ),
    "upper_shadow": (
        [0, 1, 2, 3, 5, np.inf],
        ["0-1", "1-2", "2-3", "3-5", "5+"],
        "上影线(%)",
    ),
    "lower_shadow": (
        [0, 0.5, 1, 2, 3, np.inf],
        ["0-0.5", "0.5-1", "1-2", "2-3", "3+"],
        "下影线(%)",
    ),
    "body": (
        [0, 0.5, 1, 2, 3, 5, np.inf],
        ["0-0.5", "0.5-1", "1-2", "2-3", "3-5", "5+"],
        "实体(%)",
    ),
}

SECTOR_MAP: dict[str, str] = {}


def _add_sector(sector: str, names: list[str]) -> None:
    for name in names:
        SECTOR_MAP[name] = sector


_add_sector("科技硬件", ["元器件", "半导体", "IT设备", "通信设备"])
_add_sector("软件传媒", ["软件服务", "互联网", "影视音像", "出版业", "广告包装"])
_add_sector(
    "高端制造",
    [
        "专用机械",
        "电气设备",
        "机械基件",
        "机床制造",
        "工程机械",
        "纺织机械",
        "化工机械",
        "轻工机械",
        "电器仪表",
        "运输设备",
        "汽车配件",
        "汽车整车",
        "摩托车",
        "航空",
        "船舶",
        "农用机械",
    ],
)
_add_sector(
    "周期资源",
    [
        "铝",
        "铜",
        "铅锌",
        "小金属",
        "黄金",
        "普钢",
        "特种钢",
        "钢加工",
        "化工原料",
        "化纤",
        "橡胶",
        "塑料",
        "煤炭开采",
        "焦炭加工",
        "石油开采",
        "石油加工",
        "矿物制品",
        "水泥",
        "玻璃",
        "其他建材",
        "陶瓷",
        "造纸",
        "染料涂料",
    ],
)
_add_sector(
    "消费",
    [
        "食品",
        "白酒",
        "红黄酒",
        "啤酒",
        "软饮料",
        "乳制品",
        "家用电器",
        "家居用品",
        "服饰",
        "纺织",
        "百货",
        "超市连锁",
        "其他商业",
        "商贸代理",
        "商品城",
        "酒店餐饮",
        "旅游服务",
        "旅游景点",
        "文教休闲",
        "日用化工",
    ],
)
_add_sector("医药", ["化学制药", "中成药", "生物制药", "医疗保健", "医药商业"])
_add_sector(
    "地产基建",
    ["区域地产", "全国地产", "房产服务", "建筑工程", "装修装饰", "园区开发", "路桥", "公路", "公共交通"],
)
_add_sector("金融", ["证券", "银行", "多元金融"])
_add_sector("公用事业", ["火力发电", "水力发电", "新型电力", "供气供热", "水务", "环境保护"])
_add_sector("农业", ["农药化肥", "种植业", "农业综合", "饲料", "渔业", "林业"])
_add_sector("交通运输", ["仓储物流", "水运", "空运", "港口", "铁路", "机场"])


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def load_and_enrich(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in PCT_COLS:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace("%", "", regex=False).str.replace("+", "", regex=False),
            errors="coerce",
        )
    df["industry"] = df["公司所属行业"].fillna("未知")
    df["sector"] = df["industry"].map(SECTOR_MAP).fillna("综合/未知")
    df["year"] = df["命中日期"].astype(str).str[:4]
    df["tier"] = pd.Categorical(df["止盈区间"], categories=TIER_ORDER, ordered=True)
    df["is_delayed"] = df["是否延期卖出"].astype(int)
    df["is_win"] = (df["净收益率"] > 0).astype(int)
    df["log2_mv"] = np.log2(df["T日收盘后流通市值(亿元)"])
    df["log2_t_amt"] = np.log2(df["T成交额/T-1成交额"])
    df["log2_t1_amt"] = np.log2(df["T+1成交额/T成交额"])

    # T+1 跳空：由 close_change = intraday * (1 + gap) 反推。
    intr = df["T+1日内涨幅(后复权)"].to_numpy() / 100.0
    close_chg = df["T+1开盘至收盘变化比例(后复权)"].to_numpy() / 100.0
    gap = np.full(len(df), np.nan)
    valid = np.abs(intr) > 1e-12
    gap[valid] = close_chg[valid] / intr[valid] - 1.0
    df["t1_gap"] = gap * 100.0

    df["close_chg_t1"] = df["T+1开盘至收盘变化比例(后复权)"]
    df["upper_shadow"] = df["T+1开盘至最高上行比例(后复权)"] - df["close_chg_t1"].clip(lower=0)
    df["lower_shadow"] = df["T+1开盘至最低下探比例(后复权)"] - (-df["close_chg_t1"]).clip(lower=0)
    df["body"] = df["close_chg_t1"].abs()

    # 以 T+1 开盘为分母的稳健性指标（用于口径说明）。
    denom = 1.0 + gap
    df["t1_dd_from_open"] = np.where(valid, df["T+1开盘至最低下探比例(后复权)"] / denom, np.nan)
    df["t1_up_from_open"] = np.where(valid, df["T+1开盘至最高上行比例(后复权)"] / denom, np.nan)
    return df


def summarize(group: pd.DataFrame) -> pd.Series:
    r = group["净收益率"]
    wins = r[r > 0]
    losses = r[r <= 0]
    return pd.Series(
        {
            "样本数": len(r),
            "均值(%)": r.mean(),
            "中位数(%)": r.median(),
            "胜率(%)": (r > 0).mean() * 100,
            "标准差": r.std(),
            "25分位": r.quantile(0.25),
            "75分位": r.quantile(0.75),
            "平均盈利": wins.mean() if len(wins) else np.nan,
            "平均亏损": losses.mean() if len(losses) else np.nan,
            "总盈利/总亏损": wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.nan,
            "净盈亏合计": group["净盈亏"].sum(),
            "延期卖出率(%)": group["is_delayed"].mean() * 100,
        }
    )


def boot_ci(values: np.ndarray, reps: int = 2000, seed: int = 42) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    n = len(values)
    if n < 5:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    means = values[rng.integers(0, n, (reps, n))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def group_table(df: pd.DataFrame, spec_key: str, name: str) -> pd.DataFrame:
    bins, labels, display = BIN_SPECS[spec_key]
    work = df.copy()
    work["_bin"] = pd.cut(work[spec_key], bins=bins, labels=labels, right=False)
    rows = []
    for bin_label, group in work.groupby("_bin", observed=True):
        base = summarize(group)
        lo, hi = boot_ci(group["净收益率"].to_numpy())
        rows.append(
            {
                "分组": str(bin_label),
                "样本数": int(base["样本数"]),
                "均值(%)": base["均值(%)"],
                "95%CI下限": lo,
                "95%CI上限": hi,
                "中位数(%)": base["中位数(%)"],
                "胜率(%)": base["胜率(%)"],
                "总盈利/总亏损": base["总盈利/总亏损"],
                "净盈亏合计": base["净盈亏合计"],
                "延期卖出率(%)": base["延期卖出率(%)"],
            }
        )
    return pd.DataFrame(rows).rename(columns={"分组": display})


def categorical_table(df: pd.DataFrame, col: str, order: list[str] | None = None) -> pd.DataFrame:
    rows = []
    groups = df.groupby(col, observed=True)
    keys = order if order is not None else sorted(df[col].dropna().unique())
    for key in keys:
        if key not in groups.groups:
            continue
        group = groups.get_group(key)
        base = summarize(group)
        lo, hi = boot_ci(group["净收益率"].to_numpy())
        rows.append(
            {
                col: key,
                "样本数": int(base["样本数"]),
                "均值(%)": base["均值(%)"],
                "95%CI下限": lo,
                "95%CI上限": hi,
                "中位数(%)": base["中位数(%)"],
                "胜率(%)": base["胜率(%)"],
                "总盈利/总亏损": base["总盈利/总亏损"],
                "净盈亏合计": base["净盈亏合计"],
                "延期卖出率(%)": base["延期卖出率(%)"],
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 统计检验：OLS + 聚类稳健标准误 + Wald
# ---------------------------------------------------------------------------


def _gammainc_series(a: float, x: float) -> float:
    ap = a
    s = 1.0 / a
    d = s
    for _ in range(200):
        ap += 1.0
        d *= x / ap
        s += d
        if abs(d) < abs(s) * 1e-14:
            break
    return s * exp(-x + a * log(x) - lgamma(a))


def _gammaincc_cf(a: float, x: float) -> float:
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 200):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return exp(-x + a * log(x) - lgamma(a)) * h


def chi2_sf(x: float, df: int) -> float:
    if x <= 0:
        return 1.0
    if x < df + 1:
        return 1.0 - _gammainc_series(df / 2.0, x / 2.0)
    return _gammaincc_cf(df / 2.0, x / 2.0)


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / sqrt(2.0))


def ols(y: np.ndarray, x: np.ndarray, cluster: np.ndarray | None = None) -> dict:
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    n, k = x.shape
    xtx_inv = np.linalg.pinv(x.T @ x)
    beta = xtx_inv @ (x.T @ y)
    resid = y - x @ beta
    r2 = 1.0 - (resid @ resid) / ((y - y.mean()) @ (y - y.mean()))
    adj = 1.0 - (1.0 - r2) * (n - 1) / (n - k)
    if cluster is None:
        cov = xtx_inv * (resid @ resid) / (n - k)
    else:
        meat = np.zeros((k, k))
        cluster = np.asarray(cluster)
        for _, idx in pd.Series(cluster).groupby(cluster).groups.items():
            xg = x[idx]
            ug = resid[idx]
            score = xg.T @ ug
            meat += np.outer(score, score)
        groups = pd.Series(cluster).nunique()
        cov = xtx_inv @ meat @ xtx_inv * (groups / (groups - 1)) * ((n - 1) / (n - k))
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    t = beta / np.where(se > 0, se, np.nan)
    p = np.array([2 * norm_sf(abs(z)) if np.isfinite(z) else np.nan for z in t])
    return {"beta": beta, "se": se, "t": t, "p": p, "r2": r2, "adj": adj, "n": n, "k": k, "cov": cov}


def wald(fit: dict, idx: list[int]) -> tuple[float, int, float]:
    b = fit["beta"][idx]
    v = fit["cov"][np.ix_(idx, idx)]
    stat = float(b @ np.linalg.pinv(v) @ b)
    q = len(idx)
    return stat, q, chi2_sf(stat, q)


def design(*parts: tuple[np.ndarray, list[str]]) -> tuple[np.ndarray, list[str]]:
    cols = []
    names = []
    n = np.asarray(parts[0][0]).shape[0]
    cols.append(np.ones(n))
    names.append("const")
    for arr, nm in parts:
        arr = np.asarray(arr, dtype=float)
        if arr.ndim == 1:
            arr = arr[:, None]
        cols.append(arr)
        names.extend(nm)
    return np.column_stack(cols), names


def dummies(series: pd.Series, drop_first: bool = True) -> tuple[np.ndarray, list[str]]:
    data = pd.get_dummies(series.astype(str), drop_first=drop_first, dtype=float)
    return data.to_numpy(), list(data.columns)


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


def build_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    tables: dict[str, pd.DataFrame] = {}

    # 概览
    r = df["净收益率"]
    win = r > 0
    loss = r <= 0
    total_pnl = df["净盈亏"].sum()
    tables["概览"] = pd.DataFrame(
        {
            "指标": [
                "样本数",
                "命中日期数",
                "区间起点",
                "区间终点",
                "净收益率均值(%)",
                "净收益率中位数(%)",
                "净收益率标准差",
                "胜率(%)",
                "平均盈利(%)",
                "平均亏损(%)",
                "总盈利/总亏损",
                "平均净盈亏(元)",
                "中位净盈亏(元)",
                "净盈亏合计(元)",
                "延期卖出样本",
                "延期卖出均值(%)",
                "非延期卖出均值(%)",
                "毛收益率均值(%)",
                "净收益率均值-毛收益率均值(pp)",
                "净收益率1%分位",
                "净收益率5%分位",
                "净收益率95%分位",
                "净收益率99%分位",
            ],
            "数值": [
                len(df),
                df["命中日期"].nunique(),
                df["命中日期"].min(),
                df["命中日期"].max(),
                r.mean(),
                r.median(),
                r.std(),
                win.mean() * 100,
                r[win].mean(),
                r[loss].mean(),
                df.loc[win, "净盈亏"].sum() / abs(df.loc[loss, "净盈亏"].sum()),
                df["净盈亏"].mean(),
                df["净盈亏"].median(),
                total_pnl,
                int(df["is_delayed"].sum()),
                df.loc[df["is_delayed"] == 1, "净收益率"].mean(),
                df.loc[df["is_delayed"] == 0, "净收益率"].mean(),
                df["毛收益率"].mean(),
                r.mean() - df["毛收益率"].mean(),
                r.quantile(0.01),
                r.quantile(0.05),
                r.quantile(0.95),
                r.quantile(0.99),
            ],
        }
    )

    # 分年
    rows = []
    for year, group in df.groupby("year"):
        base = summarize(group)
        rows.append({"年份": year, **base.to_dict()})
    tables["分年"] = pd.DataFrame(rows)

    # 卖出方式
    tables["卖出方式"] = categorical_table(df, "卖出方式")

    # 止盈分档
    tables["止盈分档"] = categorical_table(df, "止盈区间", TIER_ORDER)

    # 行业：原始 + 剔除年份/分档后的调整值
    y = df["净收益率"].to_numpy(float)
    year_d = dummies(df["year"])[0]
    tier_d = dummies(df["tier"].astype(str))[0]
    x_base = np.column_stack([np.ones(len(df)), year_d, tier_d])
    beta = np.linalg.pinv(x_base.T @ x_base) @ (x_base.T @ y)
    df["_resid_yt"] = y - x_base @ beta
    df["_adj_ret"] = df["_resid_yt"] + y.mean()

    rows = []
    industry_counts = df["industry"].value_counts()
    kept = [name for name, count in industry_counts.items() if count >= 20]
    for industry in kept:
        group = df[df["industry"] == industry]
        raw_lo, raw_hi = boot_ci(group["净收益率"].to_numpy())
        adj_lo, adj_hi = boot_ci(group["_adj_ret"].to_numpy())
        rows.append(
            {
                "行业": industry,
                "样本数": len(group),
                "原始均值(%)": group["净收益率"].mean(),
                "原始95%CI下限": raw_lo,
                "原始95%CI上限": raw_hi,
                "调整后均值(%)": group["_adj_ret"].mean(),
                "调整后95%CI下限": adj_lo,
                "调整后95%CI上限": adj_hi,
                "胜率(%)": (group["净收益率"] > 0).mean() * 100,
                "总盈利/总亏损": group.loc[group["净收益率"] > 0, "净收益率"].sum()
                / abs(group.loc[group["净收益率"] <= 0, "净收益率"].sum()),
            }
        )
    tables["行业"] = pd.DataFrame(rows).sort_values("原始均值(%)", ascending=False)

    rows = []
    for sector, group in df.groupby("sector"):
        raw_lo, raw_hi = boot_ci(group["净收益率"].to_numpy())
        adj_lo, adj_hi = boot_ci(group["_adj_ret"].to_numpy())
        rows.append(
            {
                "板块": sector,
                "样本数": len(group),
                "原始均值(%)": group["净收益率"].mean(),
                "原始95%CI下限": raw_lo,
                "原始95%CI上限": raw_hi,
                "调整后均值(%)": group["_adj_ret"].mean(),
                "调整后95%CI下限": adj_lo,
                "调整后95%CI上限": adj_hi,
                "胜率(%)": (group["净收益率"] > 0).mean() * 100,
            }
        )
    tables["板块"] = pd.DataFrame(rows).sort_values("原始均值(%)", ascending=False)

    # 各字段分档
    for key in BIN_SPECS:
        tables[BIN_SPECS[key][2]] = group_table(df, key, BIN_SPECS[key][2])

    # K线
    tables["T+1K线"] = categorical_table(df, "T+1收盘K线形态", ["阳线", "阴线", "开=收"])

    # T+1 跳空的精确分组（把平开单独拆出；固定分箱会把 0 并进 0~2）
    gap = df["t1_gap"]
    gap_labels = np.select(
        [
            gap < -5,
            (gap >= -5) & (gap < -2),
            (gap >= -2) & (gap < -1e-9),
            gap.abs() <= 1e-9,
            (gap > 1e-9) & (gap <= 2),
            (gap > 2) & (gap <= 5),
            gap > 5,
        ],
        ["<-5", "-5~-2", "-2~0", "=0(平开)", "0~2", "2~5", ">5"],
        default="",
    )
    gap_series = pd.Series(gap_labels, index=df.index)
    gap_series = gap_series[gap_series != ""]
    rows = []
    for label in ["<-5", "-5~-2", "-2~0", "=0(平开)", "0~2", "2~5", ">5"]:
        group = df.loc[gap_series[gap_series == label].index]
        if len(group) == 0:
            continue
        base = summarize(group)
        lo, hi = boot_ci(group["净收益率"].to_numpy())
        rows.append(
            {
                "T+1跳空(精确)": label,
                "样本数": int(base["样本数"]),
                "均值(%)": base["均值(%)"],
                "95%CI下限": lo,
                "95%CI上限": hi,
                "中位数(%)": base["中位数(%)"],
                "胜率(%)": base["胜率(%)"],
                "净盈亏合计": base["净盈亏合计"],
                "延期卖出率(%)": base["延期卖出率(%)"],
            }
        )
    tables["T+1跳空(精确)"] = pd.DataFrame(rows)

    # 交叉表
    def cross_mean(index: str, col: str, bins: list[float], labels: list[str], name: str) -> pd.DataFrame:
        work = df.copy()
        work["_bin"] = pd.cut(work[col], bins=bins, labels=labels, right=False)
        mean = work.pivot_table(index=index, columns="_bin", values="净收益率", aggfunc="mean", observed=True)
        count = work.pivot_table(index=index, columns="_bin", values="净收益率", aggfunc="count", observed=True)
        out = []
        for idx in mean.index:
            row = {"分组": idx}
            for bin_label in mean.columns:
                value = mean.loc[idx, bin_label]
                n = count.loc[idx, bin_label]
                row[str(bin_label)] = f"{value:+.2f}% (n={int(n)})" if pd.notna(value) else ""
            out.append(row)
        result = pd.DataFrame(out)
        result["_order"] = result["分组"].map({v: i for i, v in enumerate(TIER_ORDER)})
        result = result.sort_values("_order").drop(columns="_order").reset_index(drop=True)
        return result

    tables["分档x量能"] = cross_mean(
        "止盈区间", "T+1成交额/T成交额", [0, 0.55, 0.65, 0.8, np.inf], ["<0.55", "0.55-0.65", "0.65-0.8", "0.8+"], "分档x量能"
    )
    tables["分档x上冲"] = cross_mean(
        "止盈区间", "T+1开盘至最高上行比例(后复权)", [0, 2, 4, 6, 9, np.inf], ["0-2", "2-4", "4-6", "6-9", "9+"], "分档x上冲"
    )
    tables["分档x下探"] = cross_mean(
        "止盈区间", "T+1开盘至最低下探比例(后复权)", [0, 1, 2, 3, 5, np.inf], ["0-1", "1-2", "2-3", "3-5", "5+"], "分档x下探"
    )

    # K线 x 量能
    work = df.copy()
    work["_vol"] = pd.cut(work["T+1成交额/T成交额"], [0, 0.65, 0.8, np.inf], labels=["<0.65", "0.65-0.8", "0.8+"], right=False)
    rows = []
    for (kline, vol), group in work.groupby(["T+1收盘K线形态", "_vol"], observed=True):
        rows.append({"K线": kline, "量能": str(vol), "样本数": len(group), "均值(%)": group["净收益率"].mean(), "胜率(%)": (group["净收益率"] > 0).mean() * 100})
    tables["K线x量能"] = pd.DataFrame(rows)

    # 方差分解
    def fit_r2(x_parts: list[tuple[np.ndarray, list[str]]]) -> tuple[float, float, int]:
        x, _ = design(*x_parts)
        fit = ols(y, x)
        return fit["r2"], fit["adj"], fit["k"]

    year_part = (year_d, dummies(df["year"])[1])
    tier_part = (tier_d, dummies(df["tier"].astype(str))[1])
    sell_d, sell_n = dummies(df["卖出方式"])
    ind_d, ind_n = dummies(
        pd.Series(np.where(df["industry"].isin(kept), df["industry"], "其他/未知")),
    )
    t2 = df[["T+2收盘对T+1开盘涨幅(后复权)", "T+2最高对T+1开盘涨幅(后复权)"]].to_numpy(float)

    rows = []
    specs = [
        ("年份", [year_part]),
        ("年份+行业", [year_part, (ind_d, ind_n)]),
        ("年份+止盈分档", [year_part, tier_part]),
        ("年份+止盈分档+卖出方式", [year_part, tier_part, (sell_d, sell_n)]),
        ("年份+止盈分档+T+2收盘/最高", [year_part, tier_part, (t2, ["T+2收盘", "T+2最高"])]),
    ]
    for name, parts in specs:
        r2, adj, k = fit_r2(parts)
        rows.append({"模型": name, "R²": r2, "调整R²": adj, "参数个数": k})
    tables["方差分解"] = pd.DataFrame(rows)

    # 特征强度
    feature_specs = [
        ("公司所属行业", "industry", "cat"),
        ("T日收盘后流通市值(亿元)", "T日收盘后流通市值(亿元)", "T日收盘后流通市值(亿元)"),
        ("T成交额/T-1成交额", "T成交额/T-1成交额", "T成交额/T-1成交额"),
        ("T日开盘相对T-1收盘(后复权)", "T日开盘相对T-1收盘(后复权)", "T日开盘相对T-1收盘(后复权)"),
        ("T日最低相对T-1收盘(后复权)", "T日最低相对T-1收盘(后复权)", "T日最低相对T-1收盘(后复权)"),
        ("T+1成交额/T成交额", "T+1成交额/T成交额", "T+1成交额/T成交额"),
        ("T+1收盘K线形态", "T+1收盘K线形态", "cat"),
        ("T+1日内涨幅(后复权)", "T+1日内涨幅(后复权)", "T+1日内涨幅(后复权)"),
        ("T+1开盘至最低下探比例(后复权)", "T+1开盘至最低下探比例(后复权)", "T+1开盘至最低下探比例(后复权)"),
        ("T+1开盘至最高上行比例(后复权)", "T+1开盘至最高上行比例(后复权)", "T+1开盘至最高上行比例(后复权)"),
        ("T+1开盘至收盘变化比例(后复权)", "T+1开盘至收盘变化比例(后复权)", "T+1开盘至收盘变化比例(后复权)"),
        ("T+1隐含跳空", "t1_gap", "t1_gap"),
        ("上影线", "upper_shadow", "upper_shadow"),
        ("下影线", "lower_shadow", "lower_shadow"),
        ("实体", "body", "body"),
    ]

    rows = []
    for display, col, kind in feature_specs:
        if kind == "cat":
            feat_series = df[col] if col in ("industry", "T+1收盘K线形态") else df[col]
            if col == "industry":
                feat_series = pd.Series(np.where(df["industry"].isin(kept), df["industry"], "其他/未知"))
            feat_series = feat_series.astype(str)
            mask = np.ones(len(df), dtype=bool)
        else:
            feat_series = pd.qcut(df[col], 10, duplicates="drop")
            mask = np.asarray(pd.notna(feat_series))
            feat_series = feat_series.astype(str)
        feat_d, feat_n = dummies(feat_series[mask])
        yy = y[mask]
        cl = df.loc[mask, "命中日期"].to_numpy()

        x_uni, _ = design((feat_d, feat_n))
        eta2 = ols(yy, x_uni)["r2"]

        y_d = dummies(df.loc[mask, "year"])[0]
        x_entry, _ = design((y_d, dummies(df.loc[mask, "year"])[1]))
        f_entry = ols(yy, x_entry, cl)
        x_entry_full, _ = design((y_d, dummies(df.loc[mask, "year"])[1]), (feat_d, feat_n))
        f_entry_full = ols(yy, x_entry_full, cl)

        t_d = dummies(df.loc[mask, "tier"].astype(str))[0]
        t_n = dummies(df.loc[mask, "tier"].astype(str))[1]
        x_exit, _ = design((y_d, dummies(df.loc[mask, "year"])[1]), (t_d, t_n))
        f_exit = ols(yy, x_exit, cl)
        x_exit_full, names_exit = design((y_d, dummies(df.loc[mask, "year"])[1]), (t_d, t_n), (feat_d, feat_n))
        f_exit_full = ols(yy, x_exit_full, cl)
        idx = list(range(len(names_exit) - len(feat_n), len(names_exit)))
        _, _, p_value = wald(f_exit_full, idx)

        rows.append(
            {
                "字段": display,
                "样本数": int(mask.sum()),
                "单变量R²": eta2,
                "加入年份后ΔR²": f_entry_full["r2"] - f_entry["r2"],
                "再控制止盈分档后ΔR²": f_exit_full["r2"] - f_exit["r2"],
                "分档控制后p值": p_value,
            }
        )
    tables["特征强度"] = pd.DataFrame(rows)

    # 分档内回归
    rows = []
    for tier in TIER_ORDER:
        sub = df[df["止盈区间"] == tier].copy()
        sub = sub[sub["t1_gap"].notna()]
        if len(sub) < 100:
            continue
        yy = sub["净收益率"].to_numpy(float)
        cl = sub["命中日期"].to_numpy()
        y_d, y_n = dummies(sub["year"])
        x_base, base_n = design((y_d, y_n))
        base_fit = ols(yy, x_base, cl)
        for col, display in [
            ("log2_t1_amt", "T+1量比(log2)"),
            ("T+1开盘至最低下探比例(后复权)", "T+1下探(%)"),
            ("T+1开盘至最高上行比例(后复权)", "T+1上冲(%)"),
            ("t1_gap", "T+1隐含跳空(%)"),
        ]:
            z = sub[col].to_numpy(float)
            z = (z - z.mean()) / z.std()
            x_full, names = design((y_d, y_n), (z, [display]))
            fit = ols(yy, x_full, cl)
            idx = [len(names) - 1]
            b = fit["beta"][idx[0]]
            se = fit["se"][idx[0]]
            p = fit["p"][idx[0]]
            rows.append(
                {
                    "止盈分档": tier,
                    "样本数": len(sub),
                    "变量": display,
                    "标准化系数(pp/1SD)": b,
                    "聚类稳健SE": se,
                    "p值": p,
                    "模型R²": fit["r2"],
                    "基础R²": base_fit["r2"],
                }
            )
    tables["分档内回归"] = pd.DataFrame(rows)

    # 年度稳定性
    splits = [
        ("T+1量比>=0.8 vs <0.8", df["T+1成交额/T成交额"] >= 0.8, df["T+1成交额/T成交额"] < 0.8),
        ("T+1下探<=2% vs >2%", df["T+1开盘至最低下探比例(后复权)"] <= 2, df["T+1开盘至最低下探比例(后复权)"] > 2),
        ("T+1上冲>=6% vs <6%", df["T+1开盘至最高上行比例(后复权)"] >= 6, df["T+1开盘至最高上行比例(后复权)"] < 6),
        ("阳线 vs 阴线", df["T+1收盘K线形态"] == "阳线", df["T+1收盘K线形态"] == "阴线"),
        ("流通市值<20亿 vs 20-300亿", df["T日收盘后流通市值(亿元)"] < 20, (df["T日收盘后流通市值(亿元)"] >= 20) & (df["T日收盘后流通市值(亿元)"] < 300)),
        ("T量比>10 vs 2.5-3", df["T成交额/T-1成交额"] > 10, (df["T成交额/T-1成交额"] >= 2.5) & (df["T成交额/T-1成交额"] < 3)),
        ("T高开0-4% vs 4-8%", (df["T日开盘相对T-1收盘(后复权)"] >= 0) & (df["T日开盘相对T-1收盘(后复权)"] < 4), (df["T日开盘相对T-1收盘(后复权)"] >= 4) & (df["T日开盘相对T-1收盘(后复权)"] < 8)),
        ("T最低>=2% vs 0-1%", df["T日最低相对T-1收盘(后复权)"] >= 2, df["T日最低相对T-1收盘(后复权)"] < 1),
        ("科技硬件 vs 周期资源", df["sector"] == "科技硬件", df["sector"] == "周期资源"),
        ("T+1跳空<0 vs >=0", df["t1_gap"] < -1e-9, df["t1_gap"] >= -1e-9),
    ]
    rows = []
    for name, high_mask, low_mask in splits:
        high_mask = high_mask.fillna(False)
        low_mask = low_mask.fillna(False)
        mask = high_mask | low_mask
        work = df[mask].copy()
        work["_grp"] = np.where(high_mask[mask], "high", "low")
        yearly = work.groupby(["year", "_grp"])["净收益率"].mean().unstack()
        if "high" not in yearly or "low" not in yearly:
            continue
        diff = (yearly["high"] - yearly["low"]).dropna()
        rows.append(
            {
                "对比": name,
                "年度均值差(pp)": diff.mean(),
                "方向为正的年份占比(%)": (diff > 0).mean() * 100,
                "最差年份差": diff.min(),
                "最好年份差": diff.max(),
                "有效年份数": len(diff),
            }
        )
    tables["年度稳定性"] = pd.DataFrame(rows)

    # 延期卖出
    rows = []
    for name, col, bins, labels in [
        ("T+1量能", "T+1成交额/T成交额", [0, 0.65, 0.8, np.inf], ["<0.65", "0.65-0.8", "0.8+"]),
        ("T量比", "T成交额/T-1成交额", [0, 2.5, 3, 5, 10, np.inf], ["<2.5", "2.5-3", "3-5", "5-10", "10+"]),
        ("T+1下探", "T+1开盘至最低下探比例(后复权)", [0, 1, 2, 3, 5, np.inf], ["0-1", "1-2", "2-3", "3-5", "5+"]),
        ("T+1上冲", "T+1开盘至最高上行比例(后复权)", [0, 2, 4, 6, 9, np.inf], ["0-2", "2-4", "4-6", "6-9", "9+"]),
    ]:
        work = df.copy()
        work["_bin"] = pd.cut(work[col], bins=bins, labels=labels, right=False)
        for bin_label, group in work.groupby("_bin", observed=True):
            rows.append({"维度": name, "分组": str(bin_label), "样本数": len(group), "延期卖出数": int(group["is_delayed"].sum()), "延期卖出率(%)": group["is_delayed"].mean() * 100})
    for kline, group in df.groupby("T+1收盘K线形态"):
        rows.append({"维度": "T+1K线", "分组": kline, "样本数": len(group), "延期卖出数": int(group["is_delayed"].sum()), "延期卖出率(%)": group["is_delayed"].mean() * 100})
    for tier, group in df.groupby("止盈区间", observed=True):
        rows.append({"维度": "止盈分档", "分组": tier, "样本数": len(group), "延期卖出数": int(group["is_delayed"].sum()), "延期卖出率(%)": group["is_delayed"].mean() * 100})
    tables["延期风险"] = pd.DataFrame(rows)

    return tables


# ---------------------------------------------------------------------------
# 图表
# ---------------------------------------------------------------------------


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
            "axes.edgecolor": "#666666",
            "axes.labelcolor": "#333333",
            "xtick.color": "#333333",
            "ytick.color": "#333333",
        }
    )


COLORS = {
    "blue": "#3B6FB6",
    "orange": "#E07B39",
    "red": "#C0392B",
    "green": "#2E8B57",
    "gray": "#8C8C8C",
    "purple": "#7B5EA7",
}


def annotate_bars(ax, bars, values, n_values, fmt="{:+.1f}%") -> None:
    for bar, value, _n in zip(bars, values, n_values):
        if not np.isfinite(value):
            continue
        y = bar.get_height()
        va = "bottom" if y >= 0 else "top"
        offset = 0.25 if y >= 0 else -0.25
        ax.text(bar.get_x() + bar.get_width() / 2, y + offset, fmt.format(value), ha="center", va=va, fontsize=8, color="#333333")


def bar_with_ci(ax, table: pd.DataFrame, x_col: str, title: str, color: str = COLORS["blue"]) -> None:
    x = np.arange(len(table))
    values = table["均值(%)"].to_numpy()
    lo = table["95%CI下限"].to_numpy()
    hi = table["95%CI上限"].to_numpy()
    err = np.vstack([values - lo, hi - values])
    bars = ax.bar(x, values, color=color, alpha=0.82, edgecolor="white", linewidth=0.8)
    ax.errorbar(x, values, yerr=err, fmt="none", ecolor="#333333", elinewidth=0.9, capsize=2.5)
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.axhline(0.18, color=COLORS["orange"], linewidth=0.9, linestyle=":")
    ax.set_xticks(x)
    ax.set_xticklabels(table[x_col].astype(str), rotation=0, fontsize=8)
    ax.set_title(title, fontsize=10, pad=8)
    ax.set_ylabel("净收益率均值 (%)", fontsize=8)
    annotate_bars(ax, bars, values, table["样本数"].to_numpy())


def make_figures(df: pd.DataFrame, tables: dict[str, pd.DataFrame], fig_dir: Path) -> None:
    setup_plot()
    fig_dir.mkdir(parents=True, exist_ok=True)

    # fig1 分布 + 盈亏集中度
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 4.4))
    ax = axes[0]
    vals = df["净收益率"].clip(-20, 25)
    ax.hist(vals, bins=np.arange(-20, 25.5, 0.5), color=COLORS["blue"], alpha=0.82, edgecolor="white", linewidth=0.3)
    ax.axvline(df["净收益率"].mean(), color=COLORS["red"], linestyle="--", linewidth=1.3, label=f"均值 {df['净收益率'].mean():+.2f}%")
    ax.axvline(df["净收益率"].median(), color=COLORS["green"], linestyle="--", linewidth=1.3, label=f"中位数 {df['净收益率'].median():+.2f}%")
    ax.set_xlim(-20, 25)
    ax.set_title("净收益率分布：尖峰与左尾", fontsize=11)
    ax.set_xlabel("净收益率 (%)")
    ax.set_ylabel("样本数")
    ax.legend(fontsize=8, frameon=False)

    ax = axes[1]
    pnl = np.sort(df["净盈亏"].to_numpy())[::-1] / 10000.0
    cum = np.cumsum(pnl)
    pct = np.arange(1, len(pnl) + 1) / len(pnl) * 100
    ax.plot(pct, cum, color=COLORS["blue"], linewidth=1.6)
    ax.axhline(df["净盈亏"].sum() / 10000.0, color=COLORS["gray"], linestyle=":", linewidth=1.0)
    k = int(len(pnl) * 0.1)
    ax.scatter([10], [cum[k - 1]], color=COLORS["green"], zorder=5, s=30)
    ax.scatter([90], [cum[-k - 1]], color=COLORS["red"], zorder=5, s=30)
    ax.annotate(f"前10%合计 {cum[k-1]:+.0f}万", xy=(10, cum[k - 1]), xytext=(12, cum[k - 1] + 80), fontsize=8, color=COLORS["green"])
    ax.annotate(
        f"最后10%合计 {pnl[-k:].sum():+.0f}万\n（90%分位累计 {cum[-k-1]:+.0f}万）",
        xy=(90, cum[-k - 1]),
        xytext=(56, cum[-k - 1] - 240),
        fontsize=8,
        color=COLORS["red"],
    )
    ax.set_title("按单笔净盈亏排序的累计曲线（万元）", fontsize=11)
    ax.set_xlabel("交易百分位（从最好到最差）")
    ax.set_ylabel("累计净盈亏（万元）")
    fig.tight_layout()
    fig.savefig(fig_dir / "fig1_return_distribution.png", bbox_inches="tight")
    plt.close(fig)

    # fig2 卖出方式
    mode = tables["卖出方式"].copy()
    order = ["T+2 开盘", "T+2 盘中", "T+2 收盘", "顺延1日开盘", "顺延2日开盘", "顺延6日开盘", "顺延10日开盘"]
    mode["_order"] = mode["卖出方式"].map({v: i for i, v in enumerate(order)})
    mode = mode.sort_values("_order")
    mode = mode[mode["样本数"] >= 5]
    fig, ax = plt.subplots(figsize=(9.2, 4.3))
    colors = [COLORS["green"], COLORS["blue"], COLORS["red"], COLORS["orange"], COLORS["orange"], COLORS["orange"], COLORS["orange"]]
    x = np.arange(len(mode))
    bars = ax.bar(x, mode["均值(%)"], color=colors[: len(mode)], alpha=0.85, edgecolor="white")
    ax.axhline(0, color="#333333", linewidth=0.8)
    ax.axhline(df["净收益率"].mean(), color=COLORS["gray"], linestyle="--", linewidth=1.0, label=f"全体均值 {df['净收益率'].mean():+.2f}%")
    for bar, value, n, win in zip(bars, mode["均值(%)"], mode["样本数"], mode["胜率(%)"]):
        va = "bottom" if value >= 0 else "top"
        offset = 0.25 if value >= 0 else -0.25
        ax.text(bar.get_x() + bar.get_width() / 2, value + offset, f"{value:+.2f}%\nn={int(n)}, 胜率{win:.0f}%", ha="center", va=va, fontsize=7.5)
    ax.set_xticks(x)
    ax.set_xticklabels(mode["卖出方式"], fontsize=8)
    ax.set_ylabel("净收益率均值 (%)")
    ax.set_title("卖出方式决定净收益：T+2 开盘/盘中/收盘三种分支差异巨大", fontsize=11)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig2_exit_modes.png", bbox_inches="tight")
    plt.close(fig)

    # fig3 T+1 状态 2x2
    fig, axes = plt.subplots(2, 2, figsize=(12.2, 8.0))
    specs = [
        ("T+1成交额/T成交额", "T+1量能（相对 T 日）", COLORS["blue"]),
        ("T+1下探比例(%)", "T+1 开盘至最低下探（%）", COLORS["red"]),
        ("T+1上冲比例(%)", "T+1 开盘至最高上行（%）", COLORS["green"]),
        ("T+1K线", "T+1 收盘 K 线", COLORS["purple"]),
    ]
    for ax, (table_key, title, color) in zip(axes.ravel(), specs):
        table = tables[table_key]
        bar_with_ci(ax, table, table.columns[0], title, color)
    fig.suptitle("T+1 收盘状态：量能、下探、上冲、K 线对净收益的单调影响", fontsize=13, y=1.01)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig3_t1_state.png", bbox_inches="tight")
    plt.close(fig)

    # fig4 分档内交互
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 4.4))
    for ax, key, title in [
        (axes[0], "分档x量能", "止盈分档 × T+1 量能"),
        (axes[1], "分档x上冲", "止盈分档 × T+1 上冲"),
    ]:
        table = tables[key].copy().set_index("分组")
        labels = list(table.columns)
        values = np.full(table.shape, np.nan)
        counts = np.zeros(table.shape)
        for i in range(table.shape[0]):
            for j in range(table.shape[1]):
                text = str(table.iloc[i, j])
                if not text or text == "nan":
                    continue
                try:
                    value = float(text.split("%")[0])
                    n = int(text.split("n=")[1].rstrip(")"))
                    values[i, j] = value
                    counts[i, j] = n
                except Exception:
                    pass
        norm = TwoSlopeNorm(vmin=-10, vcenter=0, vmax=12)
        im = ax.imshow(values, cmap="RdYlGn", norm=norm, aspect="auto")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_yticks(range(len(table.index)))
        ax.set_yticklabels(table.index, fontsize=8)
        for i in range(table.shape[0]):
            for j in range(table.shape[1]):
                if np.isfinite(values[i, j]):
                    ax.text(j, i, f"{values[i,j]:+.1f}%\nn={int(counts[i,j])}", ha="center", va="center", fontsize=7.5, color="#222222")
        ax.set_title(title, fontsize=10)
        ax.grid(False)
    fig.colorbar(im, ax=axes, shrink=0.75, label="净收益率均值 (%)")
    fig.suptitle("分档内的交互：量能/上冲并非总是越大越好", fontsize=13, y=1.03)
    fig.savefig(fig_dir / "fig4_tier_interaction.png", bbox_inches="tight")
    plt.close(fig)

    # fig5 T 日字段
    fig, axes = plt.subplots(2, 2, figsize=(12.2, 8.0))
    specs = [
        ("流通市值(亿元)", "流通市值（亿元）", COLORS["blue"]),
        ("T成交额/T-1成交额", "T 成交额 / T-1 成交额", COLORS["orange"]),
        ("T日高开(%)", "T 日高开（%）", COLORS["purple"]),
        ("T日最低相对前收(%)", "T 日最低相对前收（%）", COLORS["green"]),
    ]
    for ax, (table_key, title, color) in zip(axes.ravel(), specs):
        table = tables[table_key]
        bar_with_ci(ax, table, table.columns[0], title, color)
    fig.suptitle("T 日盘后可知字段：对净收益的解释力很弱", fontsize=13, y=1.01)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig5_tday_fields.png", bbox_inches="tight")
    plt.close(fig)

    # fig6 行业
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 6.2), gridspec_kw={"width_ratios": [1.25, 1.0]})
    industry = tables["行业"].copy()
    industry = industry.sort_values("调整后均值(%)")
    industry = pd.concat([industry.head(8), industry.tail(8)]).drop_duplicates("行业")
    industry = industry.sort_values("调整后均值(%)")
    ax = axes[0]
    y = np.arange(len(industry))
    colors = [COLORS["green"] if v > 0 else COLORS["red"] for v in industry["调整后均值(%)"]]
    ax.errorbar(
        industry["调整后均值(%)"],
        y,
        xerr=[industry["调整后均值(%)"] - industry["调整后95%CI下限"], industry["调整后95%CI上限"] - industry["调整后均值(%)"]],
        fmt="o",
        color=COLORS["gray"],
        ecolor=COLORS["gray"],
        elinewidth=1.0,
        capsize=2.5,
        markersize=4,
    )
    ax.scatter(industry["调整后均值(%)"], y, c=colors, s=36, zorder=5)
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{row['行业']} (n={int(row['样本数'])})" for _, row in industry.iterrows()], fontsize=8)
    ax.set_xlabel("剔除年份 + 止盈分档后的调整均值 (%)")
    ax.set_title("行业：调整后均值 Top8 / Bottom8", fontsize=10)
    ax.grid(axis="x", alpha=0.2)

    sector = tables["板块"].sort_values("调整后均值(%)")
    ax = axes[1]
    y = np.arange(len(sector))
    colors = [COLORS["green"] if v > 0 else COLORS["red"] for v in sector["调整后均值(%)"]]
    ax.errorbar(
        sector["调整后均值(%)"],
        y,
        xerr=[sector["调整后均值(%)"] - sector["调整后95%CI下限"], sector["调整后95%CI上限"] - sector["调整后均值(%)"]],
        fmt="o",
        color=COLORS["gray"],
        ecolor=COLORS["gray"],
        elinewidth=1.0,
        capsize=2.5,
        markersize=4,
    )
    ax.scatter(sector["调整后均值(%)"], y, c=colors, s=36, zorder=5)
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{row['板块']} (n={int(row['样本数'])})" for _, row in sector.iterrows()], fontsize=8)
    ax.set_xlabel("剔除年份 + 止盈分档后的调整均值 (%)")
    ax.set_title("板块：科技硬件相对最强", fontsize=10)
    ax.grid(axis="x", alpha=0.2)
    fig.suptitle("行业与板块：整体联合检验不显著，只有少数组别 CI 不跨 0", fontsize=13, y=1.01)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig6_industry.png", bbox_inches="tight")
    plt.close(fig)

    # fig7 年度稳定性
    fig, axes = plt.subplots(2, 2, figsize=(12.2, 7.8), sharey=False)
    split_specs = [
        ("T+1量比", df["T+1成交额/T成交额"] >= 0.8, df["T+1成交额/T成交额"] < 0.8, "T+1 量能 ≥0.8 vs <0.8"),
        ("T+1下探", df["T+1开盘至最低下探比例(后复权)"] <= 2, df["T+1开盘至最低下探比例(后复权)"] > 2, "T+1 下探 ≤2% vs >2%"),
        ("T+1上冲", df["T+1开盘至最高上行比例(后复权)"] >= 6, df["T+1开盘至最高上行比例(后复权)"] < 6, "T+1 上冲 ≥6% vs <6%"),
        ("K线", df["T+1收盘K线形态"] == "阳线", df["T+1收盘K线形态"] == "阴线", "阳线 vs 阴线"),
    ]
    for ax, (_, high_mask, low_mask, title) in zip(axes.ravel(), split_specs):
        high_mask = high_mask.fillna(False)
        low_mask = low_mask.fillna(False)
        work = df[high_mask | low_mask].copy()
        work["_grp"] = np.where(high_mask[high_mask | low_mask], "high", "low")
        yearly = work.groupby(["year", "_grp"])["净收益率"].mean().unstack()
        ax.plot(yearly.index, yearly["high"], marker="o", color=COLORS["green"], linewidth=1.6, label="高/阳线")
        ax.plot(yearly.index, yearly["low"], marker="o", color=COLORS["red"], linewidth=1.6, label="低/阴线")
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel("净收益率均值 (%)", fontsize=8)
        ax.tick_params(axis="x", labelsize=8)
        ax.legend(fontsize=7.5, frameon=False)
    fig.suptitle("核心 T+1 状态的年度稳定性：9/9 年方向一致", fontsize=13, y=1.01)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig7_year_stability.png", bbox_inches="tight")
    plt.close(fig)

    # fig8 分支概率
    fig, axes = plt.subplots(2, 2, figsize=(12.2, 8.0))
    work = df.copy()
    work["_mode"] = work["卖出方式"].replace(
        {"顺延1日开盘": "顺延开盘", "顺延2日开盘": "顺延开盘", "顺延6日开盘": "顺延开盘", "顺延10日开盘": "顺延开盘"}
    )
    mode_order = ["T+2 开盘", "T+2 盘中", "T+2 收盘", "顺延开盘"]
    mode_colors = [COLORS["green"], COLORS["blue"], COLORS["red"], COLORS["orange"]]
    specs = [
        ("T+1量能", pd.cut(work["T+1成交额/T成交额"], [0, 0.65, 0.8, np.inf], labels=["<0.65", "0.65-0.8", "0.8+"], right=False)),
        ("T+1K线", pd.Categorical(work["T+1收盘K线形态"], categories=["阳线", "阴线", "开=收"], ordered=True)),
        ("T+1下探", pd.cut(work["T+1开盘至最低下探比例(后复权)"], [0, 0.79, 2.21, 4.02, np.inf], labels=["≤0.79", "0.79-2.21", "2.21-4.02", ">4.02"], right=False)),
        ("T+1上冲", pd.cut(work["T+1开盘至最高上行比例(后复权)"], [0, 1.80, 3.87, 7.31, np.inf], labels=["≤1.80", "1.80-3.87", "3.87-7.31", ">7.31"], right=False)),
    ]
    for ax, (title, grouping) in zip(axes.ravel(), specs):
        cross = pd.crosstab(grouping, work["_mode"], normalize="index").reindex(columns=mode_order, fill_value=0) * 100
        if title == "T+1K线":
            cross = cross.reindex(["阳线", "阴线", "开=收"])
        bottom = np.zeros(len(cross))
        x = np.arange(len(cross))
        for mode, color in zip(mode_order, mode_colors):
            values = cross[mode].to_numpy()
            ax.bar(x, values, bottom=bottom, color=color, alpha=0.88, label=mode, width=0.72)
            bottom += values
        ax.set_xticks(x)
        ax.set_xticklabels([str(v) for v in cross.index], fontsize=8, rotation=15, ha="right")
        ax.set_ylabel("占比 (%)", fontsize=8)
        ax.set_ylim(0, 100)
        ax.set_title(title, fontsize=10)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=4, frameon=False, fontsize=8.5, bbox_to_anchor=(0.5, 1.02))
    fig.suptitle("T+1 状态如何改变 T+2 的卖出分支概率", fontsize=13, y=1.06)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig8_branch_probability.png", bbox_inches="tight")
    plt.close(fig)

    # fig9 上下影线
    fig, axes = plt.subplots(1, 2, figsize=(12.2, 4.4))
    work = df.copy()
    work["_sign"] = np.where(work["close_chg_t1"] > 0, "阳线", np.where(work["close_chg_t1"] < 0, "阴线", "开=收"))
    for ax, (col, bins, labels, title) in zip(
        axes,
        [
            ("upper_shadow", [0, 1, 2, 3, 5, np.inf], ["0-1", "1-2", "2-3", "3-5", "5+"], "上影线"),
            ("lower_shadow", [0, 0.5, 1, 2, 3, np.inf], ["0-0.5", "0.5-1", "1-2", "2-3", "3+"], "下影线"),
        ],
    ):
        work["_bin"] = pd.cut(work[col], bins=bins, labels=labels, right=False)
        for sign, color in [("阳线", COLORS["green"]), ("阴线", COLORS["red"])]:
            sub = work[work["_sign"] == sign].groupby("_bin", observed=True)["净收益率"].mean()
            ax.plot(sub.index.astype(str), sub.values, marker="o", color=color, linewidth=1.6, label=sign)
        ax.axhline(0, color="#333333", linewidth=0.8)
        ax.set_title(f"{title} × 收盘阴阳", fontsize=10)
        ax.set_ylabel("净收益率均值 (%)", fontsize=8)
        ax.set_xlabel(f"{title}（%）", fontsize=8)
        ax.legend(fontsize=8, frameon=False)
    fig.suptitle("K 线形态的进一步拆解：阳线 + 小上影最好；阴线各形态都弱", fontsize=12, y=1.03)
    fig.tight_layout()
    fig.savefig(fig_dir / "fig9_shadow_shape.png", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _safe_sheet_name(name: str) -> str:
    for char in "\\/?*[]:":
        name = name.replace(char, "_")
    return name[:31]


def _to_markdown(table: pd.DataFrame) -> str:
    if hasattr(table, "to_markdown"):
        try:
            return table.to_markdown(index=False, floatfmt=".2f")
        except Exception:
            pass
    headers = [str(c) for c in table.columns]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for _, row in table.iterrows():
        values = []
        for column, value in zip(table.columns, row):
            if isinstance(value, float):
                precise = any(key in str(column) for key in ["R²", "p值", "ΔR²", "系数", "SE", "年度均值差"])
                values.append(f"{value:.4f}" if precise else f"{value:.2f}")
            else:
                values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def write_excel(tables: dict[str, pd.DataFrame], df: pd.DataFrame, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, table in tables.items():
            sheet = _safe_sheet_name(name)
            table.to_excel(writer, sheet_name=sheet, index=False)
        cols = [
            "命中日期",
            "股票代码",
            "股票名称",
            "industry",
            "sector",
            "买入日",
            "实际卖出日",
            "T日收盘后流通市值(亿元)",
            "T成交额/T-1成交额",
            "T+1成交额/T成交额",
            "T+1收盘K线形态",
            "T+1日内涨幅(后复权)",
            "T+1开盘至最低下探比例(后复权)",
            "T+1开盘至最高上行比例(后复权)",
            "T+1开盘至收盘变化比例(后复权)",
            "t1_gap",
            "upper_shadow",
            "lower_shadow",
            "body",
            "t1_dd_from_open",
            "t1_up_from_open",
            "止盈区间",
            "卖出方式",
            "净收益率",
            "净盈亏",
        ]
        df[cols].to_excel(writer, sheet_name="明细(衍生字段)", index=False)


def write_tables_markdown(tables: dict[str, pd.DataFrame], path: Path) -> None:
    lines = ["# 分析明细表", ""]
    for name, table in tables.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(_to_markdown(table))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path, help="cash_trades.csv 路径")
    parser.add_argument("--out-dir", type=Path, default=None, help="输出目录，默认 CSV 同级")
    args = parser.parse_args()

    df = load_and_enrich(args.csv)
    out_dir = args.out_dir or args.csv.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figures"

    tables = build_tables(df)
    make_figures(df, tables, fig_dir)
    write_excel(tables, df, out_dir / "analysis_tables.xlsx")
    write_tables_markdown(tables, out_dir / "analysis_tables.md")

    enriched_cols = [c for c in df.columns if not c.startswith("_")]
    df[enriched_cols].to_csv(out_dir / "cash_trades_enriched.csv", index=False, encoding="utf-8-sig")

    print(f"已生成：{out_dir / 'analysis_tables.xlsx'}")
    print(f"已生成：{out_dir / 'analysis_tables.md'}")
    print(f"已生成：{out_dir / 'cash_trades_enriched.csv'}")
    print(f"已生成图表目录：{fig_dir}")


if __name__ == "__main__":
    main()
