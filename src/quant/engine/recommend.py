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
    gate_indices: tuple[str, ...] = ("000300.SH",)
    gate_mode: str = "all"
    primary_strategies: tuple[str, ...] = ("limit_up_shakeout",)
    primary_min_co: int = 3
    secondary_min_co: int = 2
    secondary_max_rank: int = 5
    max_picks: int = 10


@dataclass(frozen=True)
class IndexStatus:
    ts_code: str
    name: str
    close: float
    ma: float
    above: bool


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
    index_statuses: tuple[IndexStatus, ...] = ()

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

    gate_index = str(_pick(gate, "index", defaults.gate_index))
    indices = _pick(gate, "indices", None)
    if indices is not None:
        gate_indices = tuple(str(c) for c in indices)
    else:
        gate_indices = (gate_index,)
    mode = str(_pick(gate, "mode", defaults.gate_mode))
    if mode not in {"all", "majority", "any", "hs300_and_any"}:
        mode = defaults.gate_mode

    return RecommendConfig(
        gate_index=gate_index,
        gate_ma_days=int(_pick(gate, "ma_days", defaults.gate_ma_days)),
        gate_indices=gate_indices,
        gate_mode=mode,
        primary_strategies=tuple(primary),
        primary_min_co=int(_pick(tiers, "primary_min_co",
                                 defaults.primary_min_co)),
        secondary_min_co=int(_pick(tiers, "secondary_min_co",
                                    defaults.secondary_min_co)),
        secondary_max_rank=int(_pick(tiers, "secondary_max_rank",
                                      defaults.secondary_max_rank)),
        max_picks=int(_pick(raw, "max_picks", defaults.max_picks)),
    )



def _index_status(code: str, date: str, ma_days: int) -> IndexStatus:
    """计算单个指数的状态；数据缺失或历史不足时抛 ValueError。"""
    series = loader.load_benchmark(code, "19900101", date)
    series = series.sort_index().dropna()
    if series.empty or series.index[-1] != date:
        raise ValueError(
            f"index_daily 未更新到 {date}（{code}），"
            "请先执行 quant data update -t index_daily")
    window = series.iloc[-ma_days:]
    if len(window) < ma_days:
        raise ValueError(
            f"{code} 历史不足 {ma_days} 个交易日，无法计算环境闸门均线")
    ma = float(window.mean())
    close = float(series.iloc[-1])
    return IndexStatus(
        ts_code=code,
        name=INDEX_NAMES_ZH.get(code, code),
        close=close,
        ma=ma,
        above=close >= ma,
    )


def market_gates(date: str, config: RecommendConfig | None = None
                 ) -> tuple[tuple[IndexStatus, ...], bool]:
    """计算配置的全部指数状态，并按 mode 返回综合闸门是否打开。"""
    config = config or RecommendConfig()
    statuses = tuple(_index_status(code, date, config.gate_ma_days)
                     for code in config.gate_indices)
    above = pd.Series([s.above for s in statuses],
                      index=[s.ts_code for s in statuses])
    n = len(above)
    mode = config.gate_mode
    if mode == "all":
        gate_open = bool(above.all())
    elif mode == "majority":
        gate_open = int(above.sum()) > n / 2
    elif mode == "any":
        gate_open = bool(above.any())
    elif mode == "hs300_and_any":
        hs = above.get("000300.SH", False)
        others = above.drop("000300.SH", errors="ignore")
        gate_open = bool(hs) and bool(others.any())
    else:
        gate_open = bool(above.all())
    return statuses, gate_open


def market_gate(date: str, config: RecommendConfig | None = None
                ) -> tuple[bool, float, float]:
    """兼容旧接口：返回 gate_index（缺省沪深300）的状态。"""
    config = config or RecommendConfig()
    statuses, _ = market_gates(date, config)
    for status in statuses:
        if status.ts_code == config.gate_index:
            return status.above, status.close, status.ma
    if not statuses:
        raise ValueError(f"没有配置任何环境闸门指数")
    return statuses[0].above, statuses[0].close, statuses[0].ma


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
                          config: RecommendConfig | None = None,
                          ignore_gate: bool = False
                          ) -> RecommendResult:
    """生成某交易日的最终推荐（只读 hit 表与指数缓存，不写任何数据）。

    ignore_gate=True 时仍按规则分层出票，但 gate_open 仍反映真实综合闸门状态，
    用于需要“假装全开也列出股票”的场景（如盘后观察清单）。
    """
    config = config or load_config()
    target = loader.resolve_trade_date(date)
    _check_watermarks(target)
    index_statuses, gate_open = market_gates(target, config)

    primary: list[Pick] = []
    secondary: list[Pick] = []
    if gate_open or ignore_gate:
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

    ref = next((s for s in index_statuses
                if s.ts_code == config.gate_index), None)
    if ref is None and index_statuses:
        ref = index_statuses[0]
    index_close = ref.close if ref is not None else None
    index_ma = ref.ma if ref is not None else None

    return RecommendResult(
        date=target,
        gate_open=gate_open,
        index_close=index_close,
        index_ma=index_ma,
        primary=tuple(primary),
        secondary=tuple(secondary),
        index_statuses=index_statuses,
    )
