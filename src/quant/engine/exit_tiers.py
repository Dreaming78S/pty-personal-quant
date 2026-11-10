"""动态止盈分段：T+1 收盘对买入价的涨幅 → T+2 止盈率的映射。

背景（limit_up_shakeout 二次筛选离场规则）：
- T 日为信号日，T+1 日买入；买入价 open(T+1)（开盘价买入，即 delta=0 口径；
  调用方也可传入限价买入价作为基准，见 tmp_scripts 里的 --delta）；
- 分档涨幅 = close(T+1) / 买入价 - 1（delta=0 时即日内涨幅 close/open-1）；
- 按该涨幅落入的分段确定 T+2 的止盈率（可为负，即止损离场）；
- T+2 执行方式与固定止盈完全一致，只是阈值逐样本不同：
  1. T+2 开盘价 > 买入价 × (1 + 止盈率)：按 T+2 开盘价卖出（开盘已高于目标价）；
  2. 否则 T+2 最高价 >= 买入价 × (1 + 止盈率)：按该目标价卖出；
  3. 否则持有到 T+2 收盘卖出（收盘跌停顺延等无法卖出的情形由调用方处理）。

分段为左开右闭区间 (lo, hi]：首段 lo 为 None 表示 -∞，末段 hi 为 None 表示
+∞，相邻段共享边界（无缝无叠）。DEFAULT_EXIT_TIERS 用一个变量概括整个分段
函数；CLI 可用 JSON 覆盖（parse_exit_tiers），固定阈值可退化为单段
（tiers_from_scalar）。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

# 每段：(下界, 上界, 止盈率)；区间左开右闭 (下界, 上界]，None 表示 ±∞
DEFAULT_EXIT_TIERS: tuple[tuple[float | None, float | None, float], ...] = (
    (None, -0.05, 0.05),
    (-0.05, 0.00, 0.01),
    (0.00, 0.05, 0.02),
    (0.05, None, 0.04),
)


def validate_exit_tiers(tiers) -> None:
    """校验分档：非空、首段下界 -∞、末段上界 +∞、相邻段共享边界、止盈率 > -1。"""
    tiers = tuple(tiers)
    if not tiers:
        raise ValueError("止盈分档不能为空")
    for i, (lo, hi, rate) in enumerate(tiers):
        first, last = i == 0, i == len(tiers) - 1
        if first and lo is not None:
            raise ValueError("首段下界必须为 None（-∞）")
        if last and hi is not None:
            raise ValueError("末段上界必须为 None（+∞）")
        if not first and lo is None:
            raise ValueError(f"仅首段下界可为 None；第 {i + 1} 段下界为 None")
        if not last and hi is None:
            raise ValueError(f"仅末段上界可为 None；第 {i + 1} 段上界为 None")
        if lo is not None and hi is not None and lo >= hi:
            raise ValueError(f"第 {i + 1} 段下界必须小于上界（当前 {lo} >= {hi}）")
        if rate is None or not math.isfinite(float(rate)):
            raise ValueError(f"第 {i + 1} 段止盈率必须是有限数字")
        if float(rate) <= -1.0:
            raise ValueError(f"第 {i + 1} 段止盈率必须 > -1（保证卖出价为正）")
        if not first:
            prev_hi = tiers[i - 1][1]
            if prev_hi != lo:
                raise ValueError(
                    f"第 {i + 1} 段下界必须等于第 {i} 段上界（当前 {lo} != {prev_hi}），"
                    "分段必须无缝无叠"
                )


def normalize_exit_tiers(raw) -> tuple[tuple[float | None, float | None, float], ...]:
    """把 [[下界, 上界, 止盈率], ...] 形式的嵌套列表规整为元组并校验。

    下界/上界为 None（JSON 的 null）表示 -∞/+∞。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("止盈分档必须是非空列表：[[下界, 上界, 止盈率], ...]")
    tiers: list[tuple[float | None, float | None, float]] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise ValueError(f"第 {i} 段必须是 [下界, 上界, 止盈率] 三个值")
        lo, hi, rate = item
        for name, bound in (("下界", lo), ("上界", hi)):
            if bound is None:
                continue
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                raise ValueError(f"第 {i} 段{name}必须是数字或 null")
            if not math.isfinite(float(bound)):
                raise ValueError(f"第 {i} 段{name}必须有限，无穷请用 null")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValueError(f"第 {i} 段止盈率必须是数字")
        if not math.isfinite(float(rate)):
            raise ValueError(f"第 {i} 段止盈率必须有限")
        tiers.append(
            (
                None if lo is None else float(lo),
                None if hi is None else float(hi),
                float(rate),
            )
        )
    result = tuple(tiers)
    validate_exit_tiers(result)
    return result


def parse_exit_tiers(text: str) -> tuple[tuple[float | None, float | None, float], ...]:
    """解析 CLI 传入的 JSON 分档：[[下界, 上界, 止盈率], ...]，null 表示 ±∞。"""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"止盈分档不是合法 JSON：{exc}") from exc
    return normalize_exit_tiers(raw)


def tiers_from_scalar(rate: float) -> tuple[tuple[None, None, float], ...]:
    """固定止盈阈值退化为单段分档（覆盖全部 T+1 日内涨幅）。"""
    rate = float(rate)
    if not math.isfinite(rate):
        raise ValueError("固定止盈阈值必须是有限数字")
    if rate <= -1.0:
        raise ValueError("固定止盈阈值必须 > -1（保证卖出价为正）")
    return ((None, None, rate),)


def _fmt_bound(value: float | None, *, lower: bool) -> str:
    if value is None:
        return "-∞" if lower else "+∞"
    return f"{value * 100:g}%"


def tier_labels(tiers) -> tuple[str, ...]:
    """每段的人读标签，如 (-∞, -5%]、(-5%, 0%]、(7%, +∞)。"""
    labels = []
    for lo, hi, _rate in tiers:
        closer = "]" if hi is not None else ")"
        labels.append(
            f"({_fmt_bound(lo, lower=True)}, {_fmt_bound(hi, lower=False)}{closer}"
        )
    return tuple(labels)


def format_tiers_summary(tiers) -> str:
    """一行概括分档，供控制台与报告使用，如 `(-∞,-5%]→-2%；(-5%,0%]→+1%`。"""
    parts = [
        f"{label.replace(' ', '')}→{rate * 100:+g}%"
        for (_lo, _hi, rate), label in zip(tiers, tier_labels(tiers))
    ]
    return "；".join(parts)


def tier_index(day1_ret: float, tiers) -> int | None:
    """返回 T+1 日内涨幅落入的段下标（0 起）；缺失/非有限值返回 None。"""
    if day1_ret is None:
        return None
    try:
        value = float(day1_ret)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    for i, (lo, hi, _rate) in enumerate(tiers):
        if (lo is None or value > lo) and (hi is None or value <= hi):
            return i
    return None  # 分档已校验覆盖全区间，正常不会到这


def tier_rate(day1_ret: float, tiers) -> float | None:
    """T+1 日内涨幅对应的止盈率；缺失/非有限值返回 None。"""
    idx = tier_index(day1_ret, tiers)
    return None if idx is None else float(tiers[idx][2])


def _tier_masks(day1_ret: pd.Series, tiers) -> list[pd.Series]:
    values = pd.to_numeric(day1_ret, errors="coerce")
    valid = values.notna() & np.isfinite(values)
    masks = []
    for lo, hi, _rate in tiers:
        mask = valid
        if lo is not None:
            mask = mask & values.gt(lo)
        if hi is not None:
            mask = mask & values.le(hi)
        masks.append(mask)
    return masks


def apply_exit_thresholds(day1_ret: pd.Series, tiers) -> pd.Series:
    """按分档把逐样本 T+1 日内涨幅映射为止盈率；缺失/非有限值为 NaN。"""
    threshold = pd.Series(np.nan, index=day1_ret.index, dtype="float64")
    for (lo, hi, rate), mask in zip(tiers, _tier_masks(day1_ret, tiers)):
        threshold[mask] = rate
    return threshold


def apply_exit_tier_codes(day1_ret: pd.Series, tiers) -> pd.Series:
    """按分档返回逐样本段下标（int64），缺失/非有限值为 -1。"""
    codes = pd.Series(-1, index=day1_ret.index, dtype="int64")
    for i, mask in enumerate(_tier_masks(day1_ret, tiers)):
        codes[mask] = i
    return codes


def tiers_to_jsonable(tiers) -> list[list[float | None]]:
    """转为可写进 run_meta.json 的嵌套列表（None 即 null）。"""
    return [[lo, hi, rate] for lo, hi, rate in tiers]
