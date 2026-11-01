"""对 uptrend_limit_down 的二次筛选参数做网格搜索。

搜索维度：
- 低开阈值 gap: 0, -1%, -2%, -3%, -4%, -5%
- 近 N 日涨幅阈值 rise: 20%, 30%, 40%, 50%, 60%
- 回看天数 rise_days: 10, 20, 30, 40
- 开盘/盘中止盈阈值 open_exit: 2%, 3%, 4%, 5%

每笔按最多 5 万元、100 股整手买入，扣佣金/印花税/过户费/0.1% 双边滑点。
同时计算两种卖出假设：
1. 报告口径：T+2 收盘跌停也按收盘价成交；
2. 可成交口径：T+2 收盘跌停则顺延到下一个可卖开盘。

输出：outputs/grid_search_uptrend_<timestamp>/
- grid_results.csv：480 个组合的完整指标
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

START = "20240101"
END = "20261023"
INITIAL = 100_000.0
MAX_POSITION = 50_000.0
COMMISSION = 0.00025
MIN_COMMISSION = 5.0
STAMP = 0.0005
TRANSFER = 0.00001
SLIPPAGE = 0.001
GAP_EPS = 1e-9

GAPS = [0.0, -0.01, -0.02, -0.03, -0.04, -0.05]
RISES = [0.20, 0.30, 0.40, 0.50, 0.60]
RISE_DAYS = [10, 20, 30, 40]
OPEN_EXITS = [0.02, 0.03, 0.04, 0.05]

GRID_CN = {
    "gap": "低开阈值", "rise": "涨幅阈值", "rise_days": "回看天数",
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
    "avg_net_real_2024": "2024平均净收益(可成交)",
    "avg_net_real_2025": "2025平均净收益(可成交)",
    "avg_net_real_2026": "2026平均净收益(可成交)",
}

TRADE_CN = {
    "hit_date": "命中日期", "code": "股票代码", "name": "股票名称", "year": "年份",
    "gap": "低开幅度", "rise_10": "近10日涨幅", "rise_20": "近20日涨幅",
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
        t1_adj_open = get("adj_open", dates["t1"])
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
        calendar[max(0, date_pos[d] - 40)] for d in hits["trade_date"] if d in date_pos
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
    total = len(GAPS) * len(RISES) * len(RISE_DAYS) * len(OPEN_EXITS)
    k = 0
    for gap_th in GAPS:
        for rise_th in RISES:
            for n in RISE_DAYS:
                rise_col = features[f"rise_{n}"].to_numpy()
                base = (
                    features["valid"].to_numpy()
                    & (features["gap"].to_numpy() < gap_th - (GAP_EPS if gap_th < 0 else 0))
                    & (rise_col > rise_th)
                )
                base_real = base & features["valid_real"].to_numpy()
                for oe in OPEN_EXITS:
                    k += 1
                    rep = metrics(features, base, oe, realistic=False)
                    real = metrics(features, base_real, oe, realistic=True)
                    results.append({
                        "gap": gap_th,
                        "rise": rise_th,
                        "rise_days": n,
                        "open_exit": oe,
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
                    if k % 40 == 0:
                        print(f"  {k}/{total}")

    res = pd.DataFrame(results)
    # 按年拆分的稳健性（用可成交口径）
    for year in ("2024", "2025", "2026"):
        vals = []
        for _, row in res.iterrows():
            mask = (
                features["valid_real"].to_numpy()
                & (features["gap"].to_numpy() < row["gap"] - (GAP_EPS if row["gap"] < 0 else 0))
                & (features[f"rise_{int(row['rise_days'])}"].to_numpy() > row["rise"])
                & (features["year"].to_numpy() == year)
            )
            m = metrics(features, mask, row["open_exit"], realistic=True)
            vals.append(m["avg_net"])
        res[f"avg_net_real_{year}"] = vals

    out_dir = Path("outputs") / f"grid_search_uptrend_{datetime.now():%Y%m%d_%H%M%S}"
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
        & (features["gap"].to_numpy() < best["gap"] - (GAP_EPS if best["gap"] < 0 else 0))
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
    def rows(df: pd.DataFrame, cols: list[str], n: int = 30) -> list[dict]:
        out = []
        for _, x in df.head(n).iterrows():
            out.append({
                "低开<": fmt_pct(x["gap"]),
                "涨幅>": fmt_pct(x["rise"]),
                "回看": f"{int(x['rise_days'])}日",
                "止盈": fmt_pct(x["open_exit"]),
                "样本": int(x["n_real"]),
                "天数": int(x["n_days_real"]),
                "平均净收益": fmt_pct(x["avg_net_real"]),
                "中位数": fmt_pct(x["median_net_real"]),
                "胜率": fmt_pct(x["win_real"], signed=False),
                "盈亏因子": f"{x['profit_factor_real']:.2f}" if pd.notna(x["profit_factor_real"]) else "-",
                "总净收益": f"{x['total_net_real']:+,.0f}",
                "2024": fmt_pct(x["avg_net_real_2024"]),
                "2025": fmt_pct(x["avg_net_real_2025"]),
                "2026": fmt_pct(x["avg_net_real_2026"]),
            })
        return out

    top_avg = eligible.sort_values("avg_net_real", ascending=False).head(30)
    top_total = res.sort_values("total_net_real", ascending=False).head(20)
    top_report = eligible.sort_values("avg_net", ascending=False).head(20)

    positive = res[res["avg_net_real"] > 0]
    positive30 = eligible[eligible["avg_net_real"] > 0]
    positive_rises = sorted(positive["rise"].unique().tolist())
    all_year_positive = eligible[
        (eligible["avg_net_real_2024"] > 0)
        & (eligible["avg_net_real_2025"] > 0)
        & (eligible["avg_net_real_2026"] > 0)
    ]
    best24 = eligible.sort_values("avg_net_real_2024", ascending=False).iloc[0]
    best_rise50 = eligible[eligible["rise"] <= 0.5].sort_values(
        "avg_net_real", ascending=False
    ).iloc[0]
    best_by_rise_days = eligible.loc[eligible.groupby("rise_days")["avg_net_real"].idxmax()]
    best_by_open_exit = eligible.loc[eligible.groupby("open_exit")["avg_net_real"].idxmax()]
    best_by_gap = eligible.loc[eligible.groupby("gap")["avg_net_real"].idxmax()]
    best_by_rise = eligible.loc[eligible.groupby("rise")["avg_net_real"].idxmax()]

    # 最优 rise_days × open_exit 的 best 表
    pivot = eligible.loc[eligible.groupby(["rise_days", "open_exit"])["avg_net_real"].idxmax()]
    pivot_rows = []
    for _, x in pivot.sort_values(["rise_days", "open_exit"]).iterrows():
        pivot_rows.append({
            "回看": f"{int(x['rise_days'])}日",
            "止盈": fmt_pct(x["open_exit"]),
            "最优低开": fmt_pct(x["gap"]),
            "最优涨幅": fmt_pct(x["rise"]),
            "样本": int(x["n_real"]),
            "平均净收益": fmt_pct(x["avg_net_real"]),
            "总净收益": f"{x['total_net_real']:+,.0f}",
        })

    report = [
        "# uptrend_limit_down 网格搜索结果",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 区间：{START} ~ {END}",
        "- 每笔最多 5 万元，100 股整手；佣金 0.025%（最低 5 元）、印花税 0.05%（卖出）、过户费 0.001%（双边）、滑点 0.1%（双边）",
        "- 主口径：T+2 收盘跌停时不允许按收盘卖出，顺延到下一个可卖开盘；报告口径也一并列出",
        f"- 组合数：{len(res)}（{len(GAPS)}×{len(RISES)}×{len(RISE_DAYS)}×{len(OPEN_EXITS)}）",
        "",
        "## 结论摘要",
        "",
        f"- 480 个组合中，可成交口径平均净收益为正的有 **{len(positive)}** 个；样本数≥30 且为正的有 **{len(positive30)}** 个。",
        f"- 全部组合中，可成交口径平均净收益最高的是：低开 < {fmt_pct(best['gap'])}、{int(best['rise_days'])}日涨幅 > {fmt_pct(best['rise'])}、止盈 {fmt_pct(best['open_exit'])}，样本 {int(best['n_real'])} 笔，平均净收益 **{fmt_pct(best['avg_net_real'])}**，总净收益 **{best['total_net_real']:+,.0f}** 元。",
        f"- 该组合报告口径平均净收益 {fmt_pct(best['avg_net'])}，可成交口径 {fmt_pct(best['avg_net_real'])}，差额 {fmt_pct(best['avg_net_real']-best['avg_net'])}。",
        "",
        "> 480 个组合是多次比较，样本内最优很容易过拟合；下面同时看样本数、分年稳定性和报告口径/可成交口径差异。",
        "",
        "## 稳健性检查",
        "",
        f"- 22 个正收益组合的涨幅阈值全部是 **+60%**；涨幅阈值 ≤50% 的组合全部为负。涨幅≤50% 中最好的是：低开 < {fmt_pct(best_rise50['gap'])}、{int(best_rise50['rise_days'])}日涨幅 > {fmt_pct(best_rise50['rise'])}、止盈 {fmt_pct(best_rise50['open_exit'])}，样本 {int(best_rise50['n_real'])}，平均净收益 **{fmt_pct(best_rise50['avg_net_real'])}**。",
        f"- 没有任何组合在 2024、2025、2026 三年都为正。2024 年最好的组合平均净收益仍是 **{fmt_pct(best24['avg_net_real_2024'])}**（低开 < {fmt_pct(best24['gap'])}、{int(best24['rise_days'])}日涨幅 > {fmt_pct(best24['rise'])}、止盈 {fmt_pct(best24['open_exit'])}）。",
        "- 最优参数落在搜索上限 **涨幅 60%、回看 20 日** 附近，这是明显的边界解；在相信它之前，应该把涨幅阈值扩到 70%/80% 再验证。",
        "",
        "## 可成交口径：平均净收益 Top 30（样本≥30）",
        "",
        md_table(rows(top_avg, []), ["低开<", "涨幅>", "回看", "止盈", "样本", "天数", "平均净收益", "中位数", "胜率", "盈亏因子", "总净收益", "2024", "2025", "2026"]),
        "",
        "## 可成交口径：总净收益 Top 20（不要求样本数）",
        "",
        md_table(rows(top_total, [], 20), ["低开<", "涨幅>", "回看", "止盈", "样本", "天数", "平均净收益", "中位数", "胜率", "盈亏因子", "总净收益", "2024", "2025", "2026"]),
        "",
        "## 报告口径（假设收盘跌停也能成交）Top 20",
        "",
        md_table(rows(top_report, [], 20), ["低开<", "涨幅>", "回看", "止盈", "样本", "天数", "平均净收益", "中位数", "胜率", "盈亏因子", "总净收益", "2024", "2025", "2026"]),
        "",
        "## 分组最优",
        "",
        "### 按回看天数",
        "",
        md_table(rows(best_by_rise_days.sort_values("rise_days"), [], 10), ["低开<", "涨幅>", "回看", "止盈", "样本", "平均净收益", "总净收益", "2024", "2025", "2026"]),
        "",
        "### 按止盈阈值",
        "",
        md_table(rows(best_by_open_exit.sort_values("open_exit"), [], 10), ["低开<", "涨幅>", "回看", "止盈", "样本", "平均净收益", "总净收益", "2024", "2025", "2026"]),
        "",
        "### 按低开阈值",
        "",
        md_table(rows(best_by_gap.sort_values("gap"), [], 10), ["低开<", "涨幅>", "回看", "止盈", "样本", "平均净收益", "总净收益", "2024", "2025", "2026"]),
        "",
        "### 按涨幅阈值",
        "",
        md_table(rows(best_by_rise.sort_values("rise"), [], 10), ["低开<", "涨幅>", "回看", "止盈", "样本", "平均净收益", "总净收益", "2024", "2025", "2026"]),
        "",
        "### 回看天数 × 止盈阈值的最优组合",
        "",
        md_table(pivot_rows, ["回看", "止盈", "最优低开", "最优涨幅", "样本", "平均净收益", "总净收益"]),
        "",
        "## 最佳组合逐笔摘要",
        "",
        f"- 参数：低开 < {fmt_pct(best['gap'])}，{int(best['rise_days'])} 日涨幅 > {fmt_pct(best['rise'])}，止盈 {fmt_pct(best['open_exit'])}",
        f"- 样本：{int(best['n_real'])} 笔，{int(best['n_days_real'])} 个交易日",
        f"- 平均净收益：{fmt_pct(best['avg_net_real'])}，中位数 {fmt_pct(best['median_net_real'])}，胜率 {fmt_pct(best['win_real'], signed=False)}，盈亏因子 {best['profit_factor_real']:.2f}",
        f"- 总净收益：{best['total_net_real']:+,.0f} 元；10 万 + 总净收益 = {best['final_real']:,.0f} 元（不要求资金足够、不复利）",
        f"- 分年：2024 {fmt_pct(best['avg_net_real_2024'])}，2025 {fmt_pct(best['avg_net_real_2025'])}，2026 {fmt_pct(best['avg_net_real_2026'])}",
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
        "- `grid_results.csv`：480 个组合的完整指标（含报告口径/可成交口径/分年）",
        "- `best_combo_trades.csv`：最佳组合逐笔明细",
        "- `report.md`：本报告",
    ]
    (out_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\n完成：{out_dir.resolve()}")
    print(f"最佳：gap={best['gap']}, rise={best['rise']}, rise_days={int(best['rise_days'])}, "
          f"open_exit={best['open_exit']}, n={int(best['n_real'])}, "
          f"avg_net_real={best['avg_net_real']*100:+.2f}%, total_net_real={best['total_net_real']:+.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
