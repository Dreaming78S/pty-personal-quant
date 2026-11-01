import pandas as pd

from quant.data import cache, db
from quant.engine import loader

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 50)
pd.set_option("display.unicode.east_asian_width", True)

TARGET = "20261030"
hits = db.read_df(
    "SELECT `rank`, ts_code, name, industry, close, raw_close "
    "FROM hit_uptrend_limit_down WHERE trade_date=%s ORDER BY `rank`",
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
    rec = {"代码": row.ts_code, "名称": row.name}
    for n in (10, 20, 40):
        d0 = open_dates[t - n]
        rec[f"{n}日基准"] = d0
        rec[f"复权{n}日%"] = round((sub.loc[TARGET, "close"] / sub.loc[d0, "close"] - 1) * 100, 2)
        rec[f"原始{n}日%"] = round((sub.loc[TARGET, "raw_close"] / sub.loc[d0, "raw_close"] - 1) * 100, 2)
    rows.append(rec)

out = pd.DataFrame(rows)
print(out.to_string(index=False))

# 逐日收盘检查最大涨幅标的
for code in ("600838.SH", "601199.SH", "600637.SH"):
    sub = px[(px["ts_code"] == code) & (px["trade_date"] <= TARGET)].tail(45)
    print(f"\n{code} 最近45个交易日 复权收盘/原始收盘")
    print(sub[["trade_date", "close", "raw_close"]].to_string(index=False))
