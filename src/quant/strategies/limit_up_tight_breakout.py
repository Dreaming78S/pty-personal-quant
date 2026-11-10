from __future__ import annotations

from typing import Literal

import pandas as pd
from pydantic import BaseModel, model_validator

from quant.strategies.base import Strategy, register_strategy

# 上限/下限类参数：填数值表示阈值；填 "unlimited" 表示不限制该条件。
Limit = float | Literal["unlimited"]


def _upper_ok(value: pd.Series, limit: Limit) -> bool | pd.Series:
    return True if limit == "unlimited" else value <= limit


def _upper_ok_scaled(value: pd.Series, limit: Limit,
                     scale: pd.Series) -> bool | pd.Series:
    return True if limit == "unlimited" else value <= scale * limit


def _lower_ok_scaled(value: pd.Series, limit: Limit,
                     scale: pd.Series) -> bool | pd.Series:
    return True if limit == "unlimited" else value >= scale * limit


class LimitUpTightBreakoutParams(BaseModel):
    min_gap_days: int = 4
    max_gap_days: int = 10
    limit_pct: float = 0.095
    range_pct: Limit = 0.10
    daily_pct: Limit = 0.04
    cumulative_pct: Limit = 0.05
    shrink_avg_ratio: Limit = 0.60
    shrink_max_ratio: Limit = 0.80
    breakout_avg_vol_ratio: Limit = 2.0
    breakout_base_vol_ratio: Limit = 1.5

    @model_validator(mode="after")
    def _validate_gap_range(self):
        if self.min_gap_days < 2:
            raise ValueError("min_gap_days 必须 ≥ 2（至少保留 1 个震荡日）")
        if self.max_gap_days < self.min_gap_days:
            raise ValueError("max_gap_days 必须 ≥ min_gap_days")
        return self


@register_strategy("limit_up_tight_breakout")
class LimitUpTightBreakout(Strategy):
    """首轮涨停后窄幅缩量整理 N 日（4≤N≤10），今日再度放量涨停突破。"""

    Params = LimitUpTightBreakoutParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.max_gap_days + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close, high, low = bars["close"], bars["high"], bars["low"]
        vol = bars["vol"]

        # 涨停判定与 limit_up_shakeout 一致：不复权真实价对比交易所除权后昨收。
        limit_up = (bars["raw_close"]
                    >= bars["raw_pre_close"] * (1.0 + p.limit_pct))
        daily_move = (close / close.shift(1) - 1.0).abs()

        signal = pd.Series(False, index=bars.index)
        for n in range(p.min_gap_days, p.max_gap_days + 1):
            window = n - 1
            base_close = close.shift(n)
            base_vol = vol.shift(n)
            # 震荡期 = T-N+1 .. T-1（不含首轮涨停日与信号日）。
            cons_high = high.shift(1).rolling(window).max()
            cons_low = low.shift(1).rolling(window).min()
            cons_vol_avg = vol.shift(1).rolling(window).mean()
            cons_vol_max = vol.shift(1).rolling(window).max()
            max_daily_move = daily_move.shift(1).rolling(window).max()

            base_ok = (base_close > 0) & (base_vol > 0)
            range_ok = _upper_ok((cons_high - cons_low) / base_close, p.range_pct)
            daily_ok = _upper_ok(max_daily_move, p.daily_pct)
            cumulative_ok = _upper_ok(
                (close.shift(1) / base_close - 1.0).abs(), p.cumulative_pct)
            shrink_ok = (
                _upper_ok_scaled(cons_vol_avg, p.shrink_avg_ratio, base_vol)
                & _upper_ok_scaled(cons_vol_max, p.shrink_max_ratio, base_vol)
            )
            breakout_ok = (
                _lower_ok_scaled(vol, p.breakout_avg_vol_ratio, cons_vol_avg)
                & _lower_ok_scaled(vol, p.breakout_base_vol_ratio, base_vol)
            )

            signal |= (limit_up.shift(n, fill_value=False) & limit_up
                       & base_ok & range_ok & daily_ok & cumulative_ok
                       & shrink_ok & breakout_ok)

        return signal.fillna(False)
