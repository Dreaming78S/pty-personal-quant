from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class MaVolumeParams(BaseModel):
    ma_short: int = 5
    ma_long: int = 20
    vol_ma: int = 20
    vol_ratio: float = 1.5


@register_strategy("ma_volume")
class MaVolume(Strategy):
    """短均线上穿长均线，且当日明显放量。"""

    Params = MaVolumeParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = max(self.p.ma_long, self.p.vol_ma) + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"]
        vol = bars["vol"]
        ma_short = close.rolling(self.p.ma_short).mean()
        ma_long = close.rolling(self.p.ma_long).mean()
        vol_ma = vol.rolling(self.p.vol_ma).mean()
        cross_up = (ma_short > ma_long) & (ma_short.shift(1) < ma_long.shift(1))
        signal = cross_up & (vol > self.p.vol_ratio * vol_ma)
        return signal.fillna(False)
