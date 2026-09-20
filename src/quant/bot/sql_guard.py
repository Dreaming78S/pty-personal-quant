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
_CTE_NAME = re.compile(r"(?i)(?:\bwith\b|,)\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s+as\s*\(")
_LIMIT = re.compile(r"\blimit\s+(\d+)(?:\s+offset\s+\d+)?\s*$", re.I)
_FROM_JOIN = re.compile(r"\b(?:from|join)\b", re.I)
_IDENTIFIER = re.compile(r"`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*)")

_NOT_ALIAS = frozenset({
    "where", "group", "having", "order", "limit", "union", "except",
    "intersect", "window", "qualify", "on", "using", "join", "inner",
    "left", "right", "full", "cross", "natural", "outer", "select",
    "from", "as", "for", "offset", "fetch",
})


class SqlRejected(ValueError):
    """SQL 未通过只读校验。"""


def _strip_comments(sql: str) -> str:
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))


def _read_identifier(text: str, pos: int) -> tuple[str, int] | None:
    match = _IDENTIFIER.match(text, pos)
    if match is None:
        return None
    return match.group(1) or match.group(2), match.end()


def _read_qualified(text: str, pos: int) -> tuple[str, int] | None:
    match = _read_identifier(text, pos)
    if match is None:
        return None
    name, pos = match
    while pos < len(text) and text[pos] == ".":
        following = _read_identifier(text, pos + 1)
        if following is None:
            break
        pos = following[1]
    return name, pos


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _skip_parentheses(text: str, pos: int) -> int:
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return pos


def _scan_table_list(text: str, pos: int, allow_comma: bool) -> set[str]:
    names: set[str] = set()
    length = len(text)
    while True:
        pos = _skip_space(text, pos)
        if pos < length and text[pos] == "(":
            pos = _skip_parentheses(text, pos)
        else:
            table = _read_qualified(text, pos)
            if table is None:
                break
            names.add(table[0].lower())
            pos = table[1]
        pos = _skip_space(text, pos)
        alias = _read_qualified(text, pos)
        if alias is not None:
            if alias[0].lower() == "as":
                pos = _skip_space(text, alias[1])
                following = _read_qualified(text, pos)
                if following is not None:
                    pos = following[1]
            elif alias[0].lower() not in _NOT_ALIAS:
                pos = alias[1]
        pos = _skip_space(text, pos)
        if allow_comma and pos < length and text[pos] == ",":
            pos += 1
            continue
        break
    return names


def _referenced_tables(body: str) -> set[str]:
    cte_names = {name.lower() for name in _CTE_NAME.findall(body)}
    names: set[str] = set()
    for anchor in _FROM_JOIN.finditer(body):
        names |= _scan_table_list(
            body, anchor.end(), allow_comma=anchor.group(0).lower() == "from")
    return names - cte_names


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
