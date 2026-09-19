from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class HighTightFlagParams(BaseModel):
    momentum_days: int = 40
    momentum_ratio: float = 1.6
    tight_days: int = 10
    tight_ratio: float = 1.15
    support_ratio: float = 0.8
    vol_base_days: int = 20
    vol_shrink: float = 0.6


@register_strategy("high_tight_flag")
class HighTightFlag(Strategy):
    """强动量后高位窄幅缩量整理（高而窄的旗形）。"""

    Params = HighTightFlagParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = max(self.p.momentum_days, self.p.vol_base_days + 1)

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        high, low, vol = bars["high"], bars["low"], bars["vol"]

        momentum = (high.rolling(p.momentum_days).max()
                    / low.rolling(p.momentum_days).min())
        tight = (high.rolling(p.tight_days).max()
                 / low.rolling(p.tight_days).min())
        support = (low.rolling(p.tight_days).min()
                   >= p.support_ratio * high.rolling(p.momentum_days).max())
        quiet = vol < p.vol_shrink * vol.shift(1).rolling(p.vol_base_days).mean()

        signal = ((momentum > p.momentum_ratio) & (tight < p.tight_ratio)
                  & support & quiet)
        return signal.fillna(False)
