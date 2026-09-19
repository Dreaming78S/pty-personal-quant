import pandas as pd

from quant.strategies import get_strategy

PARAMS = dict(high_days=3, min_amount=10_000_000.0)


def make_bars(highs, opens, closes, amounts, circ_mv=None):
    n = len(closes)
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(n)],
        "open": opens,
        "high": highs,
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
        "vol": [100.0] * n,
        "amount": amounts,
        "circ_mv": circ_mv if circ_mv is not None else [100.0] * n,
    })


def test_turtle_trade_true_on_breakout():
    highs = [10, 10, 10, 11]
    opens = [9.5, 9.5, 9.5, 10.5]
    closes = [10, 10, 10, 11]
    amounts = [5000, 5000, 5000, 20000]  # 千元，20000 千元 = 2000 万元
    s = get_strategy("turtle_trade", **PARAMS)

    signals = s.generate_signals(make_bars(highs, opens, closes, amounts))

    assert list(signals) == [False, False, False, True]


def test_turtle_trade_false_without_liquidity():
    amounts = [5000, 5000, 5000, 10000]  # 10000 千元 = 1000 万元，未"过亿"
    s = get_strategy("turtle_trade", **PARAMS)
    signals = s.generate_signals(
        make_bars([10, 10, 10, 11], [9.5, 9.5, 9.5, 10.5], [10, 10, 10, 11], amounts))
    assert not signals.any()


def test_turtle_trade_false_on_bearish_candle():
    opens = [9.5, 9.5, 9.5, 12.0]
    s = get_strategy("turtle_trade", **PARAMS)
    signals = s.generate_signals(
        make_bars([10, 10, 10, 12], opens, [10, 10, 10, 11],
                  [5000, 5000, 5000, 20000]))
    assert not signals.any()


def test_turtle_trade_false_without_true_rise():
    closes = [10, 10, 10, 10]
    s = get_strategy("turtle_trade", **PARAMS)
    signals = s.generate_signals(
        make_bars([10, 10, 10, 10.5], [9.5, 9.5, 9.5, 9.5], closes,
                  [5000, 5000, 5000, 20000]))
    assert not signals.any()


def test_turtle_trade_rank_is_circ_mv():
    bars = make_bars([10], [9.5], [10], [5000], circ_mv=[123.4])
    s = get_strategy("turtle_trade")
    assert list(s.rank(bars)) == [123.4]


def test_turtle_trade_defaults_and_warmup():
    s = get_strategy("turtle_trade")
    assert s.p.high_days == 20
    assert s.p.min_amount == 100_000_000.0
    assert s.warmup_days == 21
