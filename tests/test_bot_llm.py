import json
import urllib.error

import pytest

from quant.bot.llm import DeepSeekClient, LlmError, parse_completion


def test_parse_completion_returns_content():
    payload = {"choices": [{"message": {"content": " 答案 "}}]}

    assert parse_completion(payload) == "答案"


def test_parse_completion_falls_back_to_reasoning_content():
    payload = {"choices": [{"message": {"content": "", "reasoning_content": " 思考 "}}]}

    assert parse_completion(payload) == "思考"


def test_parse_completion_raises_on_empty():
    with pytest.raises(LlmError, match="空内容"):
        parse_completion({"choices": [{"message": {"content": "  "}}]})


def test_parse_completion_raises_on_bad_structure():
    with pytest.raises(LlmError, match="结构异常"):
        parse_completion({"error": "boom"})


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_complete_posts_and_returns_text(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": "命中 2 次"}}]})

    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen", fake_urlopen)
    client = DeepSeekClient("sk-x", "https://api.deepseek.com", "deepseek-flash")

    text = client.complete([{"role": "user", "content": "hi"}])

    assert text == "命中 2 次"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-x"
    assert captured["body"]["model"] == "deepseek-flash"
    assert captured["body"]["stream"] is False


def test_complete_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("handshake timeout")
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("quant.bot.llm.time.sleep", lambda s: sleeps.append(s))

    assert DeepSeekClient("sk-x").complete([]) == "ok"
    assert calls["n"] == 2
    assert sleeps == [1.0]


def test_complete_raises_llm_error_on_client_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {},
            type("B", (), {"read": lambda self: b'{"error":"bad model"}'})())

    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(LlmError, match="400"):
        DeepSeekClient("sk-x").complete([])
