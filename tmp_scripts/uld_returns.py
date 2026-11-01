import pandas as pd

from quant.data import cache, db
from quant.engine import loader

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 50)
pd.set_option("display.unicode.east_asian_width", True)

TARGET = "20261030"

hits = db.read_df(
    "SELECT `rank`, ts_code, name, industry, close, raw_close, amount, score, "
    "streak FROM hit_uptrend_limit_down WHERE trade_date=%s ORDER BY `rank`",
    (TARGET,),
)

market = loader.load_market_data("20200101", TARGET, ensure=False)
px = market[["ts_code", "trade_date", "close", "raw_close"]].copy()
px["trade_date"] = px["trade_date"].astype(str)

cal = cache.load_table("trade_cal")
open_dates = sorted(cal.loc[cal["is_open"] == 1, "cal_date"].astype(str))
rank_of = {d: i for i, d in enumerate(open_dates)}
t = rank_of[TARGET]

rows = []
for row in hits.itertuples(index=False):
    sub = px[px["ts_code"] == row.ts_code].set_index("trade_date").sort_index()
    rec = {
        "排名": row.rank,
        "代码": row.ts_code,
        "名称": row.name,
        "行业": row.industry,
        "收盘(后复权)": round(float(row.close), 2),
        "收盘(原始)": round(float(row.raw_close), 2),
    }
    for n in (10, 20, 40):
        d0 = open_dates[t - n]
        if d0 in sub.index and pd.notna(sub.loc[d0, "close"]):
            rec[f"近{n}日%"] = round(
                (sub.loc[TARGET, "close"] / sub.loc[d0, "close"] - 1) * 100, 2)
        else:
            rec[f"近{n}日%"] = None
    rows.append(rec)

out = pd.DataFrame(rows)
print(out.to_string(index=False))

print("\n=== 汇总（后复权收盘，T vs T-N 交易日）===")
summary = []
for n in (10, 20, 40):
    s = out[f"近{n}日%"].dropna()
    summary.append({
        "窗口": f"近{n}日",
        "样本数": len(s),
        "均值%": round(s.mean(), 2),
        "中位数%": round(s.median(), 2),
        "最大%": round(s.max(), 2),
        "最小%": round(s.min(), 2),
        "上涨家数": int((s > 0).sum()),
        "下跌家数": int((s < 0).sum()),
        "上涨占比%": round((s > 0).mean() * 100, 1),
    })
print(pd.DataFrame(summary).to_string(index=False))
