from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(-drawdown.min()) if len(drawdown) else 0.0


def annualized_return(equity: pd.Series) -> float:
    periods = len(equity) - 1
    if periods <= 0:
        return 0.0
    total = float(equity.iloc[-1]) / float(equity.iloc[0])
    if total <= 0:
        return -1.0
    return float(total ** (TRADING_DAYS / periods) - 1.0)


def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    if returns.empty:
        return 0.0
    excess = returns - risk_free_rate / TRADING_DAYS
    std = float(excess.std(ddof=1))
    if np.isnan(std) or std < 1e-12:
        return 0.0
    return float(excess.mean() / std * np.sqrt(TRADING_DAYS))


def trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    """FIFO 配对买卖，统计胜率与交易对数。"""
    if trades is None or trades.empty:
        return {"胜率": 0.0, "交易对数": 0.0}
    lots: dict[str, list[list[float]]] = {}
    wins = 0
    closed = 0
    for _, trade in trades.sort_values("trade_date").iterrows():
        code = trade["ts_code"]
        if trade["side"] == "buy":
            lots.setdefault(code, []).append(
                [float(trade["price"]), float(trade["shares"]), float(trade["fee"])])
            continue
        remaining = float(trade["shares"])
        proceeds = float(trade["price"]) * remaining - float(trade["fee"])
        cost = 0.0
        while remaining > 0 and lots.get(code):
            lot = lots[code][0]
            used = min(lot[1], remaining)
            ratio = used / lot[1]
            cost += (lot[0] * lot[1] + lot[2]) * ratio
            lot[1] -= used
            remaining -= used
            if lot[1] <= 0:
                lots[code].pop(0)
        if cost > 0:
            closed += 1
            if proceeds > cost:
                wins += 1
    return {
        "胜率": wins / closed if closed else 0.0,
        "交易对数": float(closed),
    }


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame | None = None,
                    risk_free_rate: float = 0.0) -> dict[str, float]:
    series = equity["equity"].astype(float).reset_index(drop=True)
    returns = series.pct_change().dropna()
    result = {
        "累计收益": float(series.iloc[-1] / series.iloc[0] - 1.0),
        "年化收益": annualized_return(series),
        "最大回撤": max_drawdown(series),
        "夏普比率": sharpe_ratio(returns, risk_free_rate),
        "年化波动率": float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if len(returns) > 1 else 0.0,
    }
    result["卡玛比率"] = (result["年化收益"] / result["最大回撤"]
                          if result["最大回撤"] > 0 else 0.0)

    monthly = series.copy()
    monthly.index = pd.to_datetime(equity["trade_date"], format="%Y%m%d")
    monthly_return = monthly.resample("ME").last().pct_change().dropna()
    result["月胜率"] = float((monthly_return > 0).mean()) if len(monthly_return) else 0.0

    if "benchmark" in equity.columns and equity["benchmark"].notna().any():
        bench = equity["benchmark"].astype(float).ffill()
        bench_norm = bench / bench.dropna().iloc[0]
        equity_norm = series / series.iloc[0]
        result["基准收益"] = float(bench_norm.iloc[-1] - 1.0)
        result["超额收益"] = float(equity_norm.iloc[-1] / bench_norm.iloc[-1] - 1.0)
        result["超额最大回撤"] = max_drawdown(equity_norm / bench_norm)

    if trades is not None and not trades.empty:
        result.update(trade_stats(trades))
        average_equity = float(series.mean())
        result["换手率"] = (float(trades["amount"].sum()) / average_equity
                            if average_equity else 0.0)
    return result
