from __future__ import annotations

from datetime import date


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
