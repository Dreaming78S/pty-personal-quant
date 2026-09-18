from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class HighTightFlagParams(BaseModel):
    lookback: int = 120
    min_gain: float = 0.3
    tight_days: int = 10
    max_range: float = 0.08
    vol_shrink: float = 0.6
    vol_base_days: int = 20
    high_days: int = 250
    near_high: float = 0.05


@register_strategy("high_tight_flag")
class HighTightFlag(Strategy):
    """高位紧缩旗形：前期大涨 + 近期窄幅缩量整理 + 靠近 52 周高点。"""

    Params = HighTightFlagParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = max(self.p.high_days, self.p.lookback) + self.p.tight_days

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close, high, low, vol = bars["close"], bars["high"], bars["low"], bars["vol"]

        gain = close / close.shift(p.lookback) - 1.0
        uptrend = gain >= p.min_gain

        tight_high = high.rolling(p.tight_days).max()
        tight_low = low.rolling(p.tight_days).min()
        tight = (tight_high - tight_low) / tight_low <= p.max_range

        vol_now = vol.rolling(p.tight_days).mean()
        vol_base = vol.shift(p.tight_days).rolling(p.vol_base_days).mean()
        quiet = vol_now <= p.vol_shrink * vol_base

        high_window = close.rolling(p.high_days).max()
        near = close >= (1 - p.near_high) * high_window

        signal = uptrend & tight & quiet & near
        return signal.fillna(False)
