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
    """全市场 120 日相对强度前 10% 且接近 120 日高点的突破。"""

    Params = RpsBreakoutParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.window + 1

    def prepare(self, market: pd.DataFrame) -> pd.DataFrame:
        market = market.copy()
        prev_close = market.groupby("ts_code")["close"].shift(self.p.window)
        ret = market["close"] / prev_close - 1.0
        eligible = ret.notna() & ~market["suspended"].astype(bool)
        rps = ret.where(eligible).groupby(market["trade_date"]).rank(pct=True) * 100.0
        market["rps"] = rps
        return market

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        high_max = bars["high"].rolling(p.window, min_periods=p.high_min_bars).max()
        signal = (bars["rps"] >= p.rps_threshold) & (bars["close"] >= high_max * p.high_ratio)
        return signal.fillna(False)

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        return pd.to_numeric(bars["rps"], errors="coerce")
