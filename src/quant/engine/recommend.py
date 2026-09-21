"""每日最终推荐：环境闸门 + 共振/排名分层，从既有 hit 表只读推导。

规则（configs/recommend.yaml 可调）：
- 环境闸门：信号日 gate.index 收盘 >= gate.ma_days 日均线才出手，关闸整日不推荐；
- 重点档：命中 primary_strategies 之一，或当日 >= primary_min_co 个策略共振；
- 备选档：>= secondary_min_co 个策略共振，且至少一个策略内 rank <= secondary_max_rank；
- max_picks：重点+备选总上限（先保重点档）。

口径来源：2024~2026.9 样本上 T+1 开盘买入 / T+2 收盘卖出的跨年度验证
（涨停洗盘+闸门、共振>=2&rank<=5+闸门 为连续三年扣成本后仍为正的口径）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd
import yaml

from quant.data import db, ingest, schemas
from quant.engine import loader

DEFAULT_CONFIG_PATH = Path("configs/recommend.yaml")

STRATEGY_NAMES_ZH = {
    "ma_volume": "均线放量",
    "turtle_trade": "海龟交易",
    "high_tight_flag": "高位窄幅整理",
    "limit_up_shakeout": "涨停洗盘",
    "uptrend_limit_down": "上涨趋势跌停",
    "rps_breakout": "RPS突破",
    "rise_shrink_pullback": "上涨缩量回调",
}

TIER_PRIMARY = "重点"
TIER_SECONDARY = "备选"

INDEX_NAMES_ZH = {
    "000300.SH": "沪深300",
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
    "899050.BJ": "北证50",
}


def strategy_label(name: str) -> str:
    return STRATEGY_NAMES_ZH.get(name, name)


@dataclass(frozen=True)
class RecommendConfig:
    gate_index: str = "000300.SH"
    gate_ma_days: int = 20
    primary_strategies: tuple[str, ...] = ("limit_up_shakeout",)
    primary_min_co: int = 3
    secondary_min_co: int = 2
    secondary_max_rank: int = 5
    max_picks: int = 10


@dataclass(frozen=True)
class Pick:
    ts_code: str
    name: str
    industry: str
    raw_close: float | None
    amount: float | None
    tier: str
    co_count: int
    hits: tuple[tuple[str, int], ...]  # (策略中文名, 当日该策略内 rank)，按 rank 升序


@dataclass(frozen=True)
class RecommendResult:
    date: str
    gate_open: bool
    index_close: float | None
    index_ma: float | None
    primary: tuple[Pick, ...]
    secondary: tuple[Pick, ...]

    @property
    def total(self) -> int:
        return len(self.primary) + len(self.secondary)


def _pick(mapping: dict, key: str, default):
    value = mapping.get(key)
    return default if value is None else value


def load_config(path: str | Path | None = None) -> RecommendConfig:
    """读取推荐规则配置；文件缺失或字段缺省时用代码默认值。"""
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gate = raw.get("gate") or {}
    tiers = raw.get("tiers") or {}
    defaults = RecommendConfig()
    primary = _pick(tiers, "primary_strategies", defaults.primary_strategies)
    return RecommendConfig(
        gate_index=str(_pick(gate, "index", defaults.gate_index)),
        gate_ma_days=int(_pick(gate, "ma_days", defaults.gate_ma_days)),
        primary_strategies=tuple(primary),
        primary_min_co=int(_pick(tiers, "primary_min_co",
                                 defaults.primary_min_co)),
        secondary_min_co=int(_pick(tiers, "secondary_min_co",
                                   defaults.secondary_min_co)),
        secondary_max_rank=int(_pick(tiers, "secondary_max_rank",
                                     defaults.secondary_max_rank)),
        max_picks=int(_pick(raw, "max_picks", defaults.max_picks)),
    )


def market_gate(date: str, config: RecommendConfig | None = None
                ) -> tuple[bool, float, float]:
    """环境闸门：指数收盘 >= ma_days 日均线返回 True。

    指数数据未更新到目标日或历史不足时抛 ValueError（宁可不推也不算错闸门）。
    """
    config = config or RecommendConfig()
    series = loader.load_benchmark(config.gate_index, "19900101", date)
    series = series.sort_index().dropna()
    if series.empty or series.index[-1] != date:
        raise ValueError(
            f"index_daily 未更新到 {date}（{config.gate_index}），"
            "请先执行 quant data update -t index_daily")
    window = series.iloc[-config.gate_ma_days:]
    if len(window) < config.gate_ma_days:
        raise ValueError(
            f"{config.gate_index} 历史不足 {config.gate_ma_days} 个交易日，"
            "无法计算环境闸门均线")
    ma = float(window.mean())
    close = float(series.iloc[-1])
    return close >= ma, close, ma


def _check_watermarks(target: str) -> None:
    for name in schemas.HIT_STRATEGIES:
        table = schemas.hit_table_name(name)
        watermark = ingest.get_watermark(table) or ""
        if watermark < target:
            raise ValueError(
                f"{table} 命中表未更新到 {target}（水位线 {watermark or '-'}），"
                f"请先执行 quant hits update -s {name}")


def _num(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _sort_key(pick: Pick) -> tuple[int, float]:
    amount = pick.amount if pick.amount is not None else float("-inf")
    return (-pick.co_count, -amount)


def _classify(hits: pd.DataFrame,
              config: RecommendConfig) -> tuple[list[Pick], list[Pick]]:
    """把当日全部命中行按档位规则归类：返回（重点, 备选）列表。"""
    frame = hits.copy()
    for column in ("rank", "raw_close", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    primary: list[Pick] = []
    secondary: list[Pick] = []
    primary_set = set(config.primary_strategies)
    for ts_code, group in frame.groupby("ts_code", sort=False):
        strategies = sorted(zip(group["strategy"].astype(str),
                                group["rank"]),
                            key=lambda item: (item[1] if pd.notna(item[1])
                                              else float("inf")))
        labels = tuple((strategy_label(name), int(rank))
                       for name, rank in strategies if pd.notna(rank))
        ranks = [rank for _, rank in labels]
        co_count = len(strategies)
        first = group.iloc[0]
        pick = Pick(
            ts_code=str(ts_code),
            name=("" if pd.isna(first["name"]) else str(first["name"])),
            industry=("" if pd.isna(first["industry"])
                      else str(first["industry"])),
            raw_close=_num(first["raw_close"]),
            amount=_num(first["amount"]),
            tier=TIER_PRIMARY,
            co_count=co_count,
            hits=labels,
        )
        is_primary = (co_count >= config.primary_min_co
                      or any(name in primary_set for name, _ in strategies))
        if is_primary:
            primary.append(pick)
        elif (co_count >= config.secondary_min_co
              and ranks and min(ranks) <= config.secondary_max_rank):
            secondary.append(replace(pick, tier=TIER_SECONDARY))
    primary.sort(key=_sort_key)
    secondary.sort(key=_sort_key)
    return primary, secondary


def build_recommendations(date: str | None = None,
                          config: RecommendConfig | None = None
                          ) -> RecommendResult:
    """生成某交易日的最终推荐（只读 hit 表与指数缓存，不写任何数据）。"""
    config = config or load_config()
    target = loader.resolve_trade_date(date)
    _check_watermarks(target)
    gate_open, index_close, index_ma = market_gate(target, config)

    primary: list[Pick] = []
    secondary: list[Pick] = []
    if gate_open:
        frames = []
        for name in schemas.HIT_STRATEGIES:
            table = schemas.hit_table_name(name)
            rows = db.read_df(
                f"SELECT ts_code, `rank`, name, industry, raw_close, amount "
                f"FROM `{table}` WHERE trade_date=%s", (target,))
            if rows.empty:
                continue
            rows = rows.copy()
            rows["strategy"] = name
            frames.append(rows)
        if frames:
            primary, secondary = _classify(pd.concat(frames, ignore_index=True),
                                           config)

    cap = config.max_picks
    if cap and len(primary) + len(secondary) > cap:
        primary = primary[:cap]
        secondary = secondary[:max(cap - len(primary), 0)]
    return RecommendResult(
        date=target,
        gate_open=gate_open,
        index_close=index_close,
        index_ma=index_ma,
        primary=tuple(primary),
        secondary=tuple(secondary),
    )
