import json

from quant.bot import cards
from quant.bot.qa import Answer


def _content(card, index=0):
    return card["elements"][index]["text"]["content"]


def test_build_answer_card_has_title_body_and_sql():
    answer = Answer("博敏电子近 3 日命中 2 次", "SELECT 1 LIMIT 200", 2, True)

    card = cards.build_answer_card("博敏电子最近几天命中策略的情况", answer)

    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "【问数】博敏电子最近几天命中策略的情况"
    assert _content(card) == "博敏电子近 3 日命中 2 次"
    assert "返回 2 行" in _content(card, 2)
    assert _content(card, 3) == "```sql\nSELECT 1 LIMIT 200\n```"
    json.dumps(card, ensure_ascii=False)


def test_build_answer_card_uses_grey_on_failure():
    answer = Answer("拒绝执行：只允许 SELECT / WITH 查询",
                    "DELETE FROM daily", 0, False)

    card = cards.build_answer_card("问题", answer)

    assert card["header"]["template"] == "grey"
    assert _content(card, 2) == "查询依据：返回 0 行"


def test_build_answer_card_mentions_sender_in_group():
    answer = Answer("命中 1 次", "SELECT 1", 1, True)

    card = cards.build_answer_card("问题", answer, at_open_id="ou_123")

    assert _content(card).startswith("<at id=ou_123></at>\n")


def test_build_answer_card_truncates_long_question():
    card = cards.build_answer_card("问" * 200, Answer("答"))

    assert len(card["header"]["title"]["content"]) <= cards.MAX_TITLE_CHARS + 1


def test_build_answer_card_includes_note():
    answer = Answer("失败", ok=False, note="大模型超时")

    card = cards.build_answer_card("问题", answer)

    assert "大模型超时" in _content(card, 2)


def test_build_hint_card_is_grey():
    card = cards.build_hint_card("请把问题写在 @我 之后")

    assert card["header"]["template"] == "grey"
    assert _content(card) == "请把问题写在 @我 之后"
