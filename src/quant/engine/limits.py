from __future__ import annotations

import pandas as pd

from quant.data import db, ingest, schemas
from quant.engine import loader
from quant.engine.loader import UniverseFilters

DEFAULT_START = "20180101"
DEFAULT_FILTERS = UniverseFilters(
    exclude_st=True,
    min_list_days=60,
    exclude_suspended=True,
    allowed_boards=("main",),
)
_EPS = 1e-6


def _main_board_mask(market: pd.DataFrame) -> pd.Series:
    """严格按 60/00 开头筛选主板，避免把 900 B 股等归入 main。"""
    symbol = market["ts_code"].astype(str).str.split(".").str[0]
    return symbol.str.match(r"^(60|00)\d{4}$").fillna(False).astype(bool)


def _eligible_mask(market: pd.DataFrame) -> pd.Series:
    return (loader.eligibility_mask(market, DEFAULT_FILTERS)
            & _main_board_mask(market))


def _limit_mask(market: pd.DataFrame, direction: str) -> pd.Series:
    if direction == "up":
        limit = market["up_limit"]
        return ((limit > 0)
                & (market["raw_close"] >= limit - _EPS)).fillna(False)
    if direction == "down":
        limit = market["down_limit"]
        return ((limit > 0)
                & (market["raw_close"] <= limit + _EPS)).fillna(False)
    raise ValueError(f"未知涨跌停方向：{direction}")


def _one_word_mask(market: pd.DataFrame, direction: str) -> pd.Series:
    """开盘、最高、最低、收盘都在涨/跌停价上，即全天封死。"""
    if direction == "up":
        limit = market["up_limit"]
        return ((limit > 0)
                & (market["raw_open"] >= limit - _EPS)
                & (market["raw_high"] >= limit - _EPS)
                & (market["raw_low"] >= limit - _EPS)).fillna(False)
    if direction == "down":
        limit = market["down_limit"]
        return ((limit > 0)
                & (market["raw_open"] <= limit + _EPS)
                & (market["raw_high"] <= limit + _EPS)
                & (market["raw_low"] <= limit + _EPS)).fillna(False)
    raise ValueError(f"未知涨跌停方向：{direction}")


def _streak_series(market: pd.DataFrame, event: pd.Series,
                   rank_of: dict[str, int]) -> pd.Series:
    """按交易日历计算连续事件天数；停牌、ST、次新等中断后重新从 1 开始。"""
    if market.empty:
        return pd.Series(dtype="int64", index=market.index)

    work = pd.DataFrame({
        "ts_code": market["ts_code"].astype(str).to_numpy(),
        "rank": market["trade_date"].astype(str).map(rank_of).to_numpy(),
        "event": event.to_numpy(dtype=bool),
    }, index=market.index)
    work = work.sort_values(["ts_code", "rank"], kind="stable")

    grouped = work.groupby("ts_code", sort=False)
    prev_event = grouped["event"].shift(1).fillna(False).astype(bool)
    prev_rank = grouped["rank"].shift(1)
    continued = (work["event"] & prev_event
                 & (work["rank"] == prev_rank + 1))
    segment = (work["event"] & ~continued).groupby(
        work["ts_code"], sort=False).cumsum()
    work["segment"] = segment
    work["streak"] = work.groupby(
        ["ts_code", "segment"], sort=False)["event"].cumsum()
    return work.sort_index()["streak"].reindex(market.index).astype(int)


def _rows_for(market: pd.DataFrame, direction: str, event: pd.Series,
              streak: pd.Series, names: pd.DataFrame,
              industries: pd.DataFrame) -> pd.DataFrame:
    rows = market.loc[event].copy()
    if rows.empty:
        return pd.DataFrame(columns=schemas.LIMIT_COLUMNS)

    limit_col = "up_limit" if direction == "up" else "down_limit"
    rows = rows.drop(columns=[c for c in ("name", "industry")
                              if c in rows.columns])
    rows["ts_code"] = rows["ts_code"].astype(str)
    rows["trade_date"] = rows["trade_date"].astype(str)
    rows["limit_price"] = rows[limit_col]
    rows["is_one_word"] = _one_word_mask(rows, direction).astype(int)
    rows["streak"] = streak.loc[rows.index].astype(int)

    numeric = ("raw_open", "raw_high", "raw_low", "raw_close",
               "raw_pre_close", "pct_chg", "limit_price", "amount", "vol",
               "turnover_rate", "circ_mv", "total_mv")
    for column in numeric:
        if column in rows.columns:
            rows[column] = pd.to_numeric(rows[column], errors="coerce")

    rows = rows.merge(names, on="ts_code", how="left")
    rows = rows.merge(industries, on="ts_code", how="left")
    rows = rows.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    return rows[list(schemas.LIMIT_COLUMNS)]


def build_limit_frames(market: pd.DataFrame,
                       rank_of: dict[str, int] | None = None
                       ) -> dict[str, pd.DataFrame]:
    """从行情面板生成涨停/跌停两张派生表的数据。"""
    empty = {table: pd.DataFrame(columns=schemas.LIMIT_COLUMNS)
             for table in schemas.LIMIT_TABLES}
    if market.empty:
        return empty

    rank_of = rank_of or {
        d: i for i, d in enumerate(loader.open_trade_dates())
    }
    names = loader.load_stock_names()
    industries = loader.load_stock_industries()
    eligible = _eligible_mask(market)
    frames: dict[str, pd.DataFrame] = {}
    for table, direction in schemas.LIMIT_DIRECTIONS:
        event = eligible & _limit_mask(market, direction)
        streak = _streak_series(market, event, rank_of)
        frames[table] = _rows_for(market, direction, event, streak,
                                  names, industries)
    return frames


def build_limit_rows(market: pd.DataFrame, direction: str,
                     rank_of: dict[str, int] | None = None) -> pd.DataFrame:
    table = dict((d, t) for t, d in schemas.LIMIT_DIRECTIONS).get(direction)
    if table is None:
        raise ValueError(f"未知涨跌停方向：{direction}")
    return build_limit_frames(market, rank_of)[table]


def fill_limits(from_date: str | None = None,
                to_date: str | None = None) -> dict[str, int]:
    """回填/增量写入 limit_up_daily、limit_down_daily，返回各表写入行数。

    起始日期取 from_date、已有水位线、DEFAULT_START 三者中最先可用的；
    显式指定 from_date 视为重算：先清空区间旧数据再写入。水位线只前进。
    """
    end_cap = to_date or loader.resolve_trade_date()
    plans = []
    for table in schemas.LIMIT_TABLES:
        watermark = ingest.get_watermark(table)
        start = from_date or watermark or DEFAULT_START
        dates = (ingest.trade_dates_between(start, end_cap)
                 if start <= end_cap else [])
        plans.append((table, watermark, dates))

    bounds = [d for _, _, dates in plans for d in (dates[:1] + dates[-1:])]
    frames: dict[str, pd.DataFrame] = {}
    if bounds:
        # 连板数依赖完整历史；ensure=True 避免当天行情未同步时静默漏记
        market = loader.load_market_data(
            min(bounds), max(bounds),
            warmup_days=loader.history_warmup_days(min(bounds)),
            ensure=True)
        if not market.empty:
            market["trade_date"] = market["trade_date"].astype(str)
            rank_of = {d: i for i, d in enumerate(loader.open_trade_dates())}
            frames = build_limit_frames(market, rank_of)

    results: dict[str, int] = {}
    for table, watermark, dates in plans:
        if not dates:
            results[table] = 0
            continue
        detail = frames.get(table, pd.DataFrame(columns=schemas.LIMIT_COLUMNS))
        latest = (detail[detail["trade_date"].isin(dates)].copy()
                  if not detail.empty else detail)
        if from_date:
            # 显式指定起始日期视为重算：先清空区间旧行，避免口径变更后残留
            db.execute(f"DELETE FROM `{table}` "
                       "WHERE trade_date>=%s AND trade_date<=%s",
                       (dates[0], dates[-1]))
        written = 0
        if not latest.empty:
            written = db.upsert_df(table, latest,
                                   columns=list(schemas.LIMIT_COLUMNS))
        ingest.set_watermark(table, max(watermark or "", dates[-1]))
        results[table] = written
    return results
