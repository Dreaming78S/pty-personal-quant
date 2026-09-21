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

SUMMARY_SYSTEM_PROMPT = """你是 A 股量化助手。根据查询结果回答用户问题，只输出一个 JSON 对象。

JSON 结构：
{"conclusion": "一句话结论（中文，可用 **粗体**）",
 "columns": [{"key": "date", "name": "日期", "type": "text"}],
 "rows": [{"date": "2026-09-21"}]}

要求：
- columns 最多 6 列、rows 最多 20 行；列名必须用中文（如 日期/策略/排名/收盘价/成交额/连续命中），
  不要出现 raw_close、score、trade_date 这类数据库字段名
- 策略名一律用中文，按下表映射：ma_volume=均线放量、turtle_trade=海龟交易、
  high_tight_flag=高位窄幅整理、limit_up_shakeout=涨停洗盘、uptrend_limit_down=上涨趋势跌停、
  rps_breakout=RPS突破、rise_shrink_pullback=上涨缩量回调
- 每列 type 取 text 或 number；number 列的值只放纯数字（不要单位、千分位、百分号）
- rows 中每个对象的 key 必须来自 columns，不要编造查询结果里没有的数据
- 结果为空时 conclusion 写「没有查到数据」，columns 与 rows 用空数组
- 只输出 JSON：不要 markdown 代码围栏、不要 JSON 之外的任何说明文字"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

MAX_TABLE_COLUMNS = 6
MAX_TABLE_ROWS = 20


@dataclass(frozen=True)
class Column:
    key: str
    name: str
    kind: str = "text"


@dataclass(frozen=True)
class Table:
    columns: tuple[Column, ...]
    rows: tuple[dict, ...]


@dataclass(frozen=True)
class Answer:
    text: str
    sql: str = ""
    row_count: int = 0
    ok: bool = True
    note: str = ""
    table: Table | None = None


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


def _extract_json(text: str) -> dict | None:
    """从模型输出里取出 JSON 对象（容忍 ``` 围栏与前后说明文字）。"""
    candidate = (text or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None


def parse_summary(text: str) -> tuple[str, Table | None]:
    """解析总结 JSON：返回（结论, 表格）；解析失败时回退为原始文本且无表格。"""
    raw = (text or "").strip()
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        return raw, None

    columns: list[Column] = []
    for item in payload.get("columns") or []:
        if len(columns) >= MAX_TABLE_COLUMNS:
            break
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        name = str(item.get("name") or "").strip()
        if not key or not name or any(c.key == key for c in columns):
            continue
        kind = ("number" if str(item.get("type") or "").lower() == "number"
                else "text")
        columns.append(Column(key, name, kind))

    conclusion = str(payload.get("conclusion") or "").strip()
    if not columns:
        return (conclusion or raw), None

    keys = [column.key for column in columns]
    rows: list[dict] = []
    for item in payload.get("rows") or []:
        if len(rows) >= MAX_TABLE_ROWS:
            break
        if not isinstance(item, dict):
            continue
        row = {key: item[key] for key in keys if key in item}
        if row:
            rows.append(row)
    if not rows:
        return (conclusion or raw), None
    if not conclusion:
        conclusion = "查询结果如下。"
    return conclusion, Table(tuple(columns), tuple(rows))


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
    conclusion, table = parse_summary(summary)
    return Answer(text=conclusion, sql=safe_sql, row_count=len(rows), ok=True,
                  table=table)
