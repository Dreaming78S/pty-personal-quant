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
