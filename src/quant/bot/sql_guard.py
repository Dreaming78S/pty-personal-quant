from __future__ import annotations

import re

from quant.bot import schema

MAX_ROWS = 200

BANNED_KEYWORDS = (
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "truncate", "rename", "grant", "revoke", "set", "call", "load",
    "outfile", "dumpfile", "sleep", "benchmark", "get_lock", "into",
)

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_SELECT_START = re.compile(r"(?is)^(select|with)\b")
_BANNED = re.compile(r"\b(?:" + "|".join(BANNED_KEYWORDS) + r")\b", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.I)
_CTE_NAME = re.compile(r"(?i)(?:\bwith\b|,)\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s+as\s*\(")
_LIMIT = re.compile(r"\blimit\s+(\d+)(?:\s+offset\s+\d+)?\s*$", re.I)


class SqlRejected(ValueError):
    """SQL 未通过只读校验。"""


def _strip_comments(sql: str) -> str:
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))


def _referenced_tables(body: str) -> set[str]:
    cte_names = {name.lower() for name in _CTE_NAME.findall(body)}
    return {name.lower() for name in _TABLE_REF.findall(body)} - cte_names


def validate_sql(sql: str, allowed: set[str] | None = None,
                 max_rows: int = MAX_ROWS) -> str:
    """校验只读 SQL 并规范化 LIMIT；不合法时抛 SqlRejected。"""
    body = _strip_comments(sql or "").strip()
    while body.endswith(";"):
        body = body[:-1].strip()
    if not body:
        raise SqlRejected("SQL 为空")
    if ";" in body:
        raise SqlRejected("只允许单条语句")
    if not _SELECT_START.match(body):
        raise SqlRejected("只允许 SELECT / WITH 查询")
    banned = _BANNED.search(body)
    if banned:
        raise SqlRejected(f"不允许的关键字：{banned.group(0).upper()}")
    names = _referenced_tables(body)
    unknown = sorted(names - (allowed or set(schema.allowed_tables())))
    if unknown:
        raise SqlRejected(f"不允许查询的表：{', '.join(unknown)}")

    match = _LIMIT.search(body)
    if match is None:
        return f"{body} LIMIT {max_rows}"
    if int(match.group(1)) > max_rows:
        return f"{body[:match.start()]}LIMIT {max_rows}".strip()
    return body
