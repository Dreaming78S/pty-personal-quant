from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy

_EPS = 1e-9


class UptrendLimitDownParams(BaseModel):
    ma_short: int = 20
    ma_long: int = 60
    limit_pct: float = 0.095
    vol_ma: int = 20
    vol_ratio: float = 2.0


@register_strategy("uptrend_limit_down")
class UptrendLimitDown(Strategy):
    """上升趋势中放量跌停的错杀机会。"""

    Params = UptrendLimitDownParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.ma_long + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close, raw_close, vol = bars["close"], bars["raw_close"], bars["vol"]

        ma_short = close.rolling(p.ma_short).mean()
        ma_long = close.rolling(p.ma_long).mean()
        # 均线数学相等（含浮点 1ulp 噪声）不算多头排列，避免面板长短导致信号闪烁
        diff_prev = (ma_short - ma_long).shift(1)
        uptrend = diff_prev > _EPS * ma_long.shift(1).abs()
        limit_down = raw_close <= raw_close.shift(1) * (1.0 - p.limit_pct)
        heavy = vol > p.vol_ratio * vol.rolling(p.vol_ma).mean()

        return (uptrend & limit_down & heavy).fillna(False)
