from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from quant.bot import schema
from quant.bot.llm import LlmError
from quant.bot.sql_guard import SqlRejected, validate_sql

MAX_ROWS = 200
MAX_RESULT_CHARS = 8000

SQL_SYSTEM_PROMPT = """你是 A 股量化数据库的 SQL 助手。根据用户问题写一条只读 SELECT 查询。

规则：
- 只使用下面给出的表，只输出一条 SELECT/WITH 语句，不要分号，不要写操作与 DDL。
- 日期列是 CHAR(8) 字符串，格式 YYYYMMDD；"最近几天"用 trade_date >= 'YYYYMMDD' 之类条件。
- 最新交易日：SELECT MAX(cal_date) FROM trade_cal WHERE is_open = 1 AND cal_date <= '今天'。
- 股票用 ts_code 或 name 匹配；不确定代码时用 stock_basic.name LIKE '%关键词%'。
- 口径：daily.amount 为千元且不复权；hit_* 表 close 为后复权、raw_close 为不复权，
  name/industry 为入库快照；命中表水位线在 ingest_log（task_name = 'hit_<策略>'）。
- 只输出 JSON：{"sql": "...", "explain": "一句话说明"}"""

SUMMARY_SYSTEM_PROMPT = """你是 A 股量化助手。根据查询结果用简洁中文回答用户问题。

要求：先给结论，再列关键数据（日期、策略、名称、数值）；不要编造结果里没有的数据；
结果为空就直说没有查到；不要输出与问题无关的推测。
排版：可用「- 」无序列表分行；不要使用反引号（行内代码）、表格、标题等飞书卡片不渲染的语法，
策略名与字段名直接写出来即可。"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass(frozen=True)
class Answer:
    text: str
    sql: str = ""
    row_count: int = 0
    ok: bool = True
    note: str = ""


def parse_sql_json(text: str) -> str:
    """从模型输出里取出 SQL（容忍 ``` 围栏与前后说明文字）。"""
    candidate = (text or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    payload = None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise SqlRejected("模型没有返回 JSON") from None
        try:
            payload = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc:
            raise SqlRejected("模型返回的 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise SqlRejected("模型返回的 JSON 不是对象")
    sql = str(payload.get("sql") or "").strip()
    if not sql:
        raise SqlRejected("模型没有给出 SQL")
    return sql


def render_rows(rows: pd.DataFrame, max_rows: int = MAX_ROWS,
                max_chars: int = MAX_RESULT_CHARS) -> str:
    """把结果渲染成紧凑文本（列名 + 制表符分隔行），超限截断并注明。"""
    if rows.empty:
        return "（无数据行）"
    frame = rows.head(max_rows)
    lines = ["\t".join(str(column) for column in frame.columns)]
    for _, row in frame.iterrows():
        lines.append("\t".join("" if pd.isna(value) else str(value)
                               for value in row))
    text = "\n".join(lines)
    if len(text) > max_chars:
        return f"{text[:max_chars]}\n…（结果过长已截断）"
    if len(rows) > max_rows:
        return f"{text}\n…（仅显示前 {max_rows} 行，共 {len(rows)} 行）"
    return text


def build_sql_messages(question: str, schema_text: str) -> list[dict]:
    return [{"role": "system",
             "content": f"{SQL_SYSTEM_PROMPT}\n\n可用表：\n{schema_text}"},
            {"role": "user", "content": question}]


def build_summary_messages(question: str, sql: str, rows_text: str) -> list[dict]:
    return [{"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user",
             "content": f"用户问题：{question}\n\nSQL：{sql}\n\n"
                        f"查询结果（制表符分隔）：\n{rows_text}"}]


def answer(question: str, llm, runner: Callable[[str], pd.DataFrame],
           schema_text: str | None = None) -> Answer:
    """生成 SQL → 只读校验 → 执行 → 总结；任何一步失败都返回带原因的 Answer。"""
    schema_text = schema_text or schema.schema_prompt()
    try:
        raw = llm.complete(build_sql_messages(question, schema_text))
    except LlmError as exc:
        return Answer(text="大模型暂时不可用，请稍后再问。", ok=False,
                      note=str(exc))

    try:
        sql = parse_sql_json(raw)
    except SqlRejected as exc:
        return Answer(text=f"没能生成可执行的查询：{exc}", ok=False,
                      note=raw[:200])

    try:
        safe_sql = validate_sql(sql)
    except SqlRejected as exc:
        return Answer(text=f"拒绝执行：{exc}", sql=sql, ok=False)

    try:
        rows = runner(safe_sql)
    except Exception as exc:  # noqa: BLE001 - 查询失败要回给用户而不是崩掉进程
        return Answer(text=f"查询执行失败：{exc}", sql=safe_sql, ok=False)

    if rows.empty:
        return Answer(text="没有查到数据。", sql=safe_sql, ok=True)

    rows_text = render_rows(rows)
    try:
        summary = llm.complete(build_summary_messages(question, safe_sql, rows_text))
    except LlmError as exc:
        return Answer(text=f"已查到 {len(rows)} 行数据，但总结失败：{exc}",
                      sql=safe_sql, row_count=len(rows), ok=False,
                      note=rows_text[:200])
    return Answer(text=summary, sql=safe_sql, row_count=len(rows), ok=True)
