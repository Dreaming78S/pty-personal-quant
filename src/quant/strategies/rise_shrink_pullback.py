from __future__ import annotations

from typing import Literal

import pandas as pd
from pydantic import BaseModel, model_validator

from quant.strategies.base import Strategy, register_strategy


class RiseShrinkPullbackParams(BaseModel):
    rise_days: int = 10
    rise_pct: float = 25.0
    pullback_days: int = 3
    min_bearish: int = 2
    vol_shrink: float = 0.80
    bearish_mode: Literal["close_below_open", "close_below_prev_close"] = (
        "close_below_open")

    @model_validator(mode="after")
    def _require_rise_days_above_pullback_days(self):
        if self.rise_days <= self.pullback_days:
            raise ValueError("rise_days 必须大于 pullback_days")
        return self


@register_strategy("rise_shrink_pullback")
class RiseShrinkPullback(Strategy):
    """近 M 日涨幅超 X% 后，近 P 日出现至少 L 根阴线且均量萎缩。"""

    Params = RiseShrinkPullbackParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.rise_days + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close, open_, vol = bars["close"], bars["open"], bars["vol"]

        rise = close / close.shift(p.rise_days) - 1.0 > p.rise_pct / 100.0
        if p.bearish_mode == "close_below_open":
            bearish = close < open_
        else:
            bearish = close < close.shift(1)
        enough_bearish = (bearish.rolling(p.pullback_days).sum()
                          >= p.min_bearish)
        shrink = (vol.rolling(p.pullback_days).mean()
                  < p.vol_shrink * vol.rolling(p.rise_days).mean())

        return (rise & enough_bearish & shrink).fillna(False)
