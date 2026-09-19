from __future__ import annotations

import pandas as pd

BUY_OFFSET = 1
SELL_OFFSET = 2
BUCKET_NAMES = ("<-5", "-5~-2", "-2~0", "0~2", "2~5", ">5")


def forward_returns(market: pd.DataFrame, hits: pd.DataFrame) -> pd.DataFrame:
    """命中推荐的两日持有收益：T+1 开盘买入、T+2 收盘卖出（后复权价，%）。

    T 为命中日；T+1/T+2 取交易日历上紧邻的两个交易日（停牌日不占位）。
    返回列：ts_code, trade_date, buy_date, sell_date, buy_open, sell_close,
    ret_pct, status（ok / suspended / no_data）。
    """
    dates = sorted(market["trade_date"].astype(str).unique())
    rank = {d: i for i, d in enumerate(dates)}
    date_of = {i: d for d, i in rank.items()}

    px = market[["ts_code", "trade_date", "open", "close"]].copy()
    px["trade_date"] = px["trade_date"].astype(str)

    frame = hits[["ts_code", "trade_date"]].copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["_rank"] = frame["trade_date"].map(rank)
    frame["buy_date"] = (frame["_rank"] + BUY_OFFSET).map(date_of)
    frame["sell_date"] = (frame["_rank"] + SELL_OFFSET).map(date_of)

    buy = (px[["ts_code", "trade_date", "open"]]
           .rename(columns={"trade_date": "buy_date", "open": "buy_open"}))
    sell = (px[["ts_code", "trade_date", "close"]]
            .rename(columns={"trade_date": "sell_date", "close": "sell_close"}))
    frame = frame.merge(buy, on=["ts_code", "buy_date"], how="left")
    frame = frame.merge(sell, on=["ts_code", "sell_date"], how="left")

    frame["ret_pct"] = ((frame["sell_close"] - frame["buy_open"])
                        / frame["buy_open"] * 100.0)
    frame["status"] = "ok"
    missing_price = frame["buy_open"].isna() | frame["sell_close"].isna()
    frame.loc[missing_price & frame["sell_date"].notna(), "status"] = "suspended"
    frame.loc[frame["sell_date"].isna(), "status"] = "no_data"
    return frame[["ts_code", "trade_date", "buy_date", "sell_date",
                  "buy_open", "sell_close", "ret_pct", "status"]]


def bucket_stats(returns: pd.Series) -> dict[str, int]:
    """收益分桶计数（分区无重叠）：>0 为盈利，<=0 记亏。

    <-5；-5~-2（含 -5 与 -2）；-2~0（含 0）；0~2；2~5；>5（含 5）。
    """
    r = pd.to_numeric(returns, errors="coerce").dropna()
    return {
        "可计算": int(len(r)),
        "盈利": int((r > 0).sum()),
        "<-5": int((r < -5).sum()),
        "-5~-2": int(((r >= -5) & (r <= -2)).sum()),
        "-2~0": int(((r > -2) & (r <= 0)).sum()),
        "0~2": int(((r > 0) & (r < 2)).sum()),
        "2~5": int(((r >= 2) & (r < 5)).sum()),
        ">5": int((r >= 5).sum()),
    }


def bucket_counts(stats: dict[str, int]) -> dict[str, int]:
    return {name: stats[name] for name in BUCKET_NAMES}
