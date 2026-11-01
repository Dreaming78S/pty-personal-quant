"""对 uptrend_limit_down 的二次筛选参数做网格搜索。

搜索维度（共 1008 个组合）：
- T 日低开区间 gap: (-11%,0%)、(-11%,-7%)、(-7%,-5%)、(-5%,-2%)、(-2%,0%)、(0%,11%)
- 近 N 日涨幅阈值 rise: 30%, 40%, 50%, 60%, 70%, 80%
- 回看天数 rise_days: 10, 20, 30, 40
- 开盘/盘中止盈阈值 open_exit: 固定 3%
- T+1 买入低开区间 entry_gap: 7 个闭区间

每笔按最多 5 万元、100 股整手买入，扣佣金/印花税/过户费/0.1% 双边滑点。
同时计算两种卖出假设：
1. 报告口径：T+2 收盘跌停也按收盘价成交；
2. 可成交口径：T+2 收盘跌停则顺延到下一个可卖开盘。

输出：outputs/grid_search_uptrend_ranges_<timestamp>/
- grid_results.csv：1008 个组合的完整指标
- best_combo_trades.csv：最佳组合的逐笔明细
- report.md：最终报告
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import db

START = "20180101"
END = "20261028"
INITIAL = 100_000.0
MAX_POSITION = 50_000.0
COMMISSION = 0.00025
MIN_COMMISSION = 5.0
STAMP = 0.0005
TRANSFER = 0.00001
SLIPPAGE = 0.001
GAP_EPS = 1e-9

GAP_RANGES = [
    (-0.11, 0.00),
    (-0.11, -0.07),
    (-0.07, -0.05),
    (-0.05, -0.02),
    (-0.02, 0.00),
    (0.00, 0.11),
]
RISES = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
RISE_DAYS = [10, 20, 30, 40]
OPEN_EXITS = [0.03]
ENTRY_GAP_RANGES = [
    (-0.11, 0.00),
    (-0.11, -0.02),
    (-0.11, -0.07),
    (-0.07, -0.05),
    (-0.05, -0.02),
    (-0.02, 0.00),
    (0.00, 0.11),
]

GRID_CN = {
    "gap_lo": "低开区间下限", "gap_hi": "低开区间上限",
    "entry_lo": "T+1低开区间下限", "entry_hi": "T+1低开区间上限",
    "rise": "涨幅阈值", "rise_days": "回看天数",
    "open_exit": "止盈阈值",
    "n": "样本数(报告)", "n_days": "交易日数(报告)",
    "avg_net": "平均净收益(报告)", "median_net": "中位数(报告)",
    "win": "胜率(报告)", "profit_factor": "盈亏因子(报告)",
    "total_net": "总净收益(报告)", "final": "期末资产(报告)",
    "avg_gross": "平均毛收益(报告)", "avg_day_net": "日度平均净收益(报告)",
    "n_real": "样本数(可成交)", "n_days_real": "交易日数(可成交)",
    "avg_net_real": "平均净收益(可成交)", "median_net_real": "中位数(可成交)",
    "win_real": "胜率(可成交)", "profit_factor_real": "盈亏因子(可成交)",
    "total_net_real": "总净收益(可成交)", "final_real": "期末资产(可成交)",
    "avg_gross_real": "平均毛收益(可成交)", "avg_day_net_real": "日度平均净收益(可成交)",
    "avg_net_real_2018": "2018平均净收益(可成交)",
    "avg_net_real_2019": "2019平均净收益(可成交)",
    "avg_net_real_2020": "2020平均净收益(可成交)",
    "avg_net_real_2021": "2021平均净收益(可成交)",
    "avg_net_real_2022": "2022平均净收益(可成交)",
    "avg_net_real_2023": "2023平均净收益(可成交)",
    "avg_net_real_2024": "2024平均净收益(可成交)",
    "avg_net_real_2025": "2025平均净收益(可成交)",
    "avg_net_real_2026": "2026平均净收益(可成交)",
    "avg_net_real_2018_2023": "2018-2023平均净收益(可成交)",
    "positive_years": "正收益年数",
    "min_year_avg": "最差年份平均净收益",
}

TRADE_CN = {
    "hit_date": "命中日期", "code": "股票代码", "name": "股票名称", "year": "年份",
    "gap": "低开幅度", "entry_gap": "T+1开盘低开幅度",
    "rise_10": "近10日涨幅", "rise_20": "近20日涨幅",
    "rise_30": "近30日涨幅", "rise_40": "近40日涨幅",
    "entry_raw": "次日开盘价(不复权)", "shares": "股数", "invest": "买入金额",
    "buy_fee": "买入费用", "t2_open_ret": "次次日开盘收益",
    "t2_high_ret": "次次日最高收益", "t2_close_ret": "次次日收盘收益",
    "close_limit": "收盘跌停", "next_open_ret": "顺延开盘收益",
    "next_open_date": "顺延卖出日", "valid": "有效", "valid_real": "可成交有效",
    "gross_ret": "毛收益", "net_pnl": "净收益", "net_ret": "净收益率",
    "exit_mode": "卖出方式", "real_exit": "实际卖出方式",
}


def buy_fee(amount: float) -> float:
    return max(amount * COMMISSION, MIN_COMMISSION) + amount * TRANSFER


def sell_fee(amount: float) -> float:
    return (max(amount * COMMISSION, MIN_COMMISSION)
            + amount * STAMP + amount * TRANSFER)


def sell_fee_vec(amount: np.ndarray) -> np.ndarray:
    return (np.maximum(amount * COMMISSION, MIN_COMMISSION)
            + amount * STAMP + amount * TRANSFER)


def load_calendar() -> list[str]:
    cal = db.read_df(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 ORDER BY cal_date"
    )
    return cal["cal_date"].astype(str).tolist()


def load_hits() -> pd.DataFrame:
    hits = db.read_df(
        "SELECT trade_date, ts_code, name FROM hit_uptrend_limit_down "
        "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date, ts_code",
        (START, END),
    )
    hits["trade_date"] = hits["trade_date"].astype(str)
    hits["ts_code"] = hits["ts_code"].astype(str)
    hits["name"] = hits["name"].fillna("").astype(str)
    return hits


def load_prices(codes: list[str], min_date: str, max_date: str) -> pd.DataFrame:
    frames = []
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        ph = ",".join(["%s"] * len(chunk))
        frames.append(db.read_df(
            f"""
            SELECT d.ts_code, d.trade_date, d.open, d.high, d.close, d.pre_close,
                   a.adj_factor
            FROM daily d
            LEFT JOIN adj_factor a
              ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date
            WHERE d.ts_code IN ({ph})
              AND d.trade_date BETWEEN %s AND %s
            """,
            (*chunk, min_date, max_date),
        ))
    df = pd.concat(frames, ignore_index=True)
    for c in ("open", "high", "close", "pre_close", "adj_factor"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["adj_open"] = df["open"] * df["adj_factor"]
    df["adj_high"] = df["high"] * df["adj_factor"]
    df["adj_close"] = df["close"] * df["adj_factor"]
    df["trade_date"] = df["trade_date"].astype(str)
    df["ts_code"] = df["ts_code"].astype(str)
    return df


def load_limits(codes: list[str], min_date: str, max_date: str) -> pd.DataFrame:
    frames = []
    for i in range(0, len(codes), 500):
        chunk = codes[i:i + 500]
        ph = ",".join(["%s"] * len(chunk))
        frames.append(db.read_df(
            f"""
            SELECT ts_code, trade_date, down_limit
            FROM stk_limit
            WHERE ts_code IN ({ph})
              AND trade_date BETWEEN %s AND %s
            """,
            (*chunk, min_date, max_date),
        ))
    df = pd.concat(frames, ignore_index=True)
    df["down_limit"] = pd.to_numeric(df["down_limit"], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    df["ts_code"] = df["ts_code"].astype(str)
    return df


def build_features(hits: pd.DataFrame, calendar: list[str],
                   px: pd.DataFrame, lim: pd.DataFrame) -> pd.DataFrame:
    px_idx = px.set_index(["ts_code", "trade_date"])
    lim_idx = lim.set_index(["ts_code", "trade_date"])["down_limit"]
    date_pos = {d: i for i, d in enumerate(calendar)}
    px_codes = set(px_idx.index.get_level_values(0))

    rows = []
    for _, h in hits.iterrows():
        code = h["ts_code"]
        t = h["trade_date"]
        if t not in date_pos or code not in px_codes:
            continue
        i = date_pos[t]
        dates = {
            "t": t,
            "t1": calendar[i + 1] if i + 1 < len(calendar) else None,
            "t2": calendar[i + 2] if i + 2 < len(calendar) else None,
        }
        for n in RISE_DAYS:
            dates[f"t{n}"] = calendar[i - n] if i - n >= 0 else None

        def get(col: str, d: str | None):
            if d is None:
                return np.nan
            try:
                return float(px_idx.loc[(code, d), col])
            except KeyError:
                return np.nan

        t_open = get("open", dates["t"])
        t_pre = get("pre_close", dates["t"])
        t_adj_close = get("adj_close", dates["t"])
        t1_open_raw = get("open", dates["t1"])
        t1_pre_close_raw = get("pre_close", dates["t1"])
        t1_adj_open = get("adj_open", dates["t1"])
        entry_gap = (
            t1_open_raw / t1_pre_close_raw - 1
            if pd.notna(t1_open_raw) and pd.notna(t1_pre_close_raw) and t1_pre_close_raw > 0
            else np.nan
        )
        t2_open_adj = get("adj_open", dates["t2"])
        t2_high_adj = get("adj_high", dates["t2"])
        t2_close_adj = get("adj_close", dates["t2"])
        t2_close_raw = get("close", dates["t2"])

        gap = t_open / t_pre - 1 if t_pre and t_pre > 0 else np.nan
        rises = {}
        for n in RISE_DAYS:
            ref = get("adj_close", dates[f"t{n}"])
            rises[n] = t_adj_close / ref - 1 if ref and ref > 0 else np.nan

        t2_open_ret = t2_open_adj / t1_adj_open - 1 if t1_adj_open and t1_adj_open > 0 else np.nan
        t2_high_ret = t2_high_adj / t1_adj_open - 1 if t1_adj_open and t1_adj_open > 0 else np.nan
        t2_close_ret = t2_close_adj / t1_adj_open - 1 if t1_adj_open and t1_adj_open > 0 else np.nan

        try:
            down_limit = float(lim_idx.loc[(code, dates["t2"])])
        except KeyError:
            down_limit = np.nan
        close_limit = bool(
            pd.notna(t2_close_raw) and pd.notna(down_limit)
            and t2_close_raw <= down_limit + 1e-9
        )

        next_open_ret = np.nan
        next_open_date = None
        if close_limit and dates["t2"] and dates["t2"] in date_pos:
            j = date_pos[dates["t2"]]
            for k in range(1, 6):
                if j + k >= len(calendar):
                    break
                d = calendar[j + k]
                try:
                    o = float(px_idx.loc[(code, d), "open"])
                    a = float(px_idx.loc[(code, d), "adj_factor"])
                    dl = float(lim_idx.loc[(code, d)])
                except KeyError:
                    continue
                if pd.notna(o) and pd.notna(a) and pd.notna(dl) and o > dl + 1e-9:
                    next_open_ret = (o * a) / t1_adj_open - 1
                    next_open_date = d
                    break

        price = t1_open_raw * (1 + SLIPPAGE) if pd.notna(t1_open_raw) else np.nan
        shares = int(MAX_POSITION // (price * 100)) * 100 if pd.notna(price) and price > 0 else 0
        invest = shares * price if shares > 0 else 0.0
        bfee = buy_fee(invest) if invest > 0 else 0.0

        rows.append({
            "hit_date": t,
            "code": code,
            "name": h["name"],
            "year": t[:4],
            "gap": gap,
            "entry_gap": entry_gap,
            **{f"rise_{n}": rises[n] for n in RISE_DAYS},
            "entry_raw": t1_open_raw,
            "shares": shares,
            "invest": invest,
            "buy_fee": bfee,
            "t2_open_ret": t2_open_ret,
            "t2_high_ret": t2_high_ret,
            "t2_close_ret": t2_close_ret,
            "close_limit": close_limit,
            "next_open_ret": next_open_ret,
            "next_open_date": next_open_date,
        })
    out = pd.DataFrame(rows)
    # 有效样本：买入、T+1/T+2 行情、整手都满足
    out["valid"] = (
        out["shares"].gt(0)
        & out["gap"].notna()
        & out["entry_gap"].notna()
        & out["t2_open_ret"].notna()
        & out["t2_high_ret"].notna()
        & out["t2_close_ret"].notna()
    )
    out["valid_real"] = out["valid"] & (
        ~out["close_limit"] | out["next_open_ret"].notna()
    )
    return out


def metrics(features: pd.DataFrame, mask: np.ndarray, open_exit: float,
            realistic: bool) -> dict:
    sub = features.loc[mask].copy()
    if sub.empty:
        return {"n": 0, "n_days": 0, "avg_net": np.nan, "median_net": np.nan,
                "win": np.nan, "profit_factor": np.nan, "total_net": 0.0,
                "final": INITIAL, "avg_gross": np.nan, "avg_day_net": np.nan}
    o = sub["t2_open_ret"].to_numpy()
    h = sub["t2_high_ret"].to_numpy()
    c = sub["t2_close_ret"].to_numpy()
    if realistic:
        c = np.where(sub["close_limit"].to_numpy(), sub["next_open_ret"].to_numpy(), c)
    exit_open = o > open_exit
    exit_intra = (~exit_open) & (h >= open_exit)
    r = np.where(exit_open, o, np.where(exit_intra, open_exit, c))
    ok = np.isfinite(r)
    sub = sub.loc[ok]
    r = r[ok]
    if sub.empty:
        return {"n": 0, "n_days": 0, "avg_net": np.nan, "median_net": np.nan,
                "win": np.nan, "profit_factor": np.nan, "total_net": 0.0,
                "final": INITIAL, "avg_gross": np.nan, "avg_day_net": np.nan}
    gross_exit = sub["shares"].to_numpy() * sub["entry_raw"].to_numpy() * (1 + r) * (1 - SLIPPAGE)
    sfee = sell_fee_vec(gross_exit)
    net = gross_exit - sfee - sub["invest"].to_numpy() - sub["buy_fee"].to_numpy()
    net_ret = net / MAX_POSITION
    pos_sum = net[net > 0].sum()
    neg_sum = net[net < 0].sum()
    pf = pos_sum / abs(neg_sum) if neg_sum < 0 else np.nan
    day = pd.Series(net_ret, index=sub["hit_date"].to_numpy()).groupby(level=0).mean()
    return {
        "n": len(sub),
        "n_days": int(sub["hit_date"].nunique()),
        "avg_net": float(net_ret.mean()),
        "median_net": float(np.median(net_ret)),
        "win": float((net > 0).mean()),
        "profit_factor": float(pf) if pd.notna(pf) else np.nan,
        "total_net": float(net.sum()),
        "final": float(INITIAL + net.sum()),
        "avg_gross": float(r.mean()),
        "avg_day_net": float(day.mean()) if len(day) else np.nan,
    }


def fmt_pct(x: float, signed: bool = True) -> str:
    if x is None or pd.isna(x):
        return "-"
    return f"{x*100:+.2f}%" if signed else f"{x*100:.2f}%"


def fmt_range(lo: float, hi: float) -> str:
    return f"[{fmt_pct(lo, signed=False)}, {fmt_pct(hi, signed=False)}]"


def md_table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "_无_"
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(lines)


def main() -> int:
    print("加载命中、交易日历、行情...")
    hits = load_hits()
    calendar = load_calendar()
    date_pos = {d: i for i, d in enumerate(calendar)}
    codes = sorted(hits["ts_code"].unique())
    earliest = min(
        calendar[max(0, date_pos[d] - 60)] for d in hits["trade_date"] if d in date_pos
    )
    latest = max(
        calendar[min(len(calendar) - 1, date_pos[d] + 10)]
        for d in hits["trade_date"] if d in date_pos
    )
    px = load_prices(codes, earliest, latest)
    lim = load_limits(codes, earliest, latest)
    print(f"命中={len(hits)}, 行情={len(px):,}, 涨跌停={len(lim):,}")
    features = build_features(hits, calendar, px, lim)
    print(f"可计算特征样本={len(features)}, valid={int(features['valid'].sum())}, "
          f"valid_real={int(features['valid_real'].sum())}")

    results = []
    total = (len(GAP_RANGES) * len(RISES) * len(RISE_DAYS)
             * len(OPEN_EXITS) * len(ENTRY_GAP_RANGES))
    k = 0
    gap_values = features["gap"].to_numpy()
    entry_values = features["entry_gap"].to_numpy()
    valid = features["valid"].to_numpy()
    valid_real = features["valid_real"].to_numpy()
    for gap_lo, gap_hi in GAP_RANGES:
        gap_mask = (
            (gap_values >= gap_lo - GAP_EPS)
            & (gap_values <= gap_hi + GAP_EPS)
        )
        for rise_th in RISES:
            for n in RISE_DAYS:
                rise_mask = features[f"rise_{n}"].to_numpy() > rise_th
                base_common = valid & gap_mask & rise_mask
                for entry_lo, entry_hi in ENTRY_GAP_RANGES:
                    entry_mask = (
                        (entry_values >= entry_lo - GAP_EPS)
                        & (entry_values <= entry_hi + GAP_EPS)
                    )
                    base = base_common & entry_mask
                    base_real = base & valid_real
                    for oe in OPEN_EXITS:
                        k += 1
                        rep = metrics(features, base, oe, realistic=False)
                        real = metrics(features, base_real, oe, realistic=True)
                        results.append({
                            "gap_lo": gap_lo, "gap_hi": gap_hi,
                            "rise": rise_th,
                            "rise_days": n,
                            "open_exit": oe,
                            "entry_lo": entry_lo, "entry_hi": entry_hi,
                            "n": rep["n"],
                            "n_days": rep["n_days"],
                            "avg_net": rep["avg_net"],
                            "median_net": rep["median_net"],
                            "win": rep["win"],
                            "profit_factor": rep["profit_factor"],
                            "total_net": rep["total_net"],
                            "final": rep["final"],
                            "avg_gross": rep["avg_gross"],
                            "avg_day_net": rep["avg_day_net"],
                            "n_real": real["n"],
                            "n_days_real": real["n_days"],
                            "avg_net_real": real["avg_net"],
                            "median_net_real": real["median_net"],
                            "win_real": real["win"],
                            "profit_factor_real": real["profit_factor"],
                            "total_net_real": real["total_net"],
                            "final_real": real["final"],
                            "avg_gross_real": real["avg_gross"],
                            "avg_day_net_real": real["avg_day_net"],
                        })
                        if k % 100 == 0:
                            print(f"  {k}/{total}")

    res = pd.DataFrame(results)
    # 按年拆分的稳健性（用可成交口径）
    years = sorted(features["year"].unique())
    for year in years:
        vals = []
        for _, row in res.iterrows():
            mask = (
                (features["valid_real"].to_numpy())
                & (features["gap"].to_numpy() >= row["gap_lo"] - GAP_EPS)
                & (features["gap"].to_numpy() <= row["gap_hi"] + GAP_EPS)
                & (features["entry_gap"].to_numpy() >= row["entry_lo"] - GAP_EPS)
                & (features["entry_gap"].to_numpy() <= row["entry_hi"] + GAP_EPS)
                & (features[f"rise_{int(row['rise_days'])}"].to_numpy() > row["rise"])
                & (features["year"].to_numpy() == year)
            )
            m = metrics(features, mask, row["open_exit"], realistic=True)
            vals.append(m["avg_net"])
        res[f"avg_net_real_{year}"] = vals
    # 2018-2023 合并
    hist_mask_base = features["year"].isin(list(years[:6]))
    hist_vals = []
    for _, row in res.iterrows():
        mask = (
            hist_mask_base.to_numpy()
            & (features["valid_real"].to_numpy())
            & (features["gap"].to_numpy() >= row["gap_lo"] - GAP_EPS)
            & (features["gap"].to_numpy() <= row["gap_hi"] + GAP_EPS)
            & (features["entry_gap"].to_numpy() >= row["entry_lo"] - GAP_EPS)
            & (features["entry_gap"].to_numpy() <= row["entry_hi"] + GAP_EPS)
            & (features[f"rise_{int(row['rise_days'])}"].to_numpy() > row["rise"])
        )
        m = metrics(features, mask, row["open_exit"], realistic=True)
        hist_vals.append(m["avg_net"])
    res["avg_net_real_2018_2023"] = hist_vals
    year_cols = [f"avg_net_real_{y}" for y in years]
    res["positive_years"] = (res[year_cols] > 0).sum(axis=1)
    res["min_year_avg"] = res[year_cols].min(axis=1)

    out_dir = Path("outputs") / f"grid_search_uptrend_ranges_{datetime.now():%Y%m%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    res.rename(columns=GRID_CN).to_csv(
        out_dir / "grid_results.csv", index=False, encoding="utf-8-sig"
    )

    # 最佳组合
    eligible = res[res["n_real"] >= 30].copy()
    if eligible.empty:
        eligible = res[res["n_real"] >= 10].copy()
    best = eligible.sort_values("avg_net_real", ascending=False).iloc[0]
    best_mask = (
        features["valid_real"].to_numpy()
        & (features["gap"].to_numpy() >= best["gap_lo"] - GAP_EPS)
        & (features["gap"].to_numpy() <= best["gap_hi"] + GAP_EPS)
        & (features["entry_gap"].to_numpy() >= best["entry_lo"] - GAP_EPS)
        & (features["entry_gap"].to_numpy() <= best["entry_hi"] + GAP_EPS)
        & (features[f"rise_{int(best['rise_days'])}"].to_numpy() > best["rise"])
    )
    best_trades = features.loc[best_mask].copy()
    o = best_trades["t2_open_ret"].to_numpy()
    h = best_trades["t2_high_ret"].to_numpy()
    c = np.where(best_trades["close_limit"].to_numpy(),
                 best_trades["next_open_ret"].to_numpy(),
                 best_trades["t2_close_ret"].to_numpy())
    exit_open = o > best["open_exit"]
    exit_intra = (~exit_open) & (h >= best["open_exit"])
    r = np.where(exit_open, o, np.where(exit_intra, best["open_exit"], c))
    gross_exit = best_trades["shares"].to_numpy() * best_trades["entry_raw"].to_numpy() * (1 + r) * (1 - SLIPPAGE)
    sfee = sell_fee_vec(gross_exit)
    best_trades["gross_ret"] = r
    best_trades["net_pnl"] = gross_exit - sfee - best_trades["invest"].to_numpy() - best_trades["buy_fee"].to_numpy()
    best_trades["net_ret"] = best_trades["net_pnl"] / MAX_POSITION
    best_trades["exit_mode"] = np.where(exit_open, "T+2 开盘",
                                        np.where(exit_intra, "T+2 盘中", "T+2 收盘"))
    best_trades["real_exit"] = np.where(
        (~exit_open) & (~exit_intra) & best_trades["close_limit"],
        "顺延下一开盘", best_trades["exit_mode"],
    )
    best_trades.rename(columns=TRADE_CN).to_csv(
        out_dir / "best_combo_trades.csv", index=False, encoding="utf-8-sig"
    )

    # 汇总表
    def rows(df: pd.DataFrame, n: int = 30) -> list[dict]:
        out = []
        for _, x in df.head(n).iterrows():
            out.append({
                "T日低开": fmt_range(x["gap_lo"], x["gap_hi"]),
                "T+1低开": fmt_range(x["entry_lo"], x["entry_hi"]),
                "涨幅>": fmt_pct(x["rise"]),
                "回看": f"{int(x['rise_days'])}日",
                "样本": int(x["n_real"]),
                "天数": int(x["n_days_real"]),
                "平均净收益": fmt_pct(x["avg_net_real"]),
                "中位数": fmt_pct(x["median_net_real"]),
                "胜率": fmt_pct(x["win_real"], signed=False),
                "盈亏因子": f"{x['profit_factor_real']:.2f}" if pd.notna(x["profit_factor_real"]) else "-",
                "总净收益": f"{x['total_net_real']:+,.0f}",
                "2018-23": fmt_pct(x["avg_net_real_2018_2023"]),
                "2024": fmt_pct(x["avg_net_real_2024"]),
                "2025": fmt_pct(x["avg_net_real_2025"]),
                "2026": fmt_pct(x["avg_net_real_2026"]),
                "正年数": int(x["positive_years"]),
                "最差年": fmt_pct(x["min_year_avg"]),
            })
        return out

    top_avg = eligible.sort_values("avg_net_real", ascending=False).head(30)
    top_total = res.sort_values("total_net_real", ascending=False).head(20)
    top_report = eligible.sort_values("avg_net", ascending=False).head(20)

    positive = res[res["avg_net_real"] > 0]
    positive30 = eligible[eligible["avg_net_real"] > 0]
    all_year_positive = eligible[eligible["positive_years"] == len(years)]
    best_robust = eligible.sort_values(
        ["positive_years", "min_year_avg", "avg_net_real"], ascending=False
    ).iloc[0]
    best_hist = eligible.sort_values("avg_net_real_2018_2023", ascending=False).iloc[0]

    best_by_rise_days = eligible.loc[eligible.groupby("rise_days")["avg_net_real"].idxmax()]
    best_by_gap = eligible.loc[
        eligible.groupby(["gap_lo", "gap_hi"])["avg_net_real"].idxmax()
    ]
    best_by_entry = eligible.loc[
        eligible.groupby(["entry_lo", "entry_hi"])["avg_net_real"].idxmax()
    ]
    best_by_rise = eligible.loc[eligible.groupby("rise")["avg_net_real"].idxmax()]

    report = [
        "# uptrend_limit_down 区间网格搜索结果",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 区间：{START} ~ {END}",
        "- 每笔最多 5 万元，100 股整手；佣金 0.025%（最低 5 元）、印花税 0.05%（卖出）、过户费 0.001%（双边）、滑点 0.1%（双边）",
        "- 主口径：T+2 收盘跌停时不允许按收盘卖出，顺延到下一个可卖开盘；报告口径也一并列出",
        f"- 组合数：{len(res)}（{len(GAP_RANGES)}×{len(RISES)}×{len(RISE_DAYS)}×{len(OPEN_EXITS)}×{len(ENTRY_GAP_RANGES)}）",
        "- 止盈阈值固定为 3.00%；T日低开区间和 T+1 低开区间均为闭区间，且不同区间之间有重叠。",
        "",
        "## 结论摘要",
        "",
        f"- 可成交口径平均净收益为正的组合有 **{len(positive)}** 个；样本数≥30 且为正的有 **{len(positive30)}** 个。",
        f"- 平均净收益最高：T日低开 {fmt_range(best['gap_lo'], best['gap_hi'])}、T+1低开 {fmt_range(best['entry_lo'], best['entry_hi'])}、{int(best['rise_days'])}日涨幅 > {fmt_pct(best['rise'])}、止盈 {fmt_pct(best['open_exit'])}；样本 {int(best['n_real'])} 笔，平均净收益 **{fmt_pct(best['avg_net_real'])}**，总净收益 **{best['total_net_real']:+,.0f}** 元。",
        f"- 该组合报告口径平均净收益 {fmt_pct(best['avg_net'])}，可成交口径 {fmt_pct(best['avg_net_real'])}，差额 {fmt_pct(best['avg_net_real']-best['avg_net'])}。",
        "",
        "> 1008 个组合是多次比较，样本内最优很容易过拟合；区间之间还有重叠，下面同时看样本数、分年稳定性和报告口径/可成交口径差异。",
        "",
        "## 稳健性检查",
        "",
        f"- 2018-2026 全部年份平均净收益都为正的组合：**{len(all_year_positive)}** 个。",
        f"- 正收益年数最多、且最差年份最好的组合：T日低开 {fmt_range(best_robust['gap_lo'], best_robust['gap_hi'])}、T+1低开 {fmt_range(best_robust['entry_lo'], best_robust['entry_hi'])}、{int(best_robust['rise_days'])}日涨幅 > {fmt_pct(best_robust['rise'])}；样本 {int(best_robust['n_real'])}，平均 {fmt_pct(best_robust['avg_net_real'])}，正收益年数 {int(best_robust['positive_years'])}/{len(years)}，最差年 {fmt_pct(best_robust['min_year_avg'])}。",
        f"- 2018-2023 表现最好的组合：T日低开 {fmt_range(best_hist['gap_lo'], best_hist['gap_hi'])}、T+1低开 {fmt_range(best_hist['entry_lo'], best_hist['entry_hi'])}、{int(best_hist['rise_days'])}日涨幅 > {fmt_pct(best_hist['rise'])}；样本 {int(best_hist['n_real'])}，2018-2023 平均 {fmt_pct(best_hist['avg_net_real_2018_2023'])}，全期平均 {fmt_pct(best_hist['avg_net_real'])}。",
        "- 区间有重叠：例如 `(-11%, 0%)` 包含多个更窄区间。排名靠前的组合可能是同一批样本的不同区间表述，不应当成独立验证。",
        "",
        "## 可成交口径：平均净收益 Top 30（样本≥30）",
        "",
        md_table(rows(top_avg, 30), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "天数", "平均净收益", "中位数", "胜率", "盈亏因子", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "## 可成交口径：总净收益 Top 20（不要求样本数）",
        "",
        md_table(rows(top_total, 20), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "天数", "平均净收益", "中位数", "胜率", "盈亏因子", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "## 报告口径（假设收盘跌停也能成交）Top 20",
        "",
        md_table(rows(top_report, 20), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "天数", "平均净收益", "中位数", "胜率", "盈亏因子", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "## 分组最优",
        "",
        "### 按回看天数",
        "",
        md_table(rows(best_by_rise_days.sort_values("rise_days"), 10), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "平均净收益", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "### 按涨幅阈值",
        "",
        md_table(rows(best_by_rise.sort_values("rise"), 10), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "平均净收益", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "### 按 T 日低开区间",
        "",
        md_table(rows(best_by_gap.sort_values(["gap_lo", "gap_hi"]), 10), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "平均净收益", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "### 按 T+1 低开区间",
        "",
        md_table(rows(best_by_entry.sort_values(["entry_lo", "entry_hi"]), 10), ["T日低开", "T+1低开", "涨幅>", "回看", "样本", "平均净收益", "总净收益", "2018-23", "2024", "2025", "2026", "正年数", "最差年"]),
        "",
        "## 最佳组合逐笔摘要",
        "",
        f"- 参数：T日低开 {fmt_range(best['gap_lo'], best['gap_hi'])}，T+1低开 {fmt_range(best['entry_lo'], best['entry_hi'])}，{int(best['rise_days'])} 日涨幅 > {fmt_pct(best['rise'])}，止盈 {fmt_pct(best['open_exit'])}",
        f"- 样本：{int(best['n_real'])} 笔，{int(best['n_days_real'])} 个交易日",
        f"- 平均净收益：{fmt_pct(best['avg_net_real'])}，中位数 {fmt_pct(best['median_net_real'])}，胜率 {fmt_pct(best['win_real'], signed=False)}，盈亏因子 {best['profit_factor_real']:.2f}",
        f"- 总净收益：{best['total_net_real']:+,.0f} 元；10 万 + 总净收益 = {best['final_real']:,.0f} 元（不要求资金足够、不复利）",
        f"- 分年：2018-2023 {fmt_pct(best['avg_net_real_2018_2023'])}，2024 {fmt_pct(best['avg_net_real_2024'])}，2025 {fmt_pct(best['avg_net_real_2025'])}，2026 {fmt_pct(best['avg_net_real_2026'])}；正收益年数 {int(best['positive_years'])}/{len(years)}，最差年 {fmt_pct(best['min_year_avg'])}",
        "",
        "## 口径与限制",
        "",
        "- 每笔按 5 万元上限、100 股整手；买不满 100 股或行情缺失的样本不计入。",
        "- 总净收益是 50 笔/多笔的加总，不是复利净值；不同组合的并发资金需求不同，不能直接当作 10 万账户收益。",
        "- 可成交口径只处理了 T+2 收盘跌停：顺延到下一个开盘不再跌停的交易日；若连续跌停，最多尝试 5 个交易日。",
        "- 未剔除 T+1 开盘涨停买不进、盘中触及止盈但实际无法成交、滑点扩大等情况。",
        "- 报告口径是当前脚本/历史报告的假设，偏乐观；请以可成交口径为主。",
        "",
        "## 输出文件",
        "",
        f"- `grid_results.csv`：{len(res)} 个组合的完整指标（含报告口径/可成交口径/分年）",
        "- `best_combo_trades.csv`：最佳组合逐笔明细",
        "- `report.md`：本报告",
    ]
    (out_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\n完成：{out_dir.resolve()}")
    print(
        f"最佳：T日低开={fmt_range(best['gap_lo'], best['gap_hi'])}, "
        f"T+1低开={fmt_range(best['entry_lo'], best['entry_hi'])}, "
        f"rise={best['rise']}, rise_days={int(best['rise_days'])}, "
        f"n={int(best['n_real'])}, avg_net_real={best['avg_net_real']*100:+.2f}%, "
        f"total_net_real={best['total_net_real']:+.0f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
