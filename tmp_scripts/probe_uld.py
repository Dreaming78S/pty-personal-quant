import pandas as pd

from quant.data import cache, db

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)

print(db.read_df("SHOW COLUMNS FROM hit_uptrend_limit_down"))
print(db.read_df("SELECT * FROM hit_uptrend_limit_down WHERE trade_date=%s", ("20261030",)))
print(db.read_df("SELECT COUNT(*) n, MIN(trade_date) mn, MAX(trade_date) mx FROM hit_uptrend_limit_down"))

cal = cache.load_table("trade_cal")
open_dates = sorted(cal.loc[cal["is_open"] == 1, "cal_date"].astype(str))
print("last open dates", open_dates[-8:])
daily = cache.load_table("daily", columns=["ts_code", "trade_date"])
print("daily max", daily["trade_date"].max())
