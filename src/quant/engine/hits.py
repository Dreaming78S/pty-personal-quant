from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import db, ingest, schemas
from quant.engine import loader, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import get_strategy, list_strategies, load_strategy_config

DEFAULT_START = "20240101"
HISTORY_DAYS = 10
_WINDOWS = (3, 5, 10)
DEFAULT_FILTERS = UniverseFilters(allowed_boards=("main",))
STRATEGY_CONFIG_DIR = Path("configs/strategies")


def hit_task_name(strategy: str) -> str:
    return f"hit_{strategy}"


def _strategy_params(name: str) -> dict:
    path = STRATEGY_CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    _, params = load_strategy_config(path)
    return params


def _resolve_strategies(strategy: str) -> list[str]:
    if strategy.strip().lower() in ("", "all"):
        return sorted(list_strategies())
    names = [part.strip() for part in strategy.split(",") if part.strip()]
    known = list_strategies()
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"未注册的策略：{', '.join(unknown)}")
    return names


def _hit_detail(strategy, market: pd.DataFrame) -> pd.DataFrame:
    """面板内全部交易日的命中明细（未按写库区间截取）：主板/非ST/非次新/非停牌。"""
    signals = selection.compute_signals(strategy, market)
    eligible = loader.eligibility_mask(market, DEFAULT_FILTERS)
    base = market.loc[eligible, ["ts_code", "trade_date", "close",
                                 "raw_close", "amount"]]
    candidates = signals[signals["signal"]]
    merged = base.merge(candidates[["ts_code", "trade_date", "score"]],
                        on=["ts_code", "trade_date"], how="inner")
    if merged.empty:
        return merged
    merged["trade_date"] = merged["trade_date"].astype(str)
    merged = merged.sort_values(["trade_date", "score"], ascending=[True, False])
    merged["rank"] = merged.groupby("trade_date").cumcount() + 1
    merged = merged.merge(loader.load_stock_names(), on="ts_code", how="left")
    merged = merged.merge(loader.load_stock_industries(), on="ts_code", how="left")
    return merged


def _attach_history(latest: pd.DataFrame, detail: pd.DataFrame,
                    rank_of: dict[str, int], codes: pd.Index) -> pd.DataFrame:
    """按真实交易日历回看（停牌日占窗口位置，不含当日），注入历史命中列。"""
    n_dates = len(rank_of)
    indicator = np.zeros((n_dates, len(codes)), dtype="int64")
    if not detail.empty:
        rows = detail["trade_date"].map(rank_of).to_numpy()
        cols = pd.Categorical(detail["ts_code"], categories=codes).codes
        indicator[rows, cols] = 1

    prev = np.zeros_like(indicator)
    prev[1:] = indicator[:-1]
    cum = np.cumsum(prev, axis=0)

    def window(n: int) -> np.ndarray:
        out = cum.copy()
        out[n:] = cum[n:] - cum[:-n]
        return out

    idx = np.arange(n_dates)[:, None]
    last_zero = np.maximum.accumulate(np.where(indicator == 0, idx + 1, 0), axis=0) - 1
    streak = idx - last_zero

    rows = latest["trade_date"].map(rank_of).to_numpy()
    cols = pd.Categorical(latest["ts_code"], categories=codes).codes
    latest = latest.copy()
    latest["prev_hit"] = prev[rows, cols]
    latest["hit_3d"] = window(3)[rows, cols]
    latest["hit_5d"] = window(5)[rows, cols]
    latest["hit_10d"] = window(10)[rows, cols]
    latest["streak"] = streak[rows, cols]
    return latest


def fill_hits(strategy: str = "all", from_date: str | None = None,
              to_date: str | None = None) -> dict[str, int]:
    """回填/增量写入 hit_<策略> 表，返回各策略写入行数。

    起始日期取 from_date、已有水位线、DEFAULT_START 三者中最先可用的；
    水位线只增不减，窗口内即使零命中也会推进，避免重复扫描。
    显式指定 from_date 视为重算：先清空区间旧数据再写入。
    """
    strategies = [(name, get_strategy(name, **_strategy_params(name)))
                  for name in _resolve_strategies(strategy)]
    end_cap = to_date or loader.resolve_trade_date()

    plans = []
    for name, strat in strategies:
        watermark = ingest.get_watermark(hit_task_name(name))
        start = from_date or watermark or DEFAULT_START
        dates = (ingest.trade_dates_between(start, end_cap)
                 if start <= end_cap else [])
        plans.append((name, strat, watermark, dates))

    bounds = [d for _, _, _, dates in plans for d in (dates[:1] + dates[-1:])]
    market: pd.DataFrame | None = None
    if bounds:
        max_warmup = max(strat.warmup_days for _, strat in strategies)
        market = loader.load_market_data(
            min(bounds), max(bounds), warmup_days=max_warmup + HISTORY_DAYS)
        market["trade_date"] = market["trade_date"].astype(str)

    rank_of: dict[str, int] = {}
    codes = pd.Index([], dtype=object)
    if market is not None and not market.empty:
        rank_of = {d: i for i, d in
                   enumerate(sorted(market["trade_date"].unique()))}
        codes = pd.Index(sorted(market["ts_code"].unique()))

    results: dict[str, int] = {}
    for name, strat, watermark, dates in plans:
        if not dates:
            results[name] = 0
            continue
        detail = _hit_detail(strat, market)
        latest = (detail[detail["trade_date"].isin(dates)].copy()
                  if not detail.empty else detail)
        if not latest.empty:
            latest["params"] = json.dumps(strat.p.model_dump(),
                                          ensure_ascii=False, sort_keys=True,
                                          default=str)
            latest = _attach_history(latest, detail, rank_of, codes)
            latest = latest[list(schemas.HIT_COLUMNS)]
        written = 0
        if from_date:
            # 显式指定起始日期视为重算：先清空区间内旧命中，避免语义变更后残留过期记录
            db.execute(f"DELETE FROM `{schemas.hit_table_name(name)}` "
                       "WHERE trade_date>=%s AND trade_date<=%s",
                       (dates[0], dates[-1]))
        if not latest.empty:
            written = db.upsert_df(schemas.hit_table_name(name), latest,
                                   columns=list(schemas.HIT_COLUMNS))
        ingest.set_watermark(hit_task_name(name),
                             max(watermark or "", dates[-1]))
        results[name] = written
    return results
