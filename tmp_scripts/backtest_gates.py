from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quant.data import db, schemas
from quant.engine import recommend as rec

START = "20240102"
END = "20260921"
COST = 0.003

INDEX_CODES = {
    "000300.SH": "沪深300",
    "000001.SH": "上证",
    "399001.SZ": "深证",
    "399006.SZ": "创业板",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
}


def rule_hs300(s: pd.Series) -> bool:
    return bool(s.get("000300.SH", False))


def rule_all(s: pd.Series) -> bool:
    return bool(s.all())


def rule_majority(s: pd.Series) -> bool:
    return int(s.sum()) >= 4


def rule_hs300_and_other(s: pd.Series) -> bool:
    hs = bool(s.get("000300.SH", False))
    others = s.drop("000300.SH", errors="ignore")
    return hs and bool(others.any())


def rule_szcy_kc_majority(s: pd.Series) -> bool:
    subset = s.reindex(["000001.SH", "399001.SZ", "000688.SH"])
    return int(subset.sum()) >= 2


def rule_sz_kc_zz_majority(s: pd.Series) -> bool:
    subset = s.reindex(["000001.SH", "000688.SH", "000905.SH", "000852.SH"])
    return int(subset.sum()) >= 3


RULES: dict[str, Callable[[pd.Series], bool]] = {
    "A 沪深300": rule_hs300,
    "B 七指数全站上": rule_all,
    "C 七指数多数(>=4)": rule_majority,
    "D 沪深300+任一其他": rule_hs300_and_other,
    "E 上证/深证/科创多数": rule_szcy_kc_majority,
    "F 上证/科创/中证500/中证1000多数": rule_sz_kc_zz_majority,
}


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x*100:+.2f}%"


def main() -> None:
    print("查询交易日历...")
    dates_df = db.read_df(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 "
        "AND cal_date BETWEEN %s AND %s ORDER BY cal_date",
        (START, END),
    )
    dates = dates_df["cal_date"].astype(str).tolist()
    print(f"区间共 {len(dates)} 个交易日")

    print("查询指数日线并计算MA20...")
    idx_df = db.read_df(
        "SELECT ts_code, trade_date, close FROM index_daily "
        "WHERE ts_code IN %s AND trade_date BETWEEN %s AND %s",
        (tuple(INDEX_CODES), START, END),
    )
    idx_df = idx_df.sort_values(["ts_code", "trade_date"])
    idx_df["ma20"] = idx_df.groupby("ts_code")["close"].transform(
        lambda x: x.rolling(20, min_periods=20).mean()
    )
    idx_df["above"] = idx_df["close"] >= idx_df["ma20"]
    above_df = (
        idx_df.pivot(index="trade_date", columns="ts_code", values="above")
        .reindex(dates)
        .fillna(False)
    )

    print("预加载全部命中表...")
    hit_frames = []
    for name in schemas.HIT_STRATEGIES:
        table = schemas.hit_table_name(name)
        df = db.read_df(
            f"SELECT trade_date, ts_code, `rank`, name, industry, raw_close, amount "
            f"FROM `{table}` WHERE trade_date BETWEEN %s AND %s",
            (START, END),
        )
        if not df.empty:
            df["strategy"] = name
            hit_frames.append(df)
    hits = pd.concat(hit_frames, ignore_index=True) if hit_frames else pd.DataFrame()

    config = rec.load_config()
    print("按日生成推荐股票（忽略当前闸门）...")
    pick_by_date: dict[str, list[rec.Pick]] = {}
    for d in dates:
        day = hits[hits["trade_date"] == d]
        if day.empty:
            pick_by_date[d] = []
            continue
        primary, secondary = rec._classify(day, config)
        picks = list(primary)
        if len(picks) < config.max_picks:
            picks.extend(secondary[: config.max_picks - len(picks)])
        pick_by_date[d] = picks

    n_picks = sum(len(v) for v in pick_by_date.values())
    print(f"生成 {n_picks} 个日度推荐样本")

    next1 = {d: dates[i + 1] for i, d in enumerate(dates) if i + 1 < len(dates)}
    next2 = {d: dates[i + 2] for i, d in enumerate(dates) if i + 2 < len(dates)}

    needed_pairs = []
    for d, picks in pick_by_date.items():
        d1 = next1.get(d)
        d2 = next2.get(d)
        if not d1 or not d2 or not picks:
            continue
        for p in picks:
            needed_pairs.append((p.ts_code, d1, d2))
    needed_pairs_df = pd.DataFrame(
        needed_pairs, columns=["ts_code", "entry_date", "exit_date"]
    )
    stock_set = tuple(needed_pairs_df["ts_code"].unique())
    date_set = tuple(
        set(needed_pairs_df["entry_date"]) | set(needed_pairs_df["exit_date"])
    )
    print(f"需查询 {len(stock_set)} 只股票、{len(date_set)} 个交易日的价格...")

    open_map: dict[tuple[str, str], float] = {}
    close_map: dict[tuple[str, str], float] = {}
    if needed_pairs:
        price_df = db.read_df(
            "SELECT ts_code, trade_date, open, close FROM daily "
            "WHERE ts_code IN %s AND trade_date IN %s",
            (stock_set, date_set),
        )
        price_df = price_df.dropna(subset=["open", "close"])
        open_map = price_df.set_index(["ts_code", "trade_date"])["open"].to_dict()
        close_map = price_df.set_index(["ts_code", "trade_date"])["close"].to_dict()

    print("评估各闸门规则...")
    summary_rows = []
    per_year_rows: dict[str, list[dict]] = {
        y: [] for y in sorted({d[:4] for d in dates})
    }

    for rule_name, rule in RULES.items():
        trade_records: list[tuple[str, float]] = []
        daily_rets: list[float] = []
        open_days = 0
        for d in dates:
            picks = pick_by_date.get(d, [])
            gate_open = rule(above_df.loc[d])
            if gate_open and picks:
                open_days += 1
                d1 = next1.get(d)
                d2 = next2.get(d)
                day_rets: list[float] = []
                for p in picks:
                    if not d1 or not d2:
                        continue
                    entry_raw = open_map.get((p.ts_code, d1))
                    exit_raw = close_map.get((p.ts_code, d2))
                    if entry_raw is None or exit_raw is None:
                        continue
                    entry = float(entry_raw)
                    exit_ = float(exit_raw)
                    if entry <= 0 or exit_ <= 0:
                        continue
                    net = exit_ / entry - 1 - COST
                    trade_records.append((d, net))
                    day_rets.append(net)
                daily_ret = sum(day_rets) / len(day_rets) if day_rets else 0.0
            else:
                daily_ret = 0.0
            daily_rets.append(daily_ret)

        rets = pd.Series(daily_rets)
        equity = (1 + rets).cumprod()
        total_return = equity.iloc[-1] - 1
        ann_factor = 252 / len(rets)
        ann_return = (equity.iloc[-1] ** ann_factor) - 1
        drawdown = (equity.cummax() - equity) / equity.cummax()
        max_dd = drawdown.max()
        trades = [r for _, r in trade_records]
        win_rate = sum(1 for r in trades if r > 0) / len(trades) if trades else 0.0
        avg_trade = sum(trades) / len(trades) if trades else 0.0
        max_loss = min(trades) if trades else 0.0

        row = {
            "规则": rule_name,
            "开仓天数": open_days,
            "空仓天数": len(dates) - open_days,
            "总信号数": len(trades),
            "胜率": fmt_pct(win_rate),
            "平均单笔": fmt_pct(avg_trade),
            "最大单笔亏损": fmt_pct(max_loss),
            "总收益": fmt_pct(total_return),
            "年化收益": fmt_pct(ann_return),
            "最大回撤": fmt_pct(max_dd),
        }
        summary_rows.append(row)

        for year in sorted(per_year_rows):
            mask = [d.startswith(year) for d in dates]
            year_rets = rets[mask]
            year_equity = (1 + year_rets).cumprod()
            year_total = year_equity.iloc[-1] - 1 if len(year_equity) else 0.0
            year_dd = (
                (year_equity.cummax() - year_equity) / year_equity.cummax()
            ).max()
            year_trades = [r for d_rec, r in trade_records if d_rec.startswith(year)]
            year_win = (
                sum(1 for r in year_trades if r > 0) / len(year_trades)
                if year_trades
                else 0.0
            )
            per_year_rows[year].append(
                {
                    "规则": rule_name,
                    "信号数": len(year_trades),
                    "胜率": fmt_pct(year_win),
                    "总收益": fmt_pct(year_total),
                    "最大回撤": fmt_pct(year_dd),
                }
            )

    print("\n=== 全区间对比（T+1开盘买 / T+2收盘卖，扣往返成本 0.3%） ===\n")
    print(pd.DataFrame(summary_rows).to_string(index=False))

    for year, rows in per_year_rows.items():
        print(f"\n=== {year} 年 ===\n")
        print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
