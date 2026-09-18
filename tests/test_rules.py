from decimal import Decimal

import pytest

from quant.engine import rules


def test_round_lot_main_board():
    assert rules.round_lot(1234, "600000.SH") == 1200
    assert rules.round_lot(99, "600000.SH") == 0


def test_round_lot_star_board():
    assert rules.round_lot(250, "688981.SH") == 250
    assert rules.round_lot(150, "688981.SH") == 0
    assert rules.round_lot(200, "688981.SH") == 200


def test_blocked_buy_and_sell():
    assert rules.blocked_buy(10.0, 10.0)
    assert not rules.blocked_buy(9.99, 10.0)
    assert not rules.blocked_buy(9.99, None)
    assert rules.blocked_sell(10.0, 10.0)
    assert not rules.blocked_sell(10.01, 10.0)
    assert not rules.blocked_sell(10.0, float("nan"))


def test_fees():
    fees = rules.FeeConfig()
    assert rules.buy_fee(100_000, fees) == pytest.approx(26.0)
    assert rules.sell_fee(100_000, fees) == pytest.approx(76.0)
    assert rules.buy_fee(10_000, fees) == pytest.approx(5.1)  # 触发最低佣金


def test_slippage():
    assert rules.apply_slippage(10.0, "buy", 0.001) == pytest.approx(10.01)
    assert rules.apply_slippage(10.0, "sell", 0.001) == pytest.approx(9.99)


def test_fee_and_slippage_functions_accept_decimal():
    fees = rules.FeeConfig()
    amount = Decimal("100000")
    price = Decimal("10.0")

    buy = rules.buy_fee(amount, fees)
    sell = rules.sell_fee(amount, fees)
    commission = rules.commission_of(amount, fees)
    slipped = rules.apply_slippage(price, "buy", 0.001)

    assert all(isinstance(value, float) for value in (buy, sell, commission, slipped))
    assert buy == pytest.approx(26.0)
    assert sell == pytest.approx(76.0)
    assert commission == pytest.approx(25.0)
    assert slipped == pytest.approx(10.01)
