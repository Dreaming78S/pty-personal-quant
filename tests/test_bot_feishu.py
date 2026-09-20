import json
import warnings

import pytest

warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module=r"lark_oapi\..*")

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: E402

from quant.bot import feishu  # noqa: E402
from quant.bot.qa import Answer  # noqa: E402


def make_event(text="@_user_1 博敏电子最近几天命中策略的情况",
               *, message_type="text", sender_type="user",
               chat_type="group", message_id="om_1", open_id="ou_1",
               content=None):
    payload = {
        "event": {
            "sender": {"sender_type": sender_type,
                       "sender_id": {"open_id": open_id}},
            "message": {"message_id": message_id, "chat_id": "oc_1",
                        "chat_type": chat_type, "message_type": message_type,
                        "content": content if content is not None
                        else json.dumps({"text": text})},
        }
    }
    return P2ImMessageReceiveV1(payload)


class FakeResponse:
    def __init__(self, ok=True):
        self._ok = ok
        self.code = 0 if ok else 99991663
        self.msg = "" if ok else "boom"

    def success(self):
        return self._ok


class FakeMessageApi:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def reply(self, request):
        self.requests.append(request)
        return self.response


class FakeClient:
    def __init__(self, response=None):
        self.message_api = FakeMessageApi(response or FakeResponse())
        self.im = type("Im", (), {"v1": type("V1", (), {
            "message": self.message_api})})()


def test_strip_mentions_removes_placeholders():
    assert feishu.strip_mentions("@_user_1 博敏电子 怎么样") == "博敏电子 怎么样"
    assert feishu.strip_mentions("@_user_1 @_user_2") == ""


def test_parse_event_returns_question():
    question = feishu.parse_event(make_event())

    assert question is not None
    assert question.question == "博敏电子最近几天命中策略的情况"
    assert question.message_id == "om_1"
    assert question.chat_type == "group"
    assert question.sender_open_id == "ou_1"
    assert question.is_group is True


def test_parse_event_keeps_empty_question():
    question = feishu.parse_event(make_event(text="@_user_1"))

    assert question is not None
    assert question.question == ""


def test_parse_event_ignores_bot_and_non_text():
    assert feishu.parse_event(make_event(sender_type="app")) is None
    assert feishu.parse_event(make_event(message_type="image")) is None


def test_parse_event_ignores_broken_content():
    event = make_event(content="{not json")

    question = feishu.parse_event(event)

    assert question is not None
    assert question.question == ""


def test_parse_event_handles_non_object_json():
    for content in ("123", "null", "[]", '"text"'):
        question = feishu.parse_event(make_event(content=content))

        assert question is not None
        assert question.question == ""


def test_reply_card_sends_interactive_card():
    client = FakeClient()
    card = {"header": {"title": {"tag": "plain_text", "content": "t"}},
            "elements": []}

    feishu.reply_card(client, "om_1", card)

    request = client.message_api.requests[0]
    assert request.message_id == "om_1"
    assert request.request_body.msg_type == "interactive"
    assert json.loads(request.request_body.content) == card


def test_reply_card_raises_on_business_error():
    client = FakeClient(FakeResponse(ok=False))

    with pytest.raises(RuntimeError, match="99991663"):
        feishu.reply_card(client, "om_1", {"elements": []})


def test_service_answers_and_replies(monkeypatch):
    client = FakeClient()
    service = feishu.BotService(
        client, llm=object(), runner=lambda sql: None,
        reply=lambda c, mid, card: c.message_api.requests.append(card))
    monkeypatch.setattr(feishu.qa, "answer",
                        lambda question, llm, runner: Answer("命中 2 次", "SELECT 1", 2, True))

    service.handle_event(make_event())
    service._pool.shutdown(wait=True)

    card = client.message_api.requests[0]
    assert card["header"]["title"]["content"] == "【问数】博敏电子最近几天命中策略的情况"
    assert "<at id=ou_1></at>" in card["elements"][0]["text"]["content"]


def test_service_does_not_at_in_private_chat(monkeypatch):
    client = FakeClient()
    service = feishu.BotService(
        client, llm=object(), runner=lambda sql: None,
        reply=lambda c, mid, card: c.message_api.requests.append(card))
    monkeypatch.setattr(feishu.qa, "answer",
                        lambda question, llm, runner: Answer("命中 1 次", "SELECT 1", 1, True))

    service.handle_event(make_event(chat_type="p2p"))
    service._pool.shutdown(wait=True)

    card = client.message_api.requests[0]
    assert "<at id=" not in card["elements"][0]["text"]["content"]


def test_service_hints_on_empty_question():
    client = FakeClient()
    service = feishu.BotService(
        client, llm=object(), runner=lambda sql: None,
        reply=lambda c, mid, card: c.message_api.requests.append(card))

    service.handle_event(make_event(text="@_user_1"))
    service._pool.shutdown(wait=True)

    card = client.message_api.requests[0]
    assert card["header"]["template"] == "grey"
    assert feishu.EMPTY_QUESTION_HINT in card["elements"][0]["text"]["content"]


def test_service_deduplicates_redelivered_events(monkeypatch):
    client = FakeClient()
    service = feishu.BotService(
        client, llm=object(), runner=lambda sql: None,
        reply=lambda c, mid, card: c.message_api.requests.append(card))
    monkeypatch.setattr(feishu.qa, "answer",
                        lambda question, llm, runner: Answer("答", "SELECT 1", 1, True))
    event = make_event()

    service.handle_event(event)
    service.handle_event(event)
    service._pool.shutdown(wait=True)

    assert len(client.message_api.requests) == 1


def test_service_swallows_handler_errors():
    client = FakeClient()
    service = feishu.BotService(
        client, llm=object(), runner=lambda sql: None,
        reply=lambda c, mid, card: (_ for _ in ()).throw(RuntimeError("boom")))

    service.handle_event(make_event(text="@_user_1 空"))
    service._pool.shutdown(wait=True)


def test_run_bot_requires_credentials(monkeypatch):
    monkeypatch.setattr(feishu, "get_settings",
                        lambda: type("S", (), {"feishu_app_id": None,
                                               "feishu_app_secret": None,
                                               "deepseek_api_key": None})())

    with pytest.raises(ValueError, match="FEISHU_APP_ID"):
        feishu.run_bot()


def test_run_bot_requires_deepseek_key(monkeypatch):
    monkeypatch.setattr(feishu, "get_settings",
                        lambda: type("S", (), {"feishu_app_id": "cli_x",
                                               "feishu_app_secret": "sec",
                                               "deepseek_api_key": None})())

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        feishu.run_bot()
