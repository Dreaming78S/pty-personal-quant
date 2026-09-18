from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

import pandas as pd
import yaml
from pydantic import BaseModel


class EmptyParams(BaseModel):
    pass


StrategyRegistry: dict[str, type["Strategy"]] = {}


def register_strategy(name: str):
    def decorator(cls: type["Strategy"]) -> type["Strategy"]:
        if name in StrategyRegistry:
            raise ValueError(f"策略名重复注册：{name}")
        cls.name = name
        StrategyRegistry[name] = cls
        return cls
    return decorator


def list_strategies() -> dict[str, type["Strategy"]]:
    return dict(StrategyRegistry)


def get_strategy(name: str, **params) -> "Strategy":
    if name not in StrategyRegistry:
        raise KeyError(f"未注册的策略：{name}")
    return StrategyRegistry[name](**params)


def load_strategy_config(path: str | Path) -> tuple[str, dict]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not raw or "strategy" not in raw:
        raise ValueError(f"策略配置缺少 strategy 字段：{path}")
    return raw["strategy"], raw.get("params", {}) or {}


class Strategy(ABC):
    """策略基类：输入单只股票行情（按日期升序、后复权），输出逐日布尔信号。"""

    name: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel]] = EmptyParams
    warmup_days: ClassVar[int] = 0

    def __init__(self, **params):
        self.p = self.Params(**params)

    @abstractmethod
    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        """bars 列至少含 trade_date/open/high/low/close/vol/amount；返回等长 bool Series。

        因果约定：引擎按股票传入整段已加载历史（包含信号日之后的日期），
        策略必须只使用每行 trade_date 及之前的数据，禁止负向 shift、
        全序列归一化、反向窗口等任何引用未来行情的写法。
        """

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        """信号数超过持仓数时的排序分，默认 None（由引擎按 rank_by 排序）。"""
        return None
