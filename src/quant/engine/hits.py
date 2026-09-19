from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant.data import db, ingest, schemas
from quant.engine import loader, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import get_strategy, list_strategies, load_strategy_config

DEFAULT_START = "20240101"
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


def _hit_frame(strategy, market: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    """当日全部命中（与 select 同口径）：主板/非ST/非次新/非停牌，按 score 降序编号。"""
    signals = selection.compute_signals(strategy, market)
    eligible = loader.eligibility_mask(market, DEFAULT_FILTERS)
    base = market.loc[eligible, ["ts_code", "trade_date", "close",
                                 "raw_close", "amount"]]
    candidates = signals[signals["signal"]]
    merged = base.merge(candidates[["ts_code", "trade_date", "score"]],
                        on=["ts_code", "trade_date"], how="inner")
    merged["trade_date"] = merged["trade_date"].astype(str)
    merged = merged[merged["trade_date"].isin(dates)]
    if merged.empty:
        return merged
    merged = merged.sort_values(["trade_date", "score"], ascending=[True, False])
    merged["rank"] = merged.groupby("trade_date").cumcount() + 1
    merged = merged.merge(loader.load_stock_names(), on="ts_code", how="left")
    merged["params"] = json.dumps(strategy.p.model_dump(), ensure_ascii=False,
                                  sort_keys=True, default=str)
    return merged[list(schemas.HIT_COLUMNS)]


def fill_hits(strategy: str = "all", from_date: str | None = None,
              to_date: str | None = None) -> dict[str, int]:
    """回填/增量写入 hit_<策略> 表，返回各策略写入行数。

    起始日期取 from_date、已有水位线、DEFAULT_START 三者中最先可用的；
    水位线只增不减，窗口内即使零命中也会推进，避免重复扫描。
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
        market = loader.load_market_data(
            min(bounds), max(bounds),
            warmup_days=max(strat.warmup_days for _, strat in strategies))

    results: dict[str, int] = {}
    for name, strat, watermark, dates in plans:
        if not dates:
            results[name] = 0
            continue
        frame = _hit_frame(strat, market, dates)
        written = 0
        if from_date:
            # 显式指定起始日期视为重算：先清空区间内旧命中，避免语义变更后残留过期记录
            db.execute(f"DELETE FROM `{schemas.hit_table_name(name)}` "
                       "WHERE trade_date>=%s AND trade_date<=%s",
                       (dates[0], dates[-1]))
        if not frame.empty:
            written = db.upsert_df(schemas.hit_table_name(name), frame,
                                   columns=list(schemas.HIT_COLUMNS))
        ingest.set_watermark(hit_task_name(name),
                             max(watermark or "", dates[-1]))
        results[name] = written
    return results
