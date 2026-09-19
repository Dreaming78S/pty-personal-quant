import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.engine import notify


def _hits_frame():
    return pd.DataFrame({
        "rank": [1, 2],
        "name": ["豪美新材", "博敏电子"],
        "industry": ["有色金属", "电子"],
        "raw_close": [25.31, 12.08],
    })


def _co_hits_frame():
    return pd.DataFrame({
        "strategy": ["ma_volume", "rps_breakout", "turtle_trade",
                     "high_tight_flag", "rps_breakout"],
        "ts_code": ["603936.SH", "603936.SH", "603285.SH",
                    "603285.SH", "600000.SH"],
        "name": ["博敏电子", "博敏电子", "键邦股份", "键邦股份", "浦发银行"],
        "industry": ["元器件", "元器件", "化工原料", "化工原料", "银行"],
        "raw_close": [21.34, 21.34, 37.86, 37.86, 10.25],
        "amount": [1068000.0, 1068000.0, 205000.0, 205000.0, 9999999.0],
    })


def _patch_empty_sources(monkeypatch):
    monkeypatch.setattr(notify.loader, "resolve_trade_date",
                        lambda date=None: "20260918")
    monkeypatch.setattr(notify, "resolve_strategies",
                        lambda strategy: ["ma_volume"])
    monkeypatch.setattr(notify.ingest, "get_watermark",
                        lambda table: "20260918")
    monkeypatch.setattr(notify.db, "read_df",
                        lambda sql, params=None: pd.DataFrame(
                            columns=["name", "industry", "raw_close", "rank"]))


def test_format_message_lists_hits():
    message = notify.format_message("rise_shrink_pullback", "20260918",
                                    _hits_frame())

    assert message.title == "【rise_shrink_pullback】2026-09-18 命中 2 只"
    assert message.lines == (
        "1. **豪美新材** 25.31 [有色金属]",
        "2. **博敏电子** 12.08 [电子]",
    )
    assert message.has_hits is True


def test_format_message_without_hits():
    empty = _hits_frame().iloc[0:0]

    message = notify.format_message("ma_volume", "20260918", empty)

    assert message.title == "【ma_volume】2026-09-18"
    assert message.lines == ("今日无命中",)
    assert message.has_hits is False


def test_format_message_missing_fields_use_placeholder():
    rows = pd.DataFrame({
        "rank": [1],
        "name": [None],
        "industry": [None],
        "raw_close": [float("nan")],
    })

    message = notify.format_message("ma_volume", "20260918", rows)

    assert message.lines == ("1. - - [-]",)


def test_format_message_handles_decimal_values():
    from decimal import Decimal

    rows = pd.DataFrame({
        "rank": [1],
        "name": ["浦发银行"],
        "industry": ["银行"],
        "raw_close": [Decimal("10.25")],
    })

    message = notify.format_message("ma_volume", "20260918", rows)

    assert message.lines == ("1. **浦发银行** 10.25 [银行]",)


def test_render_text_strips_markdown():
    message = notify.format_message("ma_volume", "20260918", _hits_frame())

    assert notify.render_text(message) == (
        "【ma_volume】2026-09-18 命中 2 只\n\n"
        "1. 豪美新材 25.31 [有色金属]\n"
        "2. 博敏电子 12.08 [电子]")


def test_format_message_truncates_long_lists(monkeypatch):
    monkeypatch.setattr(notify, "MAX_TEXT_CHARS", 160)
    rows = pd.DataFrame({
        "rank": list(range(1, 201)),
        "name": ["测试股"] * 200,
        "industry": ["行业"] * 200,
        "raw_close": [10.0] * 200,
    })

    message = notify.format_message("ma_volume", "20260918", rows)
    body = "\n".join(message.lines)

    assert len(message.title) + len(body) <= 160
    assert "1. **测试股** 10.00 [行业]" in body
    assert "…（清单过长，仅显示前" in body
    assert "共 200 只）" in body


def test_format_co_message_lists_multi_strategy_hits():
    message = notify.format_co_message("20260918", _co_hits_frame())

    assert message.title == "【多策略共振】2026-09-18 共 2 只"
    assert message.lines == (
        "1. **博敏电子** 21.34 [元器件] — ma_volume、rps_breakout",
        "2. **键邦股份** 37.86 [化工原料] — high_tight_flag、turtle_trade",
    )
    assert message.has_hits is True
    assert message.template == "orange"


def test_format_co_message_without_co_hits():
    single = _co_hits_frame().iloc[[4]]

    message = notify.format_co_message("20260918", single)

    assert message.title == "【多策略共振】2026-09-18"
    assert message.lines == ("今日无共振",)
    assert message.has_hits is False


def test_feishu_payload_builds_card():
    message = notify.format_message("ma_volume", "20260918", _hits_frame())

    payload = notify.feishu_payload(message)

    assert payload["msg_type"] == "interactive"
    card = payload["card"]
    assert card["header"]["title"] == {
        "tag": "plain_text", "content": "【ma_volume】2026-09-18 命中 2 只"}
    assert card["header"]["template"] == "blue"
    assert card["elements"] == [{
        "tag": "div",
        "text": {"tag": "lark_md",
                 "content": "1. **豪美新材** 25.31 [有色金属]\n"
                            "2. **博敏电子** 12.08 [电子]"},
    }]


def test_feishu_payload_no_hits_uses_grey_header():
    message = notify.format_message("ma_volume", "20260918",
                                    _hits_frame().iloc[0:0])

    payload = notify.feishu_payload(message)

    assert payload["card"]["header"]["template"] == "grey"


def test_feishu_payload_co_message_uses_orange_header():
    message = notify.format_co_message("20260918", _co_hits_frame())

    payload = notify.feishu_payload(message)

    assert payload["card"]["header"]["template"] == "orange"


def test_feishu_payload_signs_with_secret():
    message = notify.format_message("ma_volume", "20260918",
                                    _hits_frame().iloc[0:0])
    payload = notify.feishu_payload(message, secret="s3cret",
                                    timestamp="1700000000")
    string_to_sign = "1700000000\ns3cret"
    expected = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"),
                 digestmod=hashlib.sha256).digest()).decode("utf-8")

    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == expected
    assert payload["msg_type"] == "interactive"


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_message_posts_card_json(monkeypatch):
    calls = {}

    def fake_urlopen(request, timeout=None):
        calls["request"] = request
        calls["timeout"] = timeout
        return _FakeResponse(b'{"code":0,"msg":"success"}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    message = notify.format_message("ma_volume", "20260918", _hits_frame())

    notify.send_message("https://example.com/hook", message, secret="s3cret")

    request = calls["request"]
    assert request.full_url == "https://example.com/hook"
    assert calls["timeout"] == 10
    assert "json" in request.get_header("Content-type")
    body = json.loads(request.data.decode("utf-8"))
    assert body["msg_type"] == "interactive"
    assert body["card"]["header"]["title"]["content"] == (
        "【ma_volume】2026-09-18 命中 2 只")
    assert body["sign"]


def test_send_message_raises_on_feishu_error(monkeypatch):
    monkeypatch.setattr(
        notify.urllib.request, "urlopen",
        lambda request, timeout=None: _FakeResponse(
            b'{"code":19021,"msg":"sign match fail"}'))
    message = notify.format_message("ma_volume", "20260918",
                                    _hits_frame().iloc[0:0])

    with pytest.raises(RuntimeError, match="19021"):
        notify.send_message("https://example.com/hook", message)


def test_send_message_retries_transient_network_errors(monkeypatch):
    from urllib.error import URLError

    attempts = []
    sleeps = []

    def fake_urlopen(request, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise URLError(TimeoutError(
                "_ssl.c:983: The handshake operation timed out"))
        return _FakeResponse(b'{"code":0,"msg":"success"}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda seconds: sleeps.append(seconds))
    message = notify.format_message("ma_volume", "20260918",
                                    _hits_frame().iloc[0:0])

    notify.send_message("https://example.com/hook", message)

    assert len(attempts) == 3
    assert sleeps == [1.0, 1.0]


def test_send_message_raises_after_retries_exhausted(monkeypatch):
    from urllib.error import URLError

    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(1)
        raise URLError("handshake failed")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda seconds: None)
    message = notify.format_message("ma_volume", "20260918",
                                    _hits_frame().iloc[0:0])

    with pytest.raises(URLError):
        notify.send_message("https://example.com/hook", message)

    assert len(attempts) == 3


def test_send_message_does_not_retry_business_errors(monkeypatch):
    attempts = []
    sleeps = []

    def fake_urlopen(request, timeout=None):
        attempts.append(1)
        return _FakeResponse(b'{"code":19021,"msg":"sign match fail"}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda seconds: sleeps.append(seconds))
    message = notify.format_message("ma_volume", "20260918",
                                    _hits_frame().iloc[0:0])

    with pytest.raises(RuntimeError, match="19021"):
        notify.send_message("https://example.com/hook", message)

    assert len(attempts) == 1
    assert sleeps == []


def test_resolve_strategies_rejects_unknown():
    with pytest.raises(ValueError, match="未注册的策略"):
        notify.resolve_strategies("no_such_strategy")


def test_build_messages_includes_empty_strategies(monkeypatch):
    _patch_empty_sources(monkeypatch)
    monkeypatch.setattr(notify, "build_co_message",
                        lambda date: notify.FeishuMessage(
                            "【多策略共振】2026-09-18", ("今日无共振",), False))

    messages = notify.build_messages("all", "20260918")

    assert list(messages) == ["ma_volume", "多策略共振"]
    assert notify.render_text(messages["ma_volume"]) == (
        "【ma_volume】2026-09-18\n\n今日无命中")
    assert notify.render_text(messages["多策略共振"]) == (
        "【多策略共振】2026-09-18\n\n今日无共振")


def test_build_messages_can_skip_co_message(monkeypatch):
    _patch_empty_sources(monkeypatch)
    monkeypatch.setattr(notify, "build_co_message",
                        lambda date: pytest.fail("不应组装共振卡片"))

    messages = notify.build_messages("all", "20260918", include_co=False)

    assert set(messages) == {"ma_volume"}


def test_build_messages_rejects_stale_watermark(monkeypatch):
    monkeypatch.setattr(notify.loader, "resolve_trade_date",
                        lambda date=None: "20260918")
    monkeypatch.setattr(notify, "resolve_strategies",
                        lambda strategy: ["ma_volume"])
    monkeypatch.setattr(notify.ingest, "get_watermark",
                        lambda table: "20260917")

    with pytest.raises(ValueError, match="hits update"):
        notify.build_messages("ma_volume", "20260918")


def test_build_co_message_checks_all_strategy_watermarks(monkeypatch):
    monkeypatch.setattr(notify.loader, "resolve_trade_date",
                        lambda date=None: "20260918")
    monkeypatch.setattr(notify, "list_strategies",
                        lambda: {"ma_volume": object,
                                 "rps_breakout": object})
    monkeypatch.setattr(notify.ingest, "get_watermark",
                        lambda table: ("20260918" if table == "hit_ma_volume"
                                       else "20260917"))

    with pytest.raises(ValueError, match="hits update"):
        notify.build_co_message("20260918")


def test_build_co_message_collects_all_strategy_hits(monkeypatch):
    monkeypatch.setattr(notify.loader, "resolve_trade_date",
                        lambda date=None: "20260918")
    monkeypatch.setattr(notify, "list_strategies",
                        lambda: {"ma_volume": object,
                                 "rps_breakout": object})
    monkeypatch.setattr(notify.ingest, "get_watermark",
                        lambda table: "20260918")

    def fake_read(sql, params=None):
        if "hit_ma_volume" in sql:
            return pd.DataFrame({
                "ts_code": ["603936.SH"],
                "name": ["博敏电子"],
                "industry": ["元器件"],
                "raw_close": [21.34],
                "amount": [1068000.0],
            })
        return pd.DataFrame({
            "ts_code": ["603936.SH"],
            "name": ["博敏电子"],
            "industry": ["元器件"],
            "raw_close": [21.34],
            "amount": [1068000.0],
        })

    monkeypatch.setattr(notify.db, "read_df", fake_read)

    message = notify.build_co_message("20260918")

    assert message.title == "【多策略共振】2026-09-18 共 1 只"
    assert message.lines == (
        "1. **博敏电子** 21.34 [元器件] — ma_volume、rps_breakout",)


def test_notify_hits_requires_webhook(monkeypatch):
    monkeypatch.setattr(notify, "get_settings",
                        lambda: SimpleNamespace(feishu_webhook_url=None,
                                                feishu_webhook_secret=None))

    with pytest.raises(ValueError, match="FEISHU_WEBHOOK_URL"):
        notify.notify_hits("ma_volume", "20260918")


def test_notify_hits_sends_each_strategy_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(notify, "get_settings",
                        lambda: SimpleNamespace(
                            feishu_webhook_url="https://hook",
                            feishu_webhook_secret="s3cret"))
    message_a = notify.FeishuMessage("标题A", ("行A",), True)
    message_b = notify.FeishuMessage("标题B", ("行B",), True)
    monkeypatch.setattr(notify, "build_messages",
                        lambda strategy, date, include_co=True: {
                            "ma_volume": message_a,
                            "rps_breakout": message_b})
    sent = []

    def fake_send(url, message, secret=None):
        sent.append((url, message, secret))
        if message is message_b:
            raise RuntimeError("boom")

    monkeypatch.setattr(notify, "send_message", fake_send)

    results = notify.notify_hits("all", "20260918")

    assert sent == [("https://hook", message_a, "s3cret"),
                    ("https://hook", message_b, "s3cret")]
    assert results == {"ma_volume": "已发送", "rps_breakout": "失败：boom"}


def test_notify_hits_passes_include_co(monkeypatch):
    captured = {}
    monkeypatch.setattr(notify, "get_settings",
                        lambda: SimpleNamespace(
                            feishu_webhook_url="https://hook",
                            feishu_webhook_secret=None))

    def fake_build(strategy, date, include_co=True):
        captured["include_co"] = include_co
        return {}

    monkeypatch.setattr(notify, "build_messages", fake_build)

    assert notify.notify_hits("all", "20260918", include_co=False) == {}
    assert captured == {"include_co": False}
