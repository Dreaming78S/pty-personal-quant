from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import (P2ImMessageReceiveV1, ReplyMessageRequest,
                                 ReplyMessageRequestBody)

from quant.bot import cards, qa
from quant.bot.llm import DeepSeekClient
from quant.bot.runner import run_readonly
from quant.config import get_settings

MENTION_PATTERN = re.compile(r"@_user_\d+")
SEEN_MESSAGE_LIMIT = 200
WORKERS = 2
EMPTY_QUESTION_HINT = "请把问题写在 @我 之后，例如：博敏电子最近几天命中策略的情况"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Question:
    question: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"


def strip_mentions(text: str) -> str:
    """去掉飞书文本消息里的 @_user_N 占位符。"""
    return MENTION_PATTERN.sub(" ", text or "").strip()


def parse_event(data: P2ImMessageReceiveV1) -> Question | None:
    """解析消息事件；非文本、机器人自己发的、群里未 @我的消息返回 None。"""
    event = getattr(data, "event", None)
    if event is None or event.message is None or event.sender is None:
        return None
    if event.sender.sender_type == "app":
        return None
    message = event.message
    if message.message_type != "text":
        return None
    try:
        parsed = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        parsed = {}
    text = parsed.get("text", "") if isinstance(parsed, dict) else ""
    if not isinstance(text, str):
        text = ""
    if (message.chat_type or "") == "group":
        mentioned = bool(getattr(message, "mentions", None)) or bool(
            MENTION_PATTERN.search(message.content or ""))
        if not mentioned:
            return None
    sender_id = event.sender.sender_id
    return Question(
        question=strip_mentions(text),
        message_id=message.message_id or "",
        chat_id=message.chat_id or "",
        chat_type=message.chat_type or "",
        sender_open_id=getattr(sender_id, "open_id", "") or "",
    )


def reply_card(client, message_id: str, card: dict) -> None:
    """用飞书"回复消息"接口发交互卡片。"""
    body = (ReplyMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .msg_type("interactive")
            .build())
    request = (ReplyMessageRequest.builder()
               .message_id(message_id)
               .request_body(body)
               .build())
    response = client.im.v1.message.reply(request)
    if not response.success():
        raise RuntimeError(
            f"飞书回复失败：code={response.code} msg={response.msg}")


class BotService:
    """事件回调 → 一问一答 → 卡片回复。"""

    def __init__(self, client, llm,
                 runner: Callable = run_readonly,
                 reply: Callable = reply_card,
                 workers: int = WORKERS) -> None:
        self._client = client
        self._llm = llm
        self._runner = runner
        self._reply = reply
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def _is_new(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen[message_id] = None
            while len(self._seen) > SEEN_MESSAGE_LIMIT:
                self._seen.popitem(last=False)
            return True

    def handle_event(self, data: P2ImMessageReceiveV1) -> None:
        question = parse_event(data)
        if question is None or not question.message_id:
            return
        if not self._is_new(question.message_id):
            return
        self._pool.submit(self._ask_and_reply, question)

    def _ask_and_reply(self, question: Question) -> None:
        try:
            if not question.question:
                self._reply(self._client, question.message_id,
                            cards.build_hint_card(EMPTY_QUESTION_HINT))
                return
            result = qa.answer(question.question, self._llm, self._runner)
            at = question.sender_open_id if question.is_group else None
            self._reply(self._client, question.message_id,
                        cards.build_answer_card(question.question, result, at))
        except Exception:  # noqa: BLE001 - 后台线程不能因单条消息崩溃
            logger.exception("处理提问失败：%s", question.message_id)


def run_bot() -> None:
    """启动飞书长连接问数机器人（阻塞，Ctrl+C 退出）。"""
    settings = get_settings()
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise ValueError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET（.env）")
    if not settings.deepseek_api_key:
        raise ValueError("未配置 DEEPSEEK_API_KEY（.env）")

    client = (lark.Client.builder()
              .app_id(settings.feishu_app_id)
              .app_secret(settings.feishu_app_secret)
              .build())
    llm = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url,
                         settings.deepseek_model)
    service = BotService(client, llm)
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(service.handle_event)
               .build())
    logger.info("飞书问数机器人已启动，等待 @我 提问…")
    lark.ws.Client(settings.feishu_app_id, settings.feishu_app_secret,
                   event_handler=handler,
                   log_level=lark.LogLevel.INFO).start()
