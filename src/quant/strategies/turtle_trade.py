from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class TurtleTradeParams(BaseModel):
    high_days: int = 20
    min_amount: float = 100_000_000.0


@register_strategy("turtle_trade")
class TurtleTrade(Strategy):
    """20 日新高 + 成交额过亿 + 阳线真涨，按流通市值从大到小排序。"""

    Params = TurtleTradeParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.high_days + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close = bars["close"]
        prev_high = bars["high"].shift(1).rolling(p.high_days).max()
        amount_yuan = bars["amount"] * 1000.0
        signal = (
            (close > prev_high)
            & (amount_yuan > p.min_amount)
            & (close > bars["open"])
            & (close > close.shift(1))
        )
        return signal.fillna(False)

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        return pd.to_numeric(bars["circ_mv"], errors="coerce")
