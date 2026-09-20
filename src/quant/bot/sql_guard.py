from __future__ import annotations

import re

from quant.bot import schema

MAX_ROWS = 200

BANNED_KEYWORDS = (
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "truncate", "rename", "grant", "revoke", "set", "call", "load",
    "outfile", "dumpfile", "sleep", "benchmark", "get_lock", "into",
)

SYSTEM_SCHEMAS = frozenset({
    "information_schema", "mysql", "performance_schema", "sys",
})

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_SELECT_START = re.compile(r"(?is)^(select|with)\b")
_BANNED = re.compile(r"\b(?:" + "|".join(BANNED_KEYWORDS) + r")\b", re.I)
_LIMIT_COMMA = re.compile(r"(?is)\blimit\s+(\d+)\s*,\s*(\d+)\s*$")
_LIMIT_OFFSET = re.compile(r"(?is)\blimit\s+(\d+)\s+offset\s+(\d+)\s*$")
_LIMIT_PLAIN = re.compile(r"(?is)\blimit\s+(\d+)\s*$")

_TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<line_comment>--[^\n]*)"
    r"|(?P<block_comment>/\*.*?\*/)"
    r"|(?P<btick>`(?:[^`]|``)*`)"
    r"|(?P<dquote>\"(?:\\.|\"\"|[^\"\\])*\")"
    r"|(?P<string>'(?:\\.|''|[^'\\])*')"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$#]*)"
    r"|(?P<punct>[(),.;])"
    r"|(?P<other>.)",
    re.S,
)

_IDENTIFIER_KINDS = frozenset({"word", "btick", "dquote"})

_CLAUSE_BOUNDARIES = frozenset({
    "where", "group", "having", "order", "limit", "union", "except",
    "intersect", "window", "qualify", "procedure", "for", "lock",
    "returning",
})

_JOIN_MODIFIERS = frozenset({
    "left", "right", "inner", "outer", "full", "cross", "natural",
    "straight_join", "lateral", "apply",
})

_SKIPPED_KINDS = frozenset({"space", "line_comment", "block_comment"})


class SqlRejected(ValueError):
    """SQL 未通过只读校验。"""


def _strip_comments(sql: str) -> str:
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))


def _tokenize(body: str) -> list[tuple[str, str, int]]:
    """切分为 (类别, 值, 括号深度) 词法单元；引号与注释内内容不受影响。"""
    tokens: list[tuple[str, str, int]] = []
    depth = 0
    for match in _TOKEN.finditer(body):
        kind = match.lastgroup or "other"
        if kind in _SKIPPED_KINDS:
            continue
        value = match.group(0)
        if kind == "btick":
            value = value[1:-1].replace("``", "`")
        elif kind in ("dquote", "string"):
            value = value[1:-1]
        tokens.append((kind, value, depth))
        if kind == "punct":
            if value == "(":
                depth += 1
            elif value == ")":
                depth = max(depth - 1, 0)
    return tokens


def _skip_balanced(tokens: list[tuple[str, str, int]], start: int) -> int | None:
    """跳过 start 处括号及其配对右括号，返回其后位置；不配平返回 None。"""
    target = tokens[start][2] + 1
    index = start + 1
    while index < len(tokens):
        kind, value, depth = tokens[index]
        if kind == "punct" and value == ")" and depth == target:
            return index + 1
        index += 1
    return None


def _read_qualified(tokens: list[tuple[str, str, int]], start: int
                    ) -> tuple[list[str], int]:
    """读取可能带点号的限定名，返回各段与结束位置。"""
    kind, value, _ = tokens[start]
    if kind not in _IDENTIFIER_KINDS:
        return [], start
    segments = [value]
    index = start + 1
    while (index + 1 < len(tokens)
           and tokens[index][0] == "punct" and tokens[index][1] == "."
           and tokens[index + 1][0] in _IDENTIFIER_KINDS):
        segments.append(tokens[index + 1][1])
        index += 2
    return segments, index


def _scan_from_clause(tokens: list[tuple[str, str, int]], start: int
                      ) -> list[tuple[str, ...]]:
    """扫描一个 FROM/JOIN 起的表引用列表，直到底层子句边界。"""
    refs: list[tuple[str, ...]] = []
    count = len(tokens)
    if start >= count:
        return refs
    clause_depth = tokens[start - 1][2] if start > 0 else tokens[start][2]
    index = start
    expect_table = True
    while index < count:
        kind, value, depth = tokens[index]
        if kind == "punct" and value == ")":
            if depth == clause_depth:
                break
            index += 1
            continue
        if kind == "punct" and value == "(":
            following = _skip_balanced(tokens, index)
            if following is None:
                break
            index = following
            expect_table = False
            continue
        if kind == "punct" and value == ",":
            expect_table = True
            index += 1
            continue
        if kind == "punct" and value == ";":
            break
        if kind == "word":
            keyword = value.lower()
            if keyword in _CLAUSE_BOUNDARIES:
                break
            if keyword == "join":
                expect_table = True
                index += 1
                continue
            if keyword in _JOIN_MODIFIERS:
                index += 1
                continue
            if expect_table:
                segments, index = _read_qualified(tokens, index)
                if segments:
                    refs.append(tuple(segments))
                    expect_table = False
                    continue
            index += 1
            continue
        if kind in _IDENTIFIER_KINDS and expect_table:
            segments, index = _read_qualified(tokens, index)
            if segments:
                refs.append(tuple(segments))
                expect_table = False
                continue
        index += 1
    return refs


def _cte_names(tokens: list[tuple[str, str, int]]) -> set[str]:
    """仅从语句最前面的 WITH 子句收集 CTE 名，要求定义体括号配平。"""
    names: set[str] = set()
    count = len(tokens)
    if count == 0 or tokens[0][0] != "word" or tokens[0][1].lower() != "with":
        return names
    index = 1
    if (index < count and tokens[index][0] == "word"
            and tokens[index][1].lower() == "recursive"):
        index += 1
    while index < count:
        kind, value, _ = tokens[index]
        if kind not in _IDENTIFIER_KINDS:
            break
        name = value.lower()
        index += 1
        if index < count and tokens[index][0] == "punct" and tokens[index][1] == "(":
            following = _skip_balanced(tokens, index)
            if following is None:
                break
            index = following
        if not (index < count and tokens[index][0] == "word"
                and tokens[index][1].lower() == "as"):
            break
        index += 1
        if not (index < count and tokens[index][0] == "punct"
                and tokens[index][1] == "("):
            break
        following = _skip_balanced(tokens, index)
        if following is None:
            break
        names.add(name)
        index = following
        if index < count and tokens[index][0] == "punct" and tokens[index][1] == ",":
            index += 1
            continue
        break
    return names


def _analyze(body: str) -> tuple[set[str], list[tuple[str, ...]]]:
    tokens = _tokenize(body)
    cte_names = _cte_names(tokens)
    refs: list[tuple[str, ...]] = []
    for index, (kind, value, _) in enumerate(tokens):
        if kind == "word" and value.lower() in ("from", "join"):
            refs.extend(_scan_from_clause(tokens, index + 1))
    return cte_names, refs


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

    cte_names, refs = _analyze(body)
    system = sorted({segment.lower() for segments in refs
                     for segment in segments
                     if segment.lower() in SYSTEM_SCHEMAS})
    if system:
        raise SqlRejected(f"不允许查询的表：{', '.join(system)}")
    names = {segments[-1].lower() for segments in refs if segments} - cte_names
    whitelist = set(allowed) if allowed else set(schema.allowed_tables())
    unknown = sorted(names - whitelist)
    if unknown:
        raise SqlRejected(f"不允许查询的表：{', '.join(unknown)}")

    match = _LIMIT_COMMA.search(body)
    if match is not None:
        if int(match.group(2)) > max_rows:
            return f"{body[:match.start()].rstrip()} LIMIT {match.group(1)}, {max_rows}"
        return body
    match = _LIMIT_OFFSET.search(body)
    if match is not None:
        if int(match.group(1)) > max_rows:
            return f"{body[:match.start()].rstrip()} LIMIT {max_rows} OFFSET {match.group(2)}"
        return body
    match = _LIMIT_PLAIN.search(body)
    if match is not None:
        if int(match.group(1)) > max_rows:
            return f"{body[:match.start()].rstrip()} LIMIT {max_rows}"
        return body
    return f"{body} LIMIT {max_rows}"
