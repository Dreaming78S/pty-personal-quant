"""通用命中回测的筛选逻辑：推荐日 R 盘后按条件选股，R+1 开盘买入、R+1+N 收盘卖出。

三个命中条件（组内 AND）：
- T-1（推荐日 R）：必须包含 t1_strategies，且不同策略数 >= t1_min_count；
- T-2（R 的上一交易日）：必须包含 t2_strategies，且（若给定）不同策略数
  在 [t2_min_count, t2_max_count] 内；
- 累计（R、R-1、R-2）：并集必须包含 cum_strategies，且不同策略数
  在 [cum_min_count, cum_max_count] 内。

市值条件按推荐日 total_mv（万元 → 亿）闭区间过滤，缺市值数据的个股剔除。
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

MV_WAN_PER_YI = 10000.0


def screen_ideas(hits: pd.DataFrame, trade_dates: Iterable[str],
                 start: str, end: str, *,
                 t1_strategies: Iterable[str] = (), t1_min_count: int = 1,
                 t2_strategies: Iterable[str] = (), t2_min_count: int | None = None,
                 t2_max_count: int | None = None,
                 cum_strategies: Iterable[str] = (),
                 cum_min_count: int = 1,
                 cum_max_count: int | None = None) -> pd.DataFrame:
    """按买入思路筛选推荐事件。

    hits 需含 strategy/ts_code/trade_date；trade_dates 为交易日历（YYYYMMDD），
    用于定位 R 的上一/上两个交易日。返回列：ts_code, trade_date（推荐日 R）,
    n_t1, n_t2, n_cum, strategies（R 日命中的策略元组，按名称排序）。
    """
    if t1_min_count < 1:
        raise ValueError(f"t1_min_count 至少为 1（推荐日必须命中），收到 {t1_min_count}")
    if t2_min_count is not None and t2_min_count < 0:
        raise ValueError(f"t2_min_count 不能为负，收到 {t2_min_count}")
    if cum_min_count < 0:
        raise ValueError(f"cum_min_count 不能为负，收到 {cum_min_count}")
    if t2_max_count is not None:
        if t2_max_count < 0:
            raise ValueError(f"t2_max_count 不能为负，收到 {t2_max_count}")
        if t2_min_count is not None and t2_min_count > t2_max_count:
            raise ValueError(f"t2_min_count（{t2_min_count}）"
                             f"不能大于 t2_max_count（{t2_max_count}）")
    if cum_max_count is not None:
        if cum_max_count < 0:
            raise ValueError(f"cum_max_count 不能为负，收到 {cum_max_count}")
        if cum_min_count > cum_max_count:
            raise ValueError(f"cum_min_count（{cum_min_count}）"
                             f"不能大于 cum_max_count（{cum_max_count}）")

    t1_required = set(t1_strategies)
    t2_required = set(t2_strategies)
    cum_required = set(cum_strategies)

    dates = sorted({str(d) for d in trade_dates})
    rank = {d: i for i, d in enumerate(dates)}

    frame = hits[["strategy", "ts_code", "trade_date"]].copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["strategy"] = frame["strategy"].astype(str)
    frame = frame.drop_duplicates()

    by_key = (frame.groupby(["trade_date", "ts_code"])["strategy"]
              .agg(frozenset).to_dict())
    by_date: dict[str, list[tuple[str, frozenset]]] = {}
    for (d, code), strategies in by_key.items():
        by_date.setdefault(d, []).append((code, strategies))
    for rows in by_date.values():
        rows.sort(key=lambda item: item[0])

    def at(date: str | None, code: str) -> frozenset:
        if date is None:
            return frozenset()
        return by_key.get((date, code), frozenset())

    def prev(date: str, back: int) -> str | None:
        i = rank.get(date)
        if i is None or i - back < 0:
            return None
        return dates[i - back]

    rows = []
    for d in dates:
        if d < start or d > end:
            continue
        p1, p2 = prev(d, 1), prev(d, 2)
        for code, on_t1 in by_date.get(d, []):
            n_t1 = len(on_t1)
            if n_t1 < t1_min_count or not t1_required <= on_t1:
                continue
            on_t2 = at(p1, code)
            n_t2 = len(on_t2)
            if not t2_required <= on_t2:
                continue
            if t2_min_count is not None and n_t2 < t2_min_count:
                continue
            if t2_max_count is not None and n_t2 > t2_max_count:
                continue
            cum = on_t1 | on_t2 | at(p2, code)
            if not cum_required <= cum or len(cum) < cum_min_count:
                continue
            if cum_max_count is not None and len(cum) > cum_max_count:
                continue
            rows.append({"ts_code": code, "trade_date": d, "n_t1": n_t1,
                         "n_t2": n_t2, "n_cum": len(cum),
                         "strategies": tuple(sorted(on_t1))})

    out = pd.DataFrame(rows, columns=["ts_code", "trade_date", "n_t1", "n_t2",
                                      "n_cum", "strategies"])
    if out.empty:
        return out
    return out.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def filter_by_market_cap(entries: pd.DataFrame, market: pd.DataFrame,
                         mv_min_yi: float = 0.0,
                         mv_max_yi: float = 50000.0) -> pd.DataFrame:
    """按推荐日总市值（亿，闭区间）过滤，并附加 total_mv_yi 列；缺市值剔除。"""
    if mv_max_yi < mv_min_yi:
        raise ValueError(f"市值上限 {mv_max_yi} 小于下限 {mv_min_yi}")

    mv = market[["ts_code", "trade_date", "total_mv"]].copy()
    mv["trade_date"] = mv["trade_date"].astype(str)
    mv["total_mv_yi"] = (pd.to_numeric(mv["total_mv"], errors="coerce")
                         / MV_WAN_PER_YI)
    out = entries.merge(mv[["ts_code", "trade_date", "total_mv_yi"]],
                        on=["ts_code", "trade_date"], how="left")
    keep = (out["total_mv_yi"].notna()
            & (out["total_mv_yi"] >= mv_min_yi)
            & (out["total_mv_yi"] <= mv_max_yi))
    return out[keep].reset_index(drop=True)
