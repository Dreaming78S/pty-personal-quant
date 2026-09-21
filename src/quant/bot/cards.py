from __future__ import annotations

from quant.bot.qa import Answer

MAX_TITLE_CHARS = 80


def _card(title: str, elements: list[dict], template: str) -> dict:
    """组装飞书交互卡片骨架（宽屏、指定 header 颜色）。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": template},
        "elements": elements,
    }


def _md(content: str) -> dict:
    """Markdown 组件：支持列表/代码块（lark_md 文本元素不支持这些语法）。"""
    return {"tag": "markdown", "content": content}


def build_hint_card(text: str) -> dict:
    """一句提示（灰头），用于空问题等场景。"""
    return _card("【问数】", [_md(text)], "grey")


def build_answer_card(question: str, answer: Answer,
                      at_open_id: str | None = None) -> dict:
    """问答结果卡片：正文为模型总结，末尾附返回行数（不含 SQL）。"""
    title = f"【问数】{question.strip()}"
    if len(title) > MAX_TITLE_CHARS:
        title = f"{title[:MAX_TITLE_CHARS]}…"

    body = []
    if at_open_id:
        body.append(f"<at id={at_open_id}></at>")
    body.append(answer.text)

    evidence = f"返回 {answer.row_count} 行数据"
    if answer.note:
        evidence = f"{evidence} · {answer.note}"

    elements = [_md("\n".join(body)), {"tag": "hr"}, _md(evidence)]
    return _card(title, elements, "blue" if answer.ok else "grey")
