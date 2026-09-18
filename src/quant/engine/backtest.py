from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from quant.engine import loader, rules, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import Strategy


@dataclass
class BacktestConfig:
    start: str
    end: str
    initial_cash: float = 1_000_000.0
    rebalance: str = "weekly"
    top_n: int = 10
    rank_by: str = "amount"
    execution: str = "next_open"
    benchmark: str = "000300.SH"
    risk_free_rate: float = 0.0
    exclude_st: bool = True
    min_list_days: int = 60
    exclude_suspended: bool = True
    allowed_boards: tuple[str, ...] | None = None
    fees: rules.FeeConfig = field(default_factory=rules.FeeConfig)
    warmup_days: int = 0


@dataclass
class Position:
    ts_code: str
    shares: int
    cost_price: float
    buy_date: str


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    config: BacktestConfig
    strategy_name: str


def rebalance_dates(trade_dates: list[str], freq: str) -> list[str]:
    if freq == "daily":
        return list(trade_dates)
    df = pd.DataFrame({"trade_date": list(trade_dates)})
    dt = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    if freq == "weekly":
        key = dt.dt.to_period("W")
    elif freq == "monthly":
        key = dt.dt.to_period("M")
    else:
        raise ValueError(f"不支持的调仓频率：{freq}")
    df["key"] = key.astype(str)
    return df.groupby("key", sort=True)["trade_date"].first().tolist()


def filters_from_config(config: BacktestConfig) -> UniverseFilters:
    return UniverseFilters(
        exclude_st=config.exclude_st,
        min_list_days=config.min_list_days,
        exclude_suspended=config.exclude_suspended,
        allowed_boards=config.allowed_boards,
    )


def run_backtest(strategy: Strategy, config: BacktestConfig,
                 market: pd.DataFrame | None = None,
                 benchmark: pd.Series | None = None) -> BacktestResult:
    if config.execution not in ("next_open", "next_close"):
        raise ValueError(f"不支持的执行方式：{config.execution}")

    filters = filters_from_config(config)
    warmup = max(config.warmup_days, strategy.warmup_days)
    if market is None:
        market = loader.load_market_data(config.start, config.end,
                                         warmup_days=warmup, filters=filters,
                                         ensure=True)
    if market.empty:
        raise ValueError("回测区间内没有行情数据")

    all_dates = sorted(market["trade_date"].astype(str).unique().tolist())
    dates = [d for d in all_dates if config.start <= d <= config.end]
    if not dates:
        raise ValueError("回测区间内没有交易日")

    signals = selection.compute_signals(strategy, market, rank_by=config.rank_by)
    positive = signals[signals["signal"]]
    signal_by_date = {
        d: group.sort_values("score", ascending=False)["ts_code"].tolist()
        for d, group in positive.groupby("trade_date")
    }

    eligible = loader.eligibility_mask(market, filters)
    eligible_by_date = {
        d: set(group["ts_code"])
        for d, group in market[eligible].groupby("trade_date")
    }

    if config.execution == "next_open":
        price_col, raw_col = "open", "raw_open"
    else:
        price_col, raw_col = "close", "raw_close"

    info = market.set_index(["trade_date", "ts_code"])[
        ["open", "close", "raw_open", "raw_close",
         "up_limit", "down_limit", "suspended"]]
    close = market.pivot_table(index="trade_date", columns="ts_code",
                               values="close", aggfunc="last")
    close = close.reindex(all_dates).ffill()

    last_market_dates = (
        market.assign(trade_date=market["trade_date"].astype(str))
        .groupby("ts_code")["trade_date"].max().to_dict()
    )

    if benchmark is not None and not benchmark.empty:
        bench = benchmark.reindex(all_dates).ffill().reindex(dates)
        valid = bench.dropna()
        if valid.empty:
            bench_norm = pd.Series(float("nan"), index=dates)
        else:
            bench_norm = bench / valid.iloc[0] * config.initial_cash
    else:
        bench_norm = pd.Series(float("nan"), index=dates)

    rebal = set(rebalance_dates(dates, config.rebalance))
    cash = config.initial_cash
    positions: dict[str, Position] = {}
    sell_queue: list[str] = []
    trade_rows: list[dict] = []
    equity_rows: list[dict] = []
    pending: list[str] | None = None

    def record_trade(date: str, code: str, side: str, price: float,
                     shares: int, fee: float) -> None:
        trade_rows.append({
            "trade_date": date, "ts_code": code, "side": side,
            "price": price, "shares": shares,
            "amount": price * shares, "fee": fee,
        })

    def settle_delisted(date: str) -> None:
        """退市股再无行情可卖：按最后已知收盘价强制结算，避免永久持有。"""
        nonlocal cash
        for code in list(positions):
            last_date = last_market_dates.get(code)
            if last_date is None or last_date >= date:
                continue
            position = positions.pop(code)
            price = float(close.loc[date, code])
            amount = price * position.shares
            fee = rules.sell_fee(amount, config.fees)
            cash += amount - fee
            if code in sell_queue:
                sell_queue.remove(code)
            record_trade(date, code, "sell", price, position.shares, fee)

    def try_sell(date: str, code: str) -> bool:
        nonlocal cash
        position = positions.get(code)
        if position is None or position.buy_date == date:
            return False
        row = info.loc[(date, code)] if (date, code) in info.index else None
        if row is None or bool(row["suspended"]):
            return False
        if rules.blocked_sell(row[raw_col], row["down_limit"]):
            return False
        price = rules.apply_slippage(row[price_col], "sell", config.fees.slippage)
        amount = price * position.shares
        fee = rules.sell_fee(amount, config.fees)
        cash += amount - fee
        record_trade(date, code, "sell", price, position.shares, fee)
        del positions[code]
        return True

    def try_buy(date: str, code: str, target_amount: float) -> None:
        nonlocal cash
        row = info.loc[(date, code)] if (date, code) in info.index else None
        if row is None or bool(row["suspended"]):
            return
        if rules.blocked_buy(row[raw_col], row["up_limit"]):
            return
        price = rules.apply_slippage(row[price_col], "buy", config.fees.slippage)
        if price <= 0:
            return
        desired = rules.round_lot(int(target_amount // price), code)
        cash_cap = rules.round_lot(
            int(cash // (price * (1 + config.fees.commission_rate
                                  + config.fees.transfer_fee_rate))), code)
        shares = min(desired, cash_cap)
        minimum, increment = rules.lot_rule(code)
        # 最低佣金可能让“按比例粗算的上限”仍然付不起，逐手回退到买得起的股数。
        while shares >= minimum and (
                price * shares + rules.buy_fee(price * shares, config.fees) > cash):
            shares -= increment
        if shares < minimum:
            return
        amount = price * shares
        fee = rules.buy_fee(amount, config.fees)
        cash -= amount + fee
        positions[code] = Position(code, shares, price, date)
        record_trade(date, code, "buy", price, shares, fee)

    def process_sell_queue(date: str) -> None:
        for code in list(sell_queue):
            if code not in positions:
                sell_queue.remove(code)
            elif try_sell(date, code):
                sell_queue.remove(code)

    def execute_pending(date: str) -> None:
        desired = set(pending or [])
        for code in list(positions):
            if code not in desired and code not in sell_queue:
                sell_queue.append(code)
        process_sell_queue(date)

        idx = all_dates.index(date)
        prev_date = all_dates[idx - 1]
        equity_estimate = cash + sum(
            position.shares * float(close.loc[prev_date, position.ts_code])
            for position in positions.values()
        )
        target_amount = equity_estimate / config.top_n
        for code in pending or []:
            if code not in positions:
                try_buy(date, code, target_amount)

    for index, date in enumerate(all_dates):
        if date not in dates:
            continue
        settle_delisted(date)
        process_sell_queue(date)
        if pending is not None:
            execute_pending(date)
            pending = None
        if date in rebal and index + 1 < len(all_dates):
            ranked = signal_by_date.get(date, [])
            allowed = eligible_by_date.get(date, set())
            pending = [code for code in ranked if code in allowed][:config.top_n]

        equity = cash + sum(
            position.shares * float(close.loc[date, position.ts_code])
            for position in positions.values()
        )
        equity_rows.append({
            "trade_date": date,
            "equity": equity,
            "cash": cash,
            "benchmark": float(bench_norm.get(date, float("nan"))),
        })

    return BacktestResult(
        equity=pd.DataFrame(equity_rows),
        trades=pd.DataFrame(trade_rows, columns=["trade_date", "ts_code", "side",
                                                 "price", "shares", "amount", "fee"]),
        config=config,
        strategy_name=strategy.name or type(strategy).__name__,
    )
