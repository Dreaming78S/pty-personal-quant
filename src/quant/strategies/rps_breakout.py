from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class RpsBreakoutParams(BaseModel):
    window: int = 120
    rps_threshold: float = 90.0
    high_ratio: float = 0.90
    high_min_bars: int = 60


@register_strategy("rps_breakout")
class RpsBreakout(Strategy):
    """全市场 120 日相对强度前 10% 且接近 120 日高点的突破。

    涨幅基准日与高点窗口一律按交易日历计算（而非按面板行数），
    因此结果与调用方加载面板的长短无关；停牌日不占窗口位置。
    """

    Params = RpsBreakoutParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.window + 1

    def prepare(self, market: pd.DataFrame) -> pd.DataFrame:
        market = market.copy()
        market["trade_date"] = market["trade_date"].astype(str)
        dates = sorted(market["trade_date"].unique())
        rank_of = {d: i for i, d in enumerate(dates)}
        date_of = {i: d for d, i in rank_of.items()}

        lookup_date = (market["trade_date"].map(rank_of)
                       - self.p.window).map(date_of)
        close_map = market[["ts_code", "trade_date", "close"]].rename(
            columns={"trade_date": "lookup_date", "close": "ref_close"})
        ref = (market[["ts_code"]].assign(lookup_date=lookup_date)
               .merge(close_map, on=["ts_code", "lookup_date"], how="left"))
        ret = pd.Series(market["close"].to_numpy() / ref["ref_close"].to_numpy()
                        - 1.0, index=market.index)

        wide = market.pivot(index="trade_date", columns="ts_code", values="high")
        high_max = wide.rolling(self.p.window,
                                min_periods=self.p.high_min_bars).max()
        row = market["trade_date"].map({d: i for i, d in enumerate(wide.index)})
        col = pd.Categorical(market["ts_code"], categories=wide.columns)
        market["high_max"] = high_max.to_numpy()[row.to_numpy(), col.codes]

        eligible = ret.notna() & ~market["suspended"].astype(bool)
        rps = ret.where(eligible).groupby(market["trade_date"]).rank(pct=True) * 100.0
        market["rps"] = rps
        return market

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        signal = ((bars["rps"] >= p.rps_threshold)
                  & (bars["close"] >= bars["high_max"] * p.high_ratio))
        return signal.fillna(False)

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        return pd.to_numeric(bars["rps"], errors="coerce")
