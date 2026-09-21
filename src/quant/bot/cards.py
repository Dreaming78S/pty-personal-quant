from __future__ import annotations

from quant.bot.qa import Answer, Table

MAX_TITLE_CHARS = 80
TABLE_PAGE_SIZE = 5


def _card(title: str, elements: list[dict], template: str) -> dict:
    """组装飞书交互卡片骨架（宽屏、指定 header 颜色）。

    不要添加 fallback 字段：手机飞书（实测 v8.0.2）遇到该字段会整张卡不显示。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": template},
        "elements": elements,
    }


def _md(content: str) -> dict:
    """Markdown 组件：支持列表/代码块（lark_md 文本元素不支持这些语法）。"""
    return {"tag": "markdown", "content": content}


def _number_format(values: list) -> dict:
    """整列都是整数时不留小数位，否则保留两位并加千分位。"""
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return {"precision": 2, "separator": True}
        if number != int(number):
            return {"precision": 2, "separator": True}
    return {"precision": 0, "separator": True}


def _table_element(table: Table) -> dict:
    """飞书表格组件（仅能挂在卡片根节点，每页 TABLE_PAGE_SIZE 行）。"""
    columns = []
    for column in table.columns:
        spec = {"name": column.key, "display_name": column.name,
                "data_type": "text", "width": "auto"}
        if column.kind == "number":
            spec["data_type"] = "number"
            spec["horizontal_align"] = "right"
            spec["format"] = _number_format(
                [row.get(column.key) for row in table.rows])
        columns.append(spec)
    rows = [{column.key: row.get(column.key, "") for column in table.columns}
            for row in table.rows]
    return {
        "tag": "table",
        "page_size": TABLE_PAGE_SIZE,
        "row_height": "low",
        "header_style": {"background_style": "grey", "text_color": "grey",
                         "bold": True, "lines": 1},
        "columns": columns,
        "rows": rows,
    }


def build_hint_card(text: str) -> dict:
    """一句提示（灰头），用于空问题等场景。"""
    return _card("【问数】", [_md(text)], "grey")


def build_answer_card(question: str, answer: Answer,
                      at_open_id: str | None = None) -> dict:
    """问答结果卡片：结论 + 数据表格（失败时不渲染表格），末尾附返回行数。"""
    title = f"【问数】{question.strip()}"
    if len(title) > MAX_TITLE_CHARS:
        title = f"{title[:MAX_TITLE_CHARS]}…"

    body = []
    if at_open_id:
        body.append(f"<at id={at_open_id}></at>")
    body.append(answer.text)

    table = answer.table if answer.ok else None
    elements = [_md("\n".join(body))]
    if table is not None and table.rows:
        elements.append(_table_element(table))
    return _card(title, elements, "blue" if answer.ok else "grey")
