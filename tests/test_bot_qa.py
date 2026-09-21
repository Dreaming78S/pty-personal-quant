import json

import pandas as pd
import pytest

from quant.bot import qa
from quant.bot.llm import LlmError
from quant.bot.sql_guard import SqlRejected


class ScriptedLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, max_tokens=qa.MAX_ROWS, temperature=0.0):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def fake_runner(rows):
    def run(sql):
        return rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    return run


def test_parse_sql_json_handles_fences_and_noise():
    assert qa.parse_sql_json('```json\n{"sql": "SELECT 1"}\n```') == "SELECT 1"
    assert qa.parse_sql_json('好的：{"sql": "SELECT 2"} 以上') == "SELECT 2"


def test_parse_sql_json_rejects_missing_sql():
    with pytest.raises(SqlRejected, match="没有返回 JSON"):
        qa.parse_sql_json("我无法回答")
    with pytest.raises(SqlRejected, match="没有给出 SQL"):
        qa.parse_sql_json('{"explain": "无"}')


def test_render_rows_truncates_by_rows():
    frame = pd.DataFrame({"a": range(5)})

    text = qa.render_rows(frame, max_rows=3)

    assert "仅显示前 3 行" in text
    assert len(text.splitlines()) == 5


def test_render_rows_truncates_by_chars():
    frame = pd.DataFrame({"a": ["x" * 100]})

    text = qa.render_rows(frame, max_chars=10)

    assert "结果过长已截断" in text
    assert text.startswith("a\nxxxxxxxx")


def test_render_rows_handles_empty():
    assert qa.render_rows(pd.DataFrame()) == "（无数据行）"


def test_answer_happy_path():
    llm = ScriptedLlm(['{"sql": "SELECT name FROM hit_ma_volume"}', "博敏电子命中 2 次"])
    rows = pd.DataFrame({"name": ["博敏电子", "博敏电子"]})

    result = qa.answer("博敏电子命中情况", llm, fake_runner(rows))

    assert result.ok is True
    assert result.text == "博敏电子命中 2 次"
    assert result.row_count == 2
    assert result.sql == "SELECT name FROM hit_ma_volume LIMIT 200"
    assert len(llm.calls) == 2


def test_answer_rejects_unsafe_sql():
    llm = ScriptedLlm(['{"sql": "DELETE FROM hit_ma_volume"}'])

    result = qa.answer("删掉数据", llm, fake_runner([]))

    assert result.ok is False
    assert "拒绝执行" in result.text
    assert result.sql == "DELETE FROM hit_ma_volume"
    assert len(llm.calls) == 1


def test_answer_reports_llm_failure():
    llm = ScriptedLlm([LlmError("超时")])

    result = qa.answer("问题", llm, fake_runner([]))

    assert result.ok is False
    assert "大模型暂时不可用" in result.text
    assert result.note == "超时"


def test_answer_reports_query_failure():
    def boom(sql):
        raise RuntimeError("Table doesn't exist")

    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}'])

    result = qa.answer("问题", llm, boom)

    assert result.ok is False
    assert "查询执行失败" in result.text


def test_answer_reports_empty_result_without_second_call():
    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}'])

    result = qa.answer("问题", llm, fake_runner([]))

    assert result.ok is True
    assert result.text == "没有查到数据。"
    assert len(llm.calls) == 1


def test_answer_reports_summary_failure():
    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}', LlmError("boom")])

    result = qa.answer("问题", llm, fake_runner([{"a": 1}]))

    assert result.ok is False
    assert "总结失败" in result.text
    assert result.row_count == 1


def test_build_sql_messages_includes_schema_and_question():
    messages = qa.build_sql_messages("博敏电子", "### daily\nCREATE TABLE ...")

    assert messages[0]["role"] == "system"
    assert "只读" in messages[0]["content"]
    assert "### daily" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "博敏电子"}


def test_summary_prompt_requires_json_output():
    """总结必须输出 JSON（表格由代码渲染），且列名要中文化。"""
    assert "JSON" in qa.SUMMARY_SYSTEM_PROMPT
    assert "代码围栏" in qa.SUMMARY_SYSTEM_PROMPT
    assert "raw_close" in qa.SUMMARY_SYSTEM_PROMPT


def test_summary_prompt_maps_strategy_names_to_chinese():
    for name in ("ma_volume=均线放量", "turtle_trade=海龟交易",
                 "rise_shrink_pullback=上涨缩量回调"):
        assert name in qa.SUMMARY_SYSTEM_PROMPT


def test_parse_summary_builds_table():
    raw = json.dumps({
        "conclusion": "近 30 天命中 2 次",
        "columns": [{"key": "date", "name": "日期", "type": "text"},
                    {"key": "rank", "name": "排名", "type": "number"}],
        "rows": [{"date": "2026-09-21", "rank": 2},
                 {"date": "2026-09-18", "rank": 1}],
    }, ensure_ascii=False)

    conclusion, table = qa.parse_summary(raw)

    assert conclusion == "近 30 天命中 2 次"
    assert table is not None
    assert [(c.key, c.name, c.kind) for c in table.columns] == [
        ("date", "日期", "text"), ("rank", "排名", "number")]
    assert table.rows == ({"date": "2026-09-21", "rank": 2},
                          {"date": "2026-09-18", "rank": 1})


def test_parse_summary_falls_back_to_raw_text():
    conclusion, table = qa.parse_summary("博敏电子命中 2 次")

    assert conclusion == "博敏电子命中 2 次"
    assert table is None


def test_parse_summary_caps_columns_and_rows():
    raw = json.dumps({
        "conclusion": "结论",
        "columns": [{"key": f"c{i}", "name": f"列{i}"} for i in range(9)],
        "rows": [{f"c{i}": i for i in range(9)} for _ in range(30)],
    }, ensure_ascii=False)

    _, table = qa.parse_summary(raw)

    assert len(table.columns) == qa.MAX_TABLE_COLUMNS
    assert len(table.rows) == qa.MAX_TABLE_ROWS
    assert set(table.rows[0]) == {f"c{i}" for i in range(qa.MAX_TABLE_COLUMNS)}


def test_parse_summary_ignores_rows_without_known_keys():
    raw = json.dumps({"conclusion": "结论",
                      "columns": [{"key": "date", "name": "日期"}],
                      "rows": [{"date": "2026-09-21", "extra": "x"}, {"other": 1}]})

    _, table = qa.parse_summary(raw)

    assert table.rows == ({"date": "2026-09-21"},)


def test_parse_summary_without_columns_has_no_table():
    conclusion, table = qa.parse_summary('{"conclusion": "没有查到数据"}')

    assert conclusion == "没有查到数据"
    assert table is None


def test_answer_attaches_table_from_summary():
    summary = json.dumps({
        "conclusion": "命中 1 次",
        "columns": [{"key": "date", "name": "日期", "type": "text"}],
        "rows": [{"date": "2026-09-21"}],
    }, ensure_ascii=False)
    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}', summary])

    result = qa.answer("问题", llm, fake_runner([{"a": 1}]))

    assert result.ok is True
    assert result.text == "命中 1 次"
    assert result.table is not None
    assert result.table.rows == ({"date": "2026-09-21"},)
