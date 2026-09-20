from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
LLM_TIMEOUT_SECONDS = 60
LLM_MAX_TOKENS = 4096
LLM_TEMPERATURE = 0.0
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0


class LlmError(RuntimeError):
    """大模型调用失败。"""


def parse_completion(payload: dict) -> str:
    """取回答文本；content 为空时回退推理模型的 reasoning_content。"""
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"大模型响应结构异常：{payload}") from exc
    if not isinstance(message, dict):
        raise LlmError(f"大模型响应结构异常：{payload}")
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    if content is not None and not isinstance(content, str):
        raise LlmError(f"大模型响应结构异常：{payload}")
    if reasoning is not None and not isinstance(reasoning, str):
        raise LlmError(f"大模型响应结构异常：{payload}")
    text = (content or "").strip()
    if not text:
        text = (reasoning or "").strip()
    if not text:
        raise LlmError("大模型返回了空内容（可能 max_tokens 不足）")
    return text


class DeepSeekClient:
    """DeepSeek OpenAI 兼容接口的最小客户端。"""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL,
                 timeout: float = LLM_TIMEOUT_SECONDS) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict],
                 max_tokens: int = LLM_MAX_TOKENS,
                 temperature: float = LLM_TEMPERATURE) -> str:
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature,
                   "stream": False}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {self.api_key}"})
        last_error = ""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request,
                                            timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                if exc.code < 500:
                    raise LlmError(
                        f"大模型接口返回 {exc.code}：{detail}") from exc
                last_error = f"{exc.code}：{detail}"
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LlmError(f"大模型响应解析失败：{exc}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    ssl.SSLError) as exc:
                last_error = str(exc)
            else:
                return parse_completion(body)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
        raise LlmError(f"大模型请求失败：{last_error}")
