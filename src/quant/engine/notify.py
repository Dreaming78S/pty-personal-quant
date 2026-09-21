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
from quant.engine import hit_performance, loader, recommend
from quant.strategies.base import list_strategies

MAX_TEXT_CHARS = 20000
CO_MESSAGE_KEY = "多策略共振"
COMBINED_MESSAGE_KEY = "5日复合共振"
COMBINED_BASE_STRATEGY = "rise_shrink_pullback"
COMBINED_WINDOW_DAYS = 5
RECOMMEND_MESSAGE_KEY = "今日最终推荐"
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


def _display_md(date: str) -> str:
    return f"{date[4:6]}-{date[6:]}"


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
        messages[COMBINED_MESSAGE_KEY] = build_combined_message(target)
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


def format_combined_message(trade_date: str, window_dates: list[str],
                            rise_rows: pd.DataFrame,
                            other_hits: pd.DataFrame) -> FeishuMessage:
    """近 5 日复合共振：当日命中 rise_shrink_pullback 且窗口内命中过其他策略。

    rise_rows 为基准策略当日命中（ts_code/name/industry/raw_close/amount），
    other_hits 为窗口内其他策略命中明细（strategy/ts_code/trade_date）。
    """
    title = (f"【{COMBINED_MESSAGE_KEY}（当日含{COMBINED_BASE_STRATEGY}）】"
             f"{_display_date(trade_date)}")
    if rise_rows.empty or other_hits.empty:
        return FeishuMessage(title, ("今日无符合股票",), False)

    hits = other_hits[other_hits["ts_code"].isin(set(rise_rows["ts_code"]))].copy()
    if hits.empty:
        return FeishuMessage(title, ("今日无符合股票",), False)
    hits["trade_date"] = hits["trade_date"].astype(str)

    grouped: dict[str, dict[str, set[str]]] = {}
    for code, strategy, date in zip(hits["ts_code"], hits["strategy"],
                                    hits["trade_date"]):
        grouped.setdefault(code, {}).setdefault(strategy, set()).add(date)

    details = rise_rows.drop_duplicates("ts_code").set_index("ts_code")
    events = pd.DataFrame({
        "ts_code": list(grouped),
        "n_strategies": [len(by_strategy) for by_strategy in grouped.values()],
    })
    events["amount"] = events["ts_code"].map(details["amount"])
    events = events.sort_values(["n_strategies", "amount"],
                                ascending=[False, False])

    lines = [f"窗口：{_display_md(window_dates[0])} ~ "
             f"{_display_md(window_dates[-1])}（含当日）"]
    for index, (_, event) in enumerate(events.iterrows(), start=1):
        by_strategy = grouped[event["ts_code"]]
        parts = []
        for strategy in sorted(by_strategy,
                               key=lambda name: (min(by_strategy[name]), name)):
            dates = "、".join(_display_md(d)
                              for d in sorted(by_strategy[strategy]))
            parts.append(f"{strategy}({dates})")
        row = details.loc[event["ts_code"]]
        stock = _format_stock(index, row.get("name"), row.get("industry"),
                              row.get("raw_close"))
        lines.append(f"{stock} — {'、'.join(parts)}")
    return _finish(f"{title} 共 {len(events)} 只", lines, True,
                   template="purple")


def build_combined_message(date: str | None = None) -> FeishuMessage:
    """复合共振卡片：覆盖全部已注册策略，要求所有命中表都已追平目标日期。"""
    target = loader.resolve_trade_date(date)
    for name in sorted(list_strategies()):
        _check_watermark(name, target)

    dates = [str(d) for d in loader.open_trade_dates()]
    if target in dates:
        index = len(dates) - 1 - dates[::-1].index(target)
        window = dates[max(0, index - (COMBINED_WINDOW_DAYS - 1)): index + 1]
    else:
        window = [d for d in dates if d <= target][-COMBINED_WINDOW_DAYS:]
    if not window:
        window = [target]

    rise_rows = db.read_df(
        f"SELECT ts_code, name, industry, raw_close, amount "
        f"FROM `{schemas.hit_table_name(COMBINED_BASE_STRATEGY)}` "
        f"WHERE trade_date=%s", (target,))
    frames = []
    for name in sorted(list_strategies()):
        if name == COMBINED_BASE_STRATEGY:
            continue
        table = schemas.hit_table_name(name)
        rows = db.read_df(
            f"SELECT ts_code, trade_date FROM `{table}` "
            f"WHERE trade_date>=%s AND trade_date<=%s",
            (window[0], window[-1]))
        if rows.empty:
            continue
        rows = rows.copy()
        rows["strategy"] = name
        frames.append(rows)
    if frames:
        other_hits = pd.concat(frames, ignore_index=True)
    else:
        other_hits = pd.DataFrame(columns=["ts_code", "trade_date", "strategy"])
    return format_combined_message(target, window, rise_rows, other_hits)


def build_recommend_message(date: str | None = None,
                            config: recommend.RecommendConfig | None = None,
                            ignore_gate: bool = True
                            ) -> FeishuMessage:
    """今日最终推荐卡片：七个指数闸门状态 + 分层推荐 + 交易纪律一行。"""
    config = config or recommend.load_config()
    result = recommend.build_recommendations(date, config,
                                             ignore_gate=ignore_gate)
    date_text = _display_date(result.date)
    title = f"【{RECOMMEND_MESSAGE_KEY}】{date_text}"

    lines: list[str] = []
    statuses = result.index_statuses
    if statuses:
        open_count = sum(1 for s in statuses if s.above)
        lines.append(
            f"环境闸门：{open_count}/{len(statuses)} 个指数站上 "
            f"{config.gate_ma_days} 日均线"
        )
        arrow = lambda above: "↑" if above else "↓"
        for s in statuses:
            lines.append(
                f"• {s.name} {_fmt(s.close, '.2f')} / "
                f"MA{config.gate_ma_days} {_fmt(s.ma, '.2f')} {arrow(s.above)}"
            )
    else:
        # 兼容旧数据：只有单一参考指数时降级显示
        index_name = recommend.INDEX_NAMES_ZH.get(config.gate_index,
                                                   config.gate_index)
        if result.gate_open:
            lines.append(
                f"环境开：{index_name} 收盘 {_fmt(result.index_close, '.2f')} ≥ "
                f"{config.gate_ma_days} 日均线 {_fmt(result.index_ma, '.2f')}"
            )
        else:
            lines.append(
                f"今日不出手：{index_name} 收盘 {_fmt(result.index_close, '.2f')} "
                f"低于 {config.gate_ma_days} 日均线 "
                f"{_fmt(result.index_ma, '.2f')}"
            )

    picks = (*result.primary, *result.secondary)
    if not picks:
        lines.append("今日无符合推荐条件的股票")
        return FeishuMessage(title, tuple(lines), False, "grey")

    title = (f"{title} 重点 {len(result.primary)} · 备选 {len(result.secondary)}")
    for index, pick in enumerate(picks, start=1):
        stock = _format_stock(index, pick.name, pick.industry, pick.raw_close)
        detail = "、".join(f"{label}({rank})" for label, rank in pick.hits)
        lines.append(f"【{pick.tier}】{stock} — {detail}")
    lines.append("纪律：T+1 开盘买入，T+2 收盘卖出（最多持有到 T+3）")
    return _finish(title, lines, True)



def build_default_messages(date: str | None = None) -> dict[str, FeishuMessage]:
    """默认推送：今日最终推荐 + 多策略共振两张卡。"""
    target = loader.resolve_trade_date(date)
    return {
        RECOMMEND_MESSAGE_KEY: build_recommend_message(target),
        CO_MESSAGE_KEY: build_co_message(target),
    }


def notify_hits(strategy: str | None = None, date: str | None = None,
                include_co: bool = True) -> dict[str, str]:
    """逐条推送飞书卡片；单条失败不中断，返回 {名称: 状态描述}。

    strategy 为空时推默认两卡（今日最终推荐 + 多策略共振）；
    指定策略（或 all）时推各策略卡 + 共振卡（旧行为）。
    """
    settings = get_settings()
    if not settings.feishu_webhook_url:
        raise ValueError("未配置 FEISHU_WEBHOOK_URL（.env），无法推送飞书")
    if strategy in (None, ""):
        messages = build_default_messages(date)
    else:
        messages = build_messages(strategy, date, include_co)
    results: dict[str, str] = {}
    for name, message in messages.items():
        try:
            send_message(settings.feishu_webhook_url, message,
                         settings.feishu_webhook_secret)
            results[name] = "已发送"
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余策略
            results[name] = f"失败：{exc}"
    return results


