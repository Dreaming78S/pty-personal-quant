from __future__ import annotations

import pandas as pd

BUY_OFFSET = 1
SELL_OFFSET = 2
BUCKET_NAMES = ("<-5", "-5~-2", "-2~0", "0~2", "2~5", ">5")


def forward_returns(market: pd.DataFrame, hits: pd.DataFrame,
                    sell_offset: int = SELL_OFFSET) -> pd.DataFrame:
    """命中推荐的两日持有收益：T+1 开盘买入、T+sell_offset 收盘卖出（界面实际成交价，%）。

    价格取不复权 raw_open/raw_close（行情界面显示的真实价格），持有期内的
    分红现金不单独计入。T 为命中日；买入日与卖出日取交易日历上第 1 与第
    sell_offset 个交易日（停牌日不占位），默认 2 即 T+1 买入、T+2 卖出。
    返回列：ts_code, trade_date, buy_date, sell_date, buy_open, sell_close,
    ret_pct, status（ok / suspended / no_data）。
    """
    if sell_offset < BUY_OFFSET:
        raise ValueError(f"sell_offset 至少为 {BUY_OFFSET}（买入日），收到 {sell_offset}")
    dates = sorted(market["trade_date"].astype(str).unique())
    rank = {d: i for i, d in enumerate(dates)}
    date_of = {i: d for d, i in rank.items()}

    px = market[["ts_code", "trade_date", "raw_open", "raw_close"]].copy()
    px["trade_date"] = px["trade_date"].astype(str)

    frame = hits[["ts_code", "trade_date"]].copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["_rank"] = frame["trade_date"].map(rank)
    frame["buy_date"] = (frame["_rank"] + BUY_OFFSET).map(date_of)
    frame["sell_date"] = (frame["_rank"] + sell_offset).map(date_of)

    buy = (px[["ts_code", "trade_date", "raw_open"]]
           .rename(columns={"trade_date": "buy_date", "raw_open": "buy_open"}))
    sell = (px[["ts_code", "trade_date", "raw_close"]]
            .rename(columns={"trade_date": "sell_date", "raw_close": "sell_close"}))
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


def co_hit_starts(hits: pd.DataFrame, trade_dates, min_strategies: int = 2) -> pd.DataFrame:
    """共振段的首次出现事件：当日 ≥min_strategies 个策略命中，且前一交易日
    命中的策略数 ≤1（0 或 1 个，含未命中与停牌）。

    hits 需含 strategy/ts_code/trade_date 列；trade_dates 为交易日历（YYYYMMDD）。
    前一日未知（T 为日历首日）时剔除。返回列同 group_co_hits，另加 prev_n
    （前一交易日命中的策略数，0 或 1）。
    """
    hits = hits.copy()
    hits["trade_date"] = hits["trade_date"].astype(str)
    dates = sorted({str(d) for d in trade_dates})
    prev_of = {dates[i]: dates[i - 1] for i in range(1, len(dates))}
    counts = (hits.groupby(["ts_code", "trade_date"])["strategy"].nunique()
              .rename("prev_n"))

    events = group_co_hits(hits, min_strategies=min_strategies)
    events["prev_date"] = events["trade_date"].map(prev_of)
    prev = events[["ts_code", "prev_date"]].merge(
        counts.reset_index(), left_on=["ts_code", "prev_date"],
        right_on=["ts_code", "trade_date"], how="left")["prev_n"]
    events["prev_n"] = prev.to_numpy()
    known = events["prev_date"].notna()
    events.loc[known, "prev_n"] = events.loc[known, "prev_n"].fillna(0)

    result = events[events["prev_n"] <= 1].reset_index(drop=True)
    result["prev_n"] = result["prev_n"].astype(int)
    return result[["ts_code", "trade_date", "strategies", "n_strategies", "prev_n"]]


def group_co_hits(hits: pd.DataFrame, min_strategies: int = 2) -> pd.DataFrame:
    """按 (ts_code, trade_date) 聚合命中策略，返回共振事件。

    hits 需含 strategy/ts_code/trade_date 列；返回列：ts_code, trade_date,
    strategies（按名称排序的元组，同策略去重）、n_strategies。
    """
    grouped = (hits.groupby(["ts_code", "trade_date"])["strategy"]
               .agg(lambda s: tuple(sorted(set(s)))))
    frame = grouped.reset_index(name="strategies")
    frame["n_strategies"] = frame["strategies"].map(len)
    return frame[frame["n_strategies"] >= min_strategies].reset_index(drop=True)
