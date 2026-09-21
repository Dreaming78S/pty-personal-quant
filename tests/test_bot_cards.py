import json

from quant.bot import cards
from quant.bot.qa import Answer, Column, Table


def _content(card, index=0):
    element = card["elements"][index]
    return element["text"]["content"] if "text" in element else element["content"]


def test_build_answer_card_renders_markdown_body_without_sql():
    answer = Answer("近 30 天命中 2 次", "SELECT 1 LIMIT 200", 2, True)

    card = cards.build_answer_card("博敏电子最近几天命中策略的情况", answer)

    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "【问数】博敏电子最近几天命中策略的情况"
    assert card["elements"][0]["tag"] == "markdown"
    assert _content(card) == "近 30 天命中 2 次"
    assert len(card["elements"]) == 1
    assert "SELECT" not in json.dumps(card, ensure_ascii=False)
    json.dumps(card, ensure_ascii=False)


def test_build_answer_card_uses_grey_on_failure():
    answer = Answer("拒绝执行：只允许 SELECT / WITH 查询",
                    "DELETE FROM daily", 0, False)

    card = cards.build_answer_card("问题", answer)

    assert card["header"]["template"] == "grey"
    assert _content(card) == "拒绝执行：只允许 SELECT / WITH 查询"
    assert "DELETE" not in json.dumps(card, ensure_ascii=False)


def test_build_answer_card_mentions_sender_in_group():
    answer = Answer("命中 1 次", "SELECT 1", 1, True)

    card = cards.build_answer_card("问题", answer, at_open_id="ou_123")

    assert _content(card).startswith("<at id=ou_123></at>\n")


def test_build_answer_card_truncates_long_question():
    card = cards.build_answer_card("问" * 200, Answer("答"))

    assert len(card["header"]["title"]["content"]) <= cards.MAX_TITLE_CHARS + 1


def test_build_answer_card_omits_row_count_and_note():
    answer = Answer("失败", ok=False, note="大模型超时")

    card = cards.build_answer_card("问题", answer)

    dumped = json.dumps(card, ensure_ascii=False)
    assert "大模型超时" not in dumped
    assert "返回" not in dumped


def test_build_hint_card_is_grey():
    card = cards.build_hint_card("请把问题写在 @我 之后")

    assert card["header"]["template"] == "grey"
    assert card["elements"][0]["tag"] == "markdown"
    assert _content(card) == "请把问题写在 @我 之后"


def test_build_answer_card_renders_table():
    table = Table(
        columns=(Column("date", "日期"), Column("rank", "排名", "number"),
                 Column("amount", "成交额", "number")),
        rows=({"date": "2026-09-21", "rank": 2, "amount": 160740.52},),
    )
    answer = Answer("命中 1 次", "SELECT 1", 1, True, table=table)

    card = cards.build_answer_card("博敏电子最近几天命中策略的情况", answer)

    element = card["elements"][1]
    assert element["tag"] == "table"
    assert element["page_size"] == cards.TABLE_PAGE_SIZE
    assert [c["display_name"] for c in element["columns"]] == ["日期", "排名", "成交额"]
    assert [c["data_type"] for c in element["columns"]] == ["text", "number", "number"]
    assert element["columns"][1]["format"] == {"precision": 0, "separator": True}
    assert element["columns"][2]["format"] == {"precision": 2, "separator": True}
    assert element["rows"] == [{"date": "2026-09-21", "rank": 2,
                                "amount": 160740.52}]
    assert card["fallback"] == {"trigger_conditions": [
        {"type": "element_tags", "value": ["table"]}]}
    json.dumps(card, ensure_ascii=False)


def test_build_answer_card_without_table_has_no_fallback():
    card = cards.build_answer_card("问题", Answer("没有查到数据。", ok=True))

    assert "fallback" not in card
    assert [element["tag"] for element in card["elements"]] == ["markdown"]


def test_build_answer_card_failure_has_no_table():
    table = Table(columns=(Column("date", "日期"),),
                  rows=({"date": "2026-09-21"},))
    answer = Answer("查询执行失败：boom", "SELECT 1", 0, False, table=table)

    card = cards.build_answer_card("问题", answer)

    assert card["header"]["template"] == "grey"
    assert all(element["tag"] != "table" for element in card["elements"])
