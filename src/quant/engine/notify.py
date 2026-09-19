from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from typing import NamedTuple

import pandas as pd

from quant.config import get_settings
from quant.data import db, ingest, schemas
from quant.engine import hit_performance, loader
from quant.strategies.base import list_strategies

MAX_TEXT_CHARS = 20000
CO_MESSAGE_KEY = "多策略共振"
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0

logger = logging.getLogger(__name__)


class FeishuMessage(NamedTuple):
    title: str
    lines: tuple[str, ...]
    has_hits: bool
    template: str = "blue"


def resolve_strategies(strategy: str) -> list[str]:
    if strategy.strip().lower() in ("", "all"):
        return sorted(list_strategies())
    names = [part.strip() for part in strategy.split(",") if part.strip()]
    known = list_strategies()
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"未注册的策略：{', '.join(unknown)}")
    return names


def _display_date(date: str) -> str:
    return f"{date[:4]}-{date[4:6]}-{date[6:]}"


def _fmt(value, spec: str) -> str:
    if value is None or pd.isna(value):
        return "-"
    return format(float(value), spec)


def _format_stock(index: int, name, industry, raw_close) -> str:
    name_text = "-" if name is None or pd.isna(name) else f"**{name}**"
    industry_text = "-" if industry is None or pd.isna(industry) else industry
    return f"{index}. {name_text} {_fmt(raw_close, '.2f')} [{industry_text}]"


def _finish(title: str, lines: list[str], has_hits: bool,
            template: str = "blue") -> FeishuMessage:
    body = "\n".join(lines)
    if len(title) + len(body) <= MAX_TEXT_CHARS:
        return FeishuMessage(title, tuple(lines), has_hits, template)

    kept: list[str] = []
    for line in lines:
        kept.append(line)
        suffix = f"…（清单过长，仅显示前 {len(kept)} 只，共 {len(lines)} 只）"
        if len(title) + len("\n".join(kept)) + 1 + len(suffix) > MAX_TEXT_CHARS:
            kept.pop()
            break
    suffix = f"…（清单过长，仅显示前 {len(kept)} 只，共 {len(lines)} 只）"
    return FeishuMessage(title, tuple(kept + [suffix]), has_hits, template)


def format_message(strategy: str, trade_date: str, rows: pd.DataFrame) -> FeishuMessage:
    """单个策略的卡片消息：标题 + 正文行（名称/最新股价/行业），无命中单独一版。"""
    date = _display_date(trade_date)
    if rows.empty:
        return FeishuMessage(f"【{strategy}】{date}", ("今日无命中",), False)

    title = f"【{strategy}】{date} 命中 {len(rows)} 只"
    lines = [
        _format_stock(int(row["rank"]), row.get("name"),
                      row.get("industry"), row.get("raw_close"))
        for _, row in rows.iterrows()
    ]
    return _finish(title, lines, True)


def format_co_message(trade_date: str, hits: pd.DataFrame) -> FeishuMessage:
    """多策略共振卡片：当日被 ≥2 个策略命中的股票，按策略数、成交额降序。"""
    title = f"【{CO_MESSAGE_KEY}】{_display_date(trade_date)}"
    if hits.empty:
        return FeishuMessage(title, ("今日无共振",), False)

    events = hit_performance.group_co_hits(
        hits.assign(trade_date=trade_date), min_strategies=2)
    if events.empty:
        return FeishuMessage(title, ("今日无共振",), False)

    details = hits.drop_duplicates("ts_code").set_index("ts_code")
    events = events.copy()
    events["amount"] = events["ts_code"].map(details["amount"])
    events = events.sort_values(["n_strategies", "amount"],
                                ascending=[False, False])
    lines = []
    for index, (_, event) in enumerate(events.iterrows(), start=1):
        row = details.loc[event["ts_code"]]
        strategies = "、".join(event["strategies"])
        stock = _format_stock(index, row.get("name"),
                              row.get("industry"), row.get("raw_close"))
        lines.append(f"{stock} — {strategies}")
    return _finish(f"{title} 共 {len(events)} 只", lines, True,
                   template="orange")


def render_text(message: FeishuMessage) -> str:
    """纯文本预览（--dry-run），去掉加粗标记。"""
    body = "\n".join(line.replace("**", "") for line in message.lines)
    return f"{message.title}\n\n{body}"


def feishu_payload(message: FeishuMessage, secret: str | None = None,
                   timestamp: str | None = None) -> dict:
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": message.title},
            "template": message.template if message.has_hits else "grey",
        },
        "elements": [{
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(message.lines)},
        }],
    }
    payload: dict = {"msg_type": "interactive", "card": card}
    if secret:
        ts = timestamp or str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(string_to_sign.encode("utf-8"),
                          digestmod=hashlib.sha256).digest()
        payload["timestamp"] = ts
        payload["sign"] = base64.b64encode(digest).decode("utf-8")
    return payload


def _post_json(webhook_url: str, data: bytes) -> dict:
    request = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
    return json.loads(body.decode("utf-8"))


def send_message(webhook_url: str, message: FeishuMessage,
                 secret: str | None = None) -> None:
    """向飞书自定义机器人 webhook 发送交互卡片。

    网络类错误（握手超时等）自动重试 RETRY_ATTEMPTS 次、间隔 1 秒；
    业务错误（飞书返回 code!=0）不重试，直接抛 RuntimeError。
    """
    data = json.dumps(feishu_payload(message, secret),
                      ensure_ascii=False).encode("utf-8")
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            result = _post_json(webhook_url, data)
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                ssl.SSLError) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            logger.warning("飞书推送失败（第 %d/%d 次）：%s，%.0f 秒后重试",
                           attempt, RETRY_ATTEMPTS, exc, RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)
            continue
        if result.get("code") != 0:
            raise RuntimeError(
                f"飞书返回错误：code={result.get('code')} msg={result.get('msg')}")
        return


def _check_watermark(name: str, target: str) -> None:
    table = schemas.hit_table_name(name)
    watermark = ingest.get_watermark(table) or ""
    if watermark < target:
        raise ValueError(
            f"{table} 命中表未更新到 {target}（水位线 {watermark or '-'}），"
            f"请先执行 quant hits update -s {name}")


def build_messages(strategy: str = "all", date: str | None = None,
                   include_co: bool = True) -> dict[str, FeishuMessage]:
    """组装各策略消息以及共振卡片（不发送）；命中表未追平时直接报错。"""
    names = resolve_strategies(strategy)
    target = loader.resolve_trade_date(date)
    messages: dict[str, FeishuMessage] = {}
    for name in names:
        _check_watermark(name, target)
        table = schemas.hit_table_name(name)
        rows = db.read_df(
            f"SELECT name, industry, raw_close, `rank` "
            f"FROM `{table}` WHERE trade_date=%s ORDER BY `rank`", (target,))
        messages[name] = format_message(name, target, rows)
    if include_co:
        messages[CO_MESSAGE_KEY] = build_co_message(target)
    return messages


def build_co_message(date: str | None = None) -> FeishuMessage:
    """共振卡片：覆盖全部已注册策略，要求所有命中表都已追平目标日期。"""
    target = loader.resolve_trade_date(date)
    frames = []
    for name in sorted(list_strategies()):
        _check_watermark(name, target)
        table = schemas.hit_table_name(name)
        rows = db.read_df(
            f"SELECT ts_code, name, industry, raw_close, amount "
            f"FROM `{table}` WHERE trade_date=%s", (target,))
        if rows.empty:
            continue
        rows = rows.copy()
        rows["strategy"] = name
        frames.append(rows)
    hits = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return format_co_message(target, hits)


def notify_hits(strategy: str = "all", date: str | None = None,
                include_co: bool = True) -> dict[str, str]:
    """逐条推送飞书卡片；单条失败不中断，返回 {名称: 状态描述}。"""
    settings = get_settings()
    if not settings.feishu_webhook_url:
        raise ValueError("未配置 FEISHU_WEBHOOK_URL（.env），无法推送飞书")
    results: dict[str, str] = {}
    for name, message in build_messages(strategy, date, include_co).items():
        try:
            send_message(settings.feishu_webhook_url, message,
                         settings.feishu_webhook_secret)
            results[name] = "已发送"
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余策略
            results[name] = f"失败：{exc}"
    return results
