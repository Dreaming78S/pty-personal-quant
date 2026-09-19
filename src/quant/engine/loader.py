from __future__ import annotations

import bisect
import datetime
from dataclasses import dataclass

import pandas as pd

from quant.data import cache
from quant.utils.codes import board_of

MARKET_NUMERIC_COLUMNS = (
    "open", "high", "low", "close",
    "raw_open", "raw_high", "raw_low", "raw_close", "raw_pre_close",
    "vol", "amount", "adj_factor", "up_limit", "down_limit",
    "turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv",
)


@dataclass(frozen=True)
class UniverseFilters:
    exclude_st: bool = True
    min_list_days: int = 60
    exclude_suspended: bool = True
    allowed_boards: tuple[str, ...] | None = None


def _date_ranks(trade_cal: pd.DataFrame) -> tuple[dict[str, int], list[str]]:
    open_dates = sorted(trade_cal.loc[trade_cal["is_open"] == 1, "cal_date"].astype(str))
    return {d: i for i, d in enumerate(open_dates)}, open_dates


def _st_flags(daily: pd.DataFrame, namechange: pd.DataFrame) -> pd.Series:
    if namechange is None or namechange.empty:
        return pd.Series(False, index=daily.index)
    st = namechange[
        namechange["name"].astype(str).str.upper().str.contains("ST")]
    if st.empty:
        return pd.Series(False, index=daily.index)

    merged = daily[["ts_code", "trade_date"]].merge(
        st[["ts_code", "start_date", "end_date"]], on="ts_code", how="inner")
    start = merged["start_date"].fillna("")
    end = merged["end_date"].fillna("")
    within = (merged["trade_date"] >= start) & (
        end.eq("") | (merged["trade_date"] <= end))
    keys = set(zip(merged.loc[within, "ts_code"], merged.loc[within, "trade_date"]))

    index = pd.MultiIndex.from_arrays([daily["ts_code"], daily["trade_date"]])
    return pd.Series(index.isin(keys), index=daily.index)


def build_market(daily: pd.DataFrame, adj_factor: pd.DataFrame,
                 daily_basic: pd.DataFrame, suspend_d: pd.DataFrame,
                 stk_limit: pd.DataFrame, stock_basic: pd.DataFrame,
                 namechange: pd.DataFrame, trade_cal: pd.DataFrame,
                 filters: UniverseFilters) -> pd.DataFrame:
    """合并各表：输出后复权 OHLC（open/high/low/close）+ raw_* 原始价 + 过滤标记。"""
    df = daily.copy()
    for col in ("open", "high", "low", "close", "pre_close"):
        df[f"raw_{col}"] = df[col]

    df = df.merge(adj_factor[["ts_code", "trade_date", "adj_factor"]],
                  on=["ts_code", "trade_date"], how="left")

    basic_cols = ["ts_code", "trade_date", "turnover_rate", "volume_ratio",
                  "pe_ttm", "pb", "total_mv", "circ_mv"]
    df = df.merge(daily_basic[[c for c in basic_cols if c in daily_basic.columns]],
                  on=["ts_code", "trade_date"], how="left")

    limits = stk_limit[["ts_code", "trade_date", "up_limit", "down_limit"]]
    df = df.merge(limits, on=["ts_code", "trade_date"], how="left")

    # MySQL DECIMAL 经 pymysql/parquet 后是 object Decimal，统一转 float64，
    # 避免费用层出现 Decimal * float 的 TypeError。
    for col in MARKET_NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    factor = df["adj_factor"].fillna(1.0)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * factor

    suspended_keys = set(zip(
        suspend_d.loc[suspend_d["suspend_type"].astype(str) == "S", "ts_code"],
        suspend_d.loc[suspend_d["suspend_type"].astype(str) == "S", "trade_date"].astype(str),
    ))
    market_index = pd.MultiIndex.from_arrays([df["ts_code"], df["trade_date"].astype(str)])
    df["suspended"] = market_index.isin(suspended_keys)

    df["is_st"] = _st_flags(df, namechange)

    rank, open_dates = _date_ranks(trade_cal)
    list_dates = dict(zip(stock_basic["ts_code"], stock_basic["list_date"].astype(str)))
    first_rank = {}
    for code, list_date in list_dates.items():
        pos = bisect.bisect_left(open_dates, list_date)
        first_rank[code] = pos if pos < len(open_dates) else len(open_dates)
    rank_series = df["trade_date"].astype(str).map(rank)
    first_rank_series = df["ts_code"].map(first_rank)
    df["is_new"] = ((rank_series - first_rank_series) < filters.min_list_days).fillna(False)

    df["board"] = df["ts_code"].map(board_of)
    return df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def eligibility_mask(market: pd.DataFrame, filters: UniverseFilters) -> pd.Series:
    mask = ~market["is_new"]
    if filters.exclude_st:
        mask &= ~market["is_st"].astype(bool)
    if filters.exclude_suspended:
        mask &= ~market["suspended"].astype(bool)
    if filters.allowed_boards:
        mask &= market["board"].isin(filters.allowed_boards)
    return mask


def apply_universe(market: pd.DataFrame, filters: UniverseFilters) -> pd.DataFrame:
    return market[eligibility_mask(market, filters)].reset_index(drop=True)


def load_market_data(start: str, end: str, warmup_days: int = 0,
                     filters: UniverseFilters | None = None,
                     ensure: bool = False) -> pd.DataFrame:
    filters = filters or UniverseFilters()
    if ensure:
        cache.ensure_all(["trade_cal", "stock_basic", "namechange", "daily",
                          "adj_factor", "daily_basic", "suspend_d", "stk_limit",
                          "index_daily"])
    trade_cal = cache.load_table("trade_cal")
    rank, open_dates = _date_ranks(trade_cal)
    pos = bisect.bisect_left(open_dates, start)
    load_start = open_dates[max(0, pos - warmup_days)] if open_dates else start

    frame = lambda table: cache.load_table(table, start=load_start, end=end)  # noqa: E731
    daily = frame("daily")
    if daily.empty:
        return daily
    market = build_market(
        daily, frame("adj_factor"), frame("daily_basic"), frame("suspend_d"),
        frame("stk_limit"), cache.load_table("stock_basic"),
        cache.load_table("namechange"), trade_cal, filters,
    )
    return market


def resolve_trade_date(date: str | None = None) -> str:
    """未指定日期时取 <= 今天的最近交易日，避免未来日历把选股日期推到未来。"""
    trade_cal = cache.load_table("trade_cal")
    open_dates = sorted(trade_cal.loc[trade_cal["is_open"] == 1, "cal_date"].astype(str))
    cap = date or datetime.date.today().strftime("%Y%m%d")
    candidates = [d for d in open_dates if d <= cap]
    if not candidates:
        raise ValueError(f"找不到 <= {cap} 的交易日")
    return candidates[-1]


def load_stock_names() -> pd.DataFrame:
    return cache.load_table("stock_basic", columns=["ts_code", "name"])


def load_stock_industries() -> pd.DataFrame:
    return cache.load_table("stock_basic", columns=["ts_code", "industry"])


def load_benchmark(ts_code: str, start: str, end: str) -> pd.Series:
    df = cache.load_table("index_daily", columns=["ts_code", "trade_date", "close"],
                          start=start, end=end, ts_codes=[ts_code])
    if df.empty:
        raise ValueError(f"缓存中没有基准指数 {ts_code} 的行情，请先入库 index_daily")
    series = pd.to_numeric(
        df.sort_values("trade_date").set_index("trade_date")["close"],
        errors="coerce")
    series.name = ts_code
    return series
