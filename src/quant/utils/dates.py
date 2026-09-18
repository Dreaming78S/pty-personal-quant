from __future__ import annotations

from datetime import date, datetime, timedelta


def to_yyyymmdd(value: str | date) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text.replace("-", "")
    if len(text) == 8 and text.isdigit():
        return text
    raise ValueError(f"无法识别的日期格式：{value!r}（应为 YYYY-MM-DD 或 YYYYMMDD）")


def to_iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def calendar_days_between(start: str, end: str) -> list[str]:
    """返回 [start, end] 内每个自然日的 YYYYMMDD（含端点）；倒序或非法输入返回 []。"""
    try:
        start_day = datetime.strptime(start, "%Y%m%d").date()
        end_day = datetime.strptime(end, "%Y%m%d").date()
    except (TypeError, ValueError):
        return []
    if start_day > end_day:
        return []
    span = (end_day - start_day).days
    return [(start_day + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(span + 1)]
