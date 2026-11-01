"""最终筛选：2026 年 uptrend_limit_down 连续两日跳空低开样本。"""
from __future__ import annotations

import pandas as pd

from quant.data import cache, db

START, END, LOAD_START = "20260101", "20261231", "20251101"

hits = db.read_df(
    "SELECT ts_code, trade_date, name, industry, amount, score "
    "FROM hit_uptrend_limit_down WHERE trade_date BETWEEN %s AND %s "
    "ORDER BY trade_date, ts_code",
    (START, END),
)
daily = cache.load_table(
    "daily",
    columns=["ts_code", "trade_date", "open", "high", "low", "close", "pre_close"],
    start=LOAD_START, end=END,
)
adj = cache.load_table(
    "adj_factor", columns=["ts_code", "trade_date", "adj_factor"],
    start=LOAD_START, end=END,
)
for col in ["open", "high", "low", "close", "pre_close"]:
    daily[col] = pd.to_numeric(daily[col], errors="coerce")
adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
daily = daily.merge(adj, on=["ts_code", "trade_date"], how="left")
daily["adj_factor"] = daily["adj_factor"].fillna(1.0)
for col in ["open", "high", "low", "close"]:
    daily[f"a_{col}"] = daily[col] * daily["adj_factor"]

cal = cache.load_table("trade_cal")
cal = cal[(cal["exchange"] == "SSE") & (cal["is_open"] == 1)].sort_values("cal_date").copy()
cal["prev_date"] = cal["cal_date"].shift(1)
cal["prev2_date"] = cal["cal_date"].shift(2)
cal = cal[["cal_date", "prev_date", "prev2_date"]]

hit_dates = set(hits["trade_date"].astype(str))
needed = set(hit_dates) | set(cal.loc[cal["cal_date"].isin(hit_dates), "prev_date"].dropna()) \
    | set(cal.loc[cal["cal_date"].isin(hit_dates), "prev2_date"].dropna())
quotes = daily[daily["trade_date"].isin(needed)].set_index(["ts_code", "trade_date"])

rows = []
for h in hits.itertuples(index=False):
    code, d0 = h.ts_code, str(h.trade_date)
    r = cal[cal["cal_date"] == d0]
    if r.empty:
        continue
    d1, d2 = r.iloc[0]["prev_date"], r.iloc[0]["prev2_date"]
    try:
        q0, q1, q2 = quotes.loc[(code, d0)], quotes.loc[(code, d1)], quotes.loc[(code, d2)]
    except KeyError:
        continue
    rows.append({
        "代码": code, "名称": h.name, "行业": h.industry, "命中日期": d0,
        "前一日": d1, "前两日": d2,
        "前日开盘": q1["open"], "前日最低": q1["low"], "前日收盘": q1["close"],
        "前日昨收": q1["pre_close"], "前两日最低": q2["low"], "前两日收盘": q2["close"],
        "当日开盘": q0["open"], "当日最低": q0["low"], "当日收盘": q0["close"],
        "当日昨收": q0["pre_close"],
        "成交额(千元)": float(h.amount),
        # 跳空用后复权价，避免除权缺口误判
        "当日复权开盘": q0["a_open"], "前日复权收盘": q1["a_close"],
        "前日复权开盘": q1["a_open"], "前两日复权收盘": q2["a_close"],
        "当日跳空": q0["a_open"] < q1["a_low"],
        "前日跳空": q1["a_open"] < q2["a_low"],
        "当日跳空幅度": q0["a_open"] / q1["a_low"] - 1,
        "前日跳空幅度": q1["a_open"] / q2["a_low"] - 1,
        "当日涨跌幅": q0["close"] / q0["pre_close"] - 1,
        "前日涨跌幅": q1["close"] / q1["pre_close"] - 1,
        "当日收绿": q0["close"] < q0["pre_close"],
        "当日阴线": q0["close"] < q0["open"],
        "前日收绿": q1["close"] < q1["pre_close"],
    })

df = pd.DataFrame(rows)
strict = df[df["当日跳空"] & df["前日跳空"]].copy()
loose = df[
    (df["当日复权开盘"] < df["前日复权收盘"])
    & (df["前日复权开盘"] < df["前两日复权收盘"])
].copy()
strict = strict.sort_values(["命中日期", "代码"]).reset_index(drop=True)
loose = loose.sort_values(["命中日期", "代码"]).reset_index(drop=True)

strict.to_csv("tmp_scripts/uptrend_gap_strict.csv", index=False, encoding="utf-8-sig")
loose.to_csv("tmp_scripts/uptrend_gap_loose.csv", index=False, encoding="utf-8-sig")

print("2026 命中:", len(hits), "可对齐前两日:", len(df))
print("严格双跳空:", len(strict))
print("  其中前一日收跌:", int(strict["前日收绿"].sum()))
print("  其中当日阴线:", int(strict["当日阴线"].sum()))
print("严格双跳空+前一日收跌+当日阴线:", int((strict["前日收绿"] & strict["当日阴线"]).sum()))
print("宽松双跳空:", len(loose))
print("  其中前一日收跌:", int(loose["前日收绿"].sum()))
print("  其中当日阴线:", int(loose["当日阴线"].sum()))
