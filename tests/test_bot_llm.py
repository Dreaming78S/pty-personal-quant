import io
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


def test_parse_completion_raises_on_non_dict_message():
    with pytest.raises(LlmError, match="结构异常"):
        parse_completion({"choices": [{"message": "oops"}]})


def test_parse_completion_raises_on_non_string_content():
    with pytest.raises(LlmError, match="结构异常"):
        parse_completion({"choices": [{"message": {"content": ["x"]}}]})


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
    assert captured["body"]["temperature"] == 0
    assert captured["body"]["max_tokens"] == 4096


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
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":"bad model"}'))

    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("quant.bot.llm.time.sleep", lambda s: sleeps.append(s))

    with pytest.raises(LlmError, match="400"):
        DeepSeekClient("sk-x").complete([])
    assert calls["n"] == 1
    assert sleeps == []


class RawResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_complete_raises_llm_error_on_invalid_json(monkeypatch):
    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen",
                        lambda request, timeout=None: RawResponse(b"{not json"))

    with pytest.raises(LlmError, match="解析失败"):
        DeepSeekClient("sk-x").complete([])


def test_complete_raises_llm_error_on_non_utf8(monkeypatch):
    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen",
                        lambda request, timeout=None: RawResponse(b"\xff\xfe"))

    with pytest.raises(LlmError, match="解析失败"):
        DeepSeekClient("sk-x").complete([])


def test_complete_retries_server_error_then_raises(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            request.full_url, 500, "Server Error", {},
            io.BytesIO(b'{"error":"upstream"}'))

    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("quant.bot.llm.time.sleep", lambda s: sleeps.append(s))

    with pytest.raises(LlmError, match="500"):
        DeepSeekClient("sk-x").complete([])
    assert calls["n"] == 3
    assert sleeps == [1.0, 1.0]


def test_complete_retries_server_error_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                request.full_url, 500, "Server Error", {},
                io.BytesIO(b'{"error":"upstream"}'))
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("quant.bot.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("quant.bot.llm.time.sleep", lambda s: sleeps.append(s))

    assert DeepSeekClient("sk-x").complete([]) == "ok"
    assert calls["n"] == 2
    assert sleeps == [1.0]
