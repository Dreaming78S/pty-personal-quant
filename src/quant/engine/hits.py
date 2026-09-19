from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant.data import db, ingest, schemas
from quant.engine import loader, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import get_strategy, list_strategies, load_strategy_config

DEFAULT_START = "20240101"
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


def _attach_history(latest: pd.DataFrame, keys: set[tuple[str, str]],
                    rank_of: dict[str, int]) -> pd.DataFrame:
    """按真实交易日历回看（停牌日占窗口位置，不含当日），注入历史命中列。

    统计口径是"表内已记录的命中"（keys = 数据库现有行 ∪ 本次写入行），
    因此每一行都能用表自身复核，且与计算面板的长短无关。
    """
    by_code: dict[str, set[int]] = {}
    for code, d in keys:
        by_code.setdefault(code, set()).add(rank_of[d])

    prev_hits, hit3, hit5, hit10, streaks = [], [], [], [], []
    for code, d in zip(latest["ts_code"], latest["trade_date"]):
        r = rank_of[d]
        ranks = by_code[code]
        prev_hits.append(1 if (r - 1) in ranks else 0)
        hit3.append(sum(1 for k in range(r - 3, r) if k in ranks))
        hit5.append(sum(1 for k in range(r - 5, r) if k in ranks))
        hit10.append(sum(1 for k in range(r - 10, r) if k in ranks))
        run = 0
        k = r
        while k in ranks:
            run += 1
            k -= 1
        streaks.append(run)

    latest = latest.copy()
    latest["prev_hit"] = prev_hits
    latest["hit_3d"] = hit3
    latest["hit_5d"] = hit5
    latest["hit_10d"] = hit10
    latest["streak"] = streaks
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
        # 行窗口指标依赖累计行数：一律加载完整历史，保证结果与水位线/面板无关
        market = loader.load_market_data(
            min(bounds), max(bounds),
            warmup_days=loader.history_warmup_days(min(bounds)))
        market["trade_date"] = market["trade_date"].astype(str)

    rank_of = {d: i for i, d in enumerate(loader.open_trade_dates())}

    results: dict[str, int] = {}
    for name, strat, watermark, dates in plans:
        if not dates:
            results[name] = 0
            continue
        detail = _hit_detail(strat, market)
        latest = (detail[detail["trade_date"].isin(dates)].copy()
                  if not detail.empty else detail)
        if not latest.empty:
            table = schemas.hit_table_name(name)
            existing = db.read_df(
                f"SELECT ts_code, trade_date FROM `{table}`")
            keys = set(zip(existing["ts_code"].astype(str),
                           existing["trade_date"].astype(str))) if not existing.empty else set()
            if from_date:
                keys = {k for k in keys if not (dates[0] <= k[1] <= dates[-1])}
            keys |= set(zip(latest["ts_code"], latest["trade_date"]))
            latest["params"] = json.dumps(strat.p.model_dump(),
                                          ensure_ascii=False, sort_keys=True,
                                          default=str)
            latest = _attach_history(latest, keys, rank_of)
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
