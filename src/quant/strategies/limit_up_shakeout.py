from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class LimitUpShakeoutParams(BaseModel):
    limit_pct: float = 0.095
    vol_ratio: float = 2.0


@register_strategy("limit_up_shakeout")
class LimitUpShakeout(Strategy):
    """昨日涨停、今日放量收阴不破昨收（洗盘回踩）。"""

    Params = LimitUpShakeoutParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = 3

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        raw_close = bars["raw_close"]
        close, open_ = bars["close"], bars["open"]
        vol = bars["vol"]

        limit_up = raw_close.shift(1) >= raw_close.shift(2) * (1.0 + p.limit_pct)
        bearish = close < open_
        heavy = vol > p.vol_ratio * vol.shift(1)
        support = bars["low"] >= close.shift(1)

        return (limit_up & bearish & heavy & support).fillna(False)
