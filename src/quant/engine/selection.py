from __future__ import annotations

import pandas as pd

from quant.engine import loader
from quant.engine.loader import UniverseFilters
from quant.strategies.base import Strategy


def compute_signals(strategy: Strategy, market: pd.DataFrame,
                    rank_by: str = "amount") -> pd.DataFrame:
    frames = []
    for ts_code, group in market.groupby("ts_code", sort=False):
        group = group.sort_values("trade_date")
        signals = strategy.generate_signals(group)
        scores = strategy.rank(group)
        if scores is None:
            scores = group[rank_by]
        frames.append(pd.DataFrame({
            "ts_code": ts_code,
            "trade_date": group["trade_date"].values,
            "signal": signals.fillna(False).astype(bool).values,
            "score": pd.to_numeric(scores, errors="coerce").values,
        }))
    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", "signal", "score"])
    return pd.concat(frames, ignore_index=True)


def run_selection(strategy: Strategy, trade_date: str, top_n: int = 20,
                  market: pd.DataFrame | None = None,
                  filters: UniverseFilters | None = None,
                  rank_by: str = "amount") -> pd.DataFrame:
    filters = filters or UniverseFilters()
    if market is None:
        market = loader.load_market_data(
            start=trade_date, end=trade_date,
            warmup_days=strategy.warmup_days, filters=filters)
    if market.empty:
        raise ValueError(f"{trade_date} 没有可用行情，请先更新数据")
    signals = compute_signals(strategy, market, rank_by=rank_by)
    today = market[market["trade_date"] == trade_date]
    eligible = loader.apply_universe(today, filters)
    candidates = signals[(signals["trade_date"] == trade_date) & signals["signal"]]
    merged = eligible.merge(candidates[["ts_code", "score"]], on="ts_code", how="inner")
    merged = merged.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
    merged.insert(0, "rank", merged.index + 1)
    names = loader.load_stock_names()
    merged = merged.merge(names, on="ts_code", how="left")
    return merged[["rank", "ts_code", "name", "trade_date", "close",
                   "raw_close", "amount", "score"]]
