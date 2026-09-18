from __future__ import annotations

import math
from dataclasses import dataclass

from quant.utils.codes import board_of


def lot_rule(ts_code: str) -> tuple[int, int]:
    """返回（最小申报股数，递增股数）。科创板 200 股起、1 股递增。"""
    if board_of(ts_code) == "star":
        return 200, 1
    return 100, 100


def round_lot(shares: int, ts_code: str) -> int:
    minimum, increment = lot_rule(ts_code)
    if shares < minimum:
        return 0
    return (shares - minimum) // increment * increment + minimum


def _valid(value) -> bool:
    if value is None:
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def blocked_buy(raw_open: float, up_limit) -> bool:
    return _valid(up_limit) and raw_open >= float(up_limit) - 1e-9


def blocked_sell(raw_open: float, down_limit) -> bool:
    return _valid(down_limit) and raw_open <= float(down_limit) + 1e-9


@dataclass
class FeeConfig:
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005      # 仅卖出
    transfer_fee_rate: float = 0.00001  # 双边
    slippage: float = 0.001


def commission_of(amount: float, fees: FeeConfig) -> float:
    amount = float(amount)
    return max(amount * fees.commission_rate, fees.min_commission)


def buy_fee(amount: float, fees: FeeConfig) -> float:
    amount = float(amount)
    return commission_of(amount, fees) + amount * fees.transfer_fee_rate


def sell_fee(amount: float, fees: FeeConfig) -> float:
    amount = float(amount)
    return (commission_of(amount, fees)
            + amount * fees.stamp_tax_rate
            + amount * fees.transfer_fee_rate)


def apply_slippage(price: float, side: str, slippage: float) -> float:
    price = float(price)
    if side == "buy":
        return price * (1 + slippage)
    return price * (1 - slippage)
