# 飞书问数机器人（text-to-SQL）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让飞书机器人通过长连接接收 @提问，用 DeepSeek 把自然语言翻译成只读 SQL、查库后总结成中文卡片回复。

**Architecture:** 新增 `src/quant/bot/` 包，按职责拆成 schema（表白名单提示词）、sql_guard（只读校验）、runner（只读事务执行）、llm（DeepSeek 客户端）、qa（编排）、cards（卡片渲染）、feishu（长连接与事件处理）七个单元；CLI 暴露 `quant bot serve`（常驻）与 `quant bot ask`（本地联调）。

**Tech Stack:** Python 3.12 / uv / pydantic-settings / pymysql / pandas / typer / `lark-oapi==1.7.3`（飞书长连接与消息回复）/ 标准库 `urllib`（DeepSeek，OpenAI 兼容）

**Spec:** `docs/superpowers/specs/2026-09-20-feishu-bot-nl2sql-design.md`

## Global Constraints

- 依赖：仅新增 `lark-oapi`（已 `uv add`，版本 1.7.3）；不得引入其他新依赖
- 模型：`deepseek-flash`，base_url `https://api.deepseek.com`，`temperature=0`，`max_tokens=4096`（推理模型，`content` 可能为空需回退 `reasoning_content`）
- 限额常量：`MAX_ROWS=200`、`MAX_RESULT_CHARS=8000`、`SQL_TIMEOUT_SECONDS=15`、`LLM_TIMEOUT_SECONDS=60`
- 白名单 = `schemas.TABLES` ∪ `schemas.HIT_TABLES`（共 20 张，含 `ingest_log`）
- 安全：单条语句、仅 `SELECT`/`WITH`、关键字黑名单、表名白名单、`START TRANSACTION READ ONLY`
- 所有面向用户文本用中文；源文件 UTF-8 无 BOM
- 提交信息前缀：`feat:` / `fix:` / `test:` / `docs:` / `chore:`；禁止 `git add .`，只暂存本任务涉及文件
- 测试：`uv run pytest -q`；单测不得联网、不得连库（需要真库的用 `@pytest.mark.integration`）
- 每步都要先看到失败、再看到通过（TDD）

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/quant/bot/__init__.py` | 空包标记 |
| `src/quant/bot/schema.py` | 表白名单 + 喂给模型的 DDL 文本（进程内缓存） |
| `src/quant/bot/sql_guard.py` | `validate_sql()` 只读校验与 `LIMIT` 规范化 |
| `src/quant/bot/runner.py` | `run_readonly()` 只读事务执行查询 |
| `src/quant/bot/llm.py` | `DeepSeekClient` 与响应解析 |
| `src/quant/bot/qa.py` | `answer()` 编排、提示词、结果渲染与截断 |
| `src/quant/bot/cards.py` | 交互卡片渲染 |
| `src/quant/bot/feishu.py` | 事件解析、去重、回复、`run_bot()` |
| `src/quant/cli.py` | 新增 `bot` 子命令组 |
| `src/quant/config.py`、`.env.example`、`.env` | 新增 5 个配置项 |
| `tests/test_bot_*.py`、`tests/test_cli_bot.py`、`tests/test_config.py` | 单测 |

说明：spec 原写"从 `information_schema` 读列拼 DDL"，实现改为直接用 `schemas` 里的 DDL 常量——同样是代码内的权威结构，且无 DB 依赖、不会与迁移漂移。

---

### Task 1: 配置项与依赖

**Files:**
- Modify: `src/quant/config.py`
- Modify: `.env.example`
- Modify: `.env`（本地文件，不入库）
- Modify: `tests/test_config.py`
- Modify: `pyproject.toml`、`uv.lock`（`uv add lark-oapi` 已执行，一并提交）

**Interfaces:**
- Produces: `Settings.feishu_app_id`、`Settings.feishu_app_secret`、`Settings.deepseek_api_key`、`Settings.deepseek_base_url`（默认 `https://api.deepseek.com`）、`Settings.deepseek_model`（默认 `deepseek-flash`），后续所有任务通过 `get_settings()` 读取

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_config.py`：

```python
def _set_required(monkeypatch):
    monkeypatch.setenv("ALIYUN_RDS_HOST", "db")
    monkeypatch.setenv("ALIYUN_RDS_USER", "u")
    monkeypatch.setenv("ALIYUN_RDS_PASSPORT", "p")
    monkeypatch.setenv("ALIYUN_RDS_DATABASE", "d")
    monkeypatch.setenv("TUSHARE_TOKEN", "t")


def test_settings_reads_bot_env(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    s = Settings(_env_file=None)

    assert s.feishu_app_id == "cli_x"
    assert s.feishu_app_secret == "sec"
    assert s.deepseek_api_key == "sk-x"
    assert s.deepseek_base_url == "https://api.deepseek.com"
    assert s.deepseek_model == "deepseek-flash"


def test_settings_bot_keys_optional(monkeypatch):
    _set_required(monkeypatch)
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)

    assert s.feishu_app_id is None
    assert s.deepseek_api_key is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL —— `AttributeError: 'Settings' object has no attribute 'feishu_app_id'`

- [ ] **Step 3: 实现配置字段**

`src/quant/config.py` 中 `Settings` 追加（放在 `feishu_webhook_secret` 之后）：

```python
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
```

`.env.example` 末尾追加：

```
# 飞书应用（长连接问数机器人）
feishu_app_id=
feishu_app_secret=
# DeepSeek（问数机器人）
deepseek_api_key=
deepseek_base_url=https://api.deepseek.com
deepseek_model=deepseek-flash
```

`.env` 末尾追加真实值（`feishu_app_id` / `feishu_app_secret` / `deepseek_api_key` 用用户提供的凭据，`deepseek_base_url` 与 `deepseek_model` 同 `.env.example`）。`.env` 已在 `.gitignore` 中，切勿提交。

> 注：本机 `.env` 已由协调者预先填好这三项真实凭据，实施者只需确认存在，**不要读取或回显其值**。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_config.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/quant/config.py tests/test_config.py .env.example pyproject.toml uv.lock
git commit -m "feat: add bot config keys for feishu app and deepseek"
```

---

### Task 2: 表白名单提示词 `bot/schema.py`

**Files:**
- Create: `src/quant/bot/__init__.py`（空文件）
- Create: `src/quant/bot/schema.py`
- Test: `tests/test_bot_schema.py`

**Interfaces:**
- Consumes: `quant.data.schemas.TABLES` / `HIT_TABLES`
- Produces: `allowed_tables() -> list[str]`、`schema_prompt() -> str`（Task 3 用它作为 `validate_sql` 的默认白名单，Task 6 用它拼提示词）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_schema.py`：

```python
from quant.bot import schema
from quant.data import schemas


def test_allowed_tables_covers_all_schema_tables():
    names = schema.allowed_tables()

    assert names == sorted({*schemas.TABLES, *schemas.HIT_TABLES})
    assert "ingest_log" in names
    assert "hit_rise_shrink_pullback" in names
    assert len(names) == 20


def test_schema_prompt_contains_every_table_and_columns():
    text = schema.schema_prompt()

    for name in schema.allowed_tables():
        assert f"### {name}" in text
    assert "trade_date" in text
    assert "raw_close" in text
    assert "不复权收盘价" in text


def test_schema_prompt_is_cached():
    assert schema.schema_prompt() is schema.schema_prompt()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_schema.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot'`

- [ ] **Step 3: 实现**

`src/quant/bot/__init__.py` 建为空文件。

`src/quant/bot/schema.py`：

```python
from __future__ import annotations

from functools import lru_cache

from quant.data import schemas


def allowed_tables() -> list[str]:
    """机器人可查询的表白名单（随 schemas 自动同步）。"""
    return sorted({*schemas.TABLES, *schemas.HIT_TABLES})


def _ddl_of(table: str) -> str:
    spec = schemas.TABLES.get(table) or schemas.HIT_TABLES[table]
    return spec.ddl.strip()


@lru_cache(maxsize=1)
def schema_prompt() -> str:
    """喂给大模型的表白名单与建表语句（含列注释）。"""
    return "\n\n".join(f"### {table}\n{_ddl_of(table)}"
                       for table in allowed_tables())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_schema.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add src/quant/bot/__init__.py src/quant/bot/schema.py tests/test_bot_schema.py
git commit -m "feat: add bot schema whitelist prompt"
```

---

### Task 3: 只读 SQL 校验 `bot/sql_guard.py`

**Files:**
- Create: `src/quant/bot/sql_guard.py`
- Test: `tests/test_bot_sql_guard.py`

**Interfaces:**
- Consumes: `quant.bot.schema.allowed_tables()`
- Produces: `SqlRejected(ValueError)`、`validate_sql(sql, allowed=None, max_rows=200) -> str`（返回可执行 SQL，Task 6 调用）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_sql_guard.py`：

```python
import pytest

from quant.bot.sql_guard import SqlRejected, validate_sql

ALLOWED = {"daily", "stock_basic", "hit_ma_volume"}


def test_validate_appends_limit():
    assert validate_sql("SELECT ts_code FROM daily",
                        allowed=ALLOWED) == "SELECT ts_code FROM daily LIMIT 200"


def test_validate_keeps_smaller_limit():
    sql = validate_sql("SELECT ts_code FROM daily LIMIT 10", allowed=ALLOWED)

    assert sql.endswith("LIMIT 10")


def test_validate_tightens_larger_limit():
    sql = validate_sql("SELECT ts_code FROM daily LIMIT 5000", allowed=ALLOWED)

    assert sql.endswith("LIMIT 200")


def test_validate_accepts_with_cte_and_join():
    sql = ("WITH t AS (SELECT ts_code FROM hit_ma_volume) "
           "SELECT b.name FROM t JOIN stock_basic b ON b.ts_code = t.ts_code")

    result = validate_sql(sql, allowed=ALLOWED)

    assert result.endswith("LIMIT 200")


def test_validate_strips_comments_and_trailing_semicolon():
    assert validate_sql("SELECT 1 FROM daily -- 说明\n",
                        allowed=ALLOWED).endswith("LIMIT 200")
    assert validate_sql("SELECT 1 FROM daily;",
                        allowed=ALLOWED).endswith("LIMIT 200")


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM daily; DROP TABLE daily",
    "UPDATE daily SET close = 1",
    "DELETE FROM daily",
    "DROP TABLE daily",
    "CREATE TABLE x (a INT)",
    "INSERT INTO daily (ts_code) VALUES ('x')",
    "SELECT * FROM daily INTO OUTFILE '/tmp/x'",
    "SELECT SLEEP(10)",
    "SELECT * FROM information_schema.tables",
    "",
])
def test_validate_rejects_dangerous_sql(sql):
    with pytest.raises(SqlRejected):
        validate_sql(sql, allowed=ALLOWED)


def test_validate_rejects_unknown_table():
    with pytest.raises(SqlRejected, match="不允许查询的表"):
        validate_sql("SELECT * FROM mysql_user", allowed=ALLOWED)


def test_validate_uses_schema_whitelist_by_default():
    with pytest.raises(SqlRejected):
        validate_sql("SELECT * FROM some_other_table")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_sql_guard.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot.sql_guard'`

- [ ] **Step 3: 实现**

`src/quant/bot/sql_guard.py`：

```python
from __future__ import annotations

import re

from quant.bot import schema

MAX_ROWS = 200

BANNED_KEYWORDS = (
    "insert", "update", "delete", "replace", "drop", "alter", "create",
    "truncate", "rename", "grant", "revoke", "set", "call", "load",
    "outfile", "dumpfile", "sleep", "benchmark", "get_lock", "into",
)

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"--[^\n]*")
_SELECT_START = re.compile(r"(?is)^(select|with)\b")
_BANNED = re.compile(r"\b(?:" + "|".join(BANNED_KEYWORDS) + r")\b", re.I)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.I)
_CTE_NAME = re.compile(r"(?i)(?:\bwith\b|,)\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s+as\s*\(")
_LIMIT = re.compile(r"\blimit\s+(\d+)(?:\s+offset\s+\d+)?\s*$", re.I)


class SqlRejected(ValueError):
    """SQL 未通过只读校验。"""


def _strip_comments(sql: str) -> str:
    return _COMMENT_LINE.sub(" ", _COMMENT_BLOCK.sub(" ", sql))


def _referenced_tables(body: str) -> set[str]:
    cte_names = {name.lower() for name in _CTE_NAME.findall(body)}
    return {name.lower() for name in _TABLE_REF.findall(body)} - cte_names


def validate_sql(sql: str, allowed: set[str] | None = None,
                 max_rows: int = MAX_ROWS) -> str:
    """校验只读 SQL 并规范化 LIMIT；不合法时抛 SqlRejected。"""
    body = _strip_comments(sql or "").strip()
    while body.endswith(";"):
        body = body[:-1].strip()
    if not body:
        raise SqlRejected("SQL 为空")
    if ";" in body:
        raise SqlRejected("只允许单条语句")
    if not _SELECT_START.match(body):
        raise SqlRejected("只允许 SELECT / WITH 查询")
    banned = _BANNED.search(body)
    if banned:
        raise SqlRejected(f"不允许的关键字：{banned.group(0).upper()}")
    names = _referenced_tables(body)
    unknown = sorted(names - (allowed or set(schema.allowed_tables())))
    if unknown:
        raise SqlRejected(f"不允许查询的表：{', '.join(unknown)}")

    match = _LIMIT.search(body)
    if match is None:
        return f"{body} LIMIT {max_rows}"
    if int(match.group(1)) > max_rows:
        return f"{body[:match.start()]}LIMIT {max_rows}".strip()
    return body
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_sql_guard.py -q`
Expected: PASS（17 passed）

- [ ] **Step 5: 提交**

```bash
git add src/quant/bot/sql_guard.py tests/test_bot_sql_guard.py
git commit -m "feat: add read-only sql guard for bot"
```

---

### Task 4: 只读执行器 `bot/runner.py`

**Files:**
- Create: `src/quant/bot/runner.py`
- Test: `tests/test_bot_runner.py`

**Interfaces:**
- Consumes: `quant.config.get_settings()`
- Produces: `run_readonly(sql, timeout=15) -> pandas.DataFrame`（Task 6 通过依赖注入使用，签名必须是 `Callable[[str], pd.DataFrame]`）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_runner.py`：

```python
import pandas as pd
import pytest

from quant.bot import runner


class FakeCursor:
    def __init__(self, rows):
        self.statements = []
        self._rows = rows

    def execute(self, sql, params=None):
        self.statements.append(sql)

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)
        self.rolled_back = False
        self.closed = False
        self.connect_kwargs = {}

    def cursor(self, *args, **kwargs):
        return self.cursor_obj

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_run_readonly_uses_read_only_transaction(monkeypatch):
    fake = FakeConnection([{"ts_code": "600360.SH"}])
    monkeypatch.setattr(
        runner.pymysql, "connect",
        lambda **kwargs: (fake.connect_kwargs.update(kwargs), fake)[1])

    frame = runner.run_readonly("SELECT ts_code FROM daily LIMIT 1")

    assert fake.cursor_obj.statements == [
        "START TRANSACTION READ ONLY", "SELECT ts_code FROM daily LIMIT 1"]
    assert list(frame.columns) == ["ts_code"]
    assert fake.connect_kwargs["read_timeout"] == 15
    assert fake.connect_kwargs["connect_timeout"] == 15
    assert fake.rolled_back is True
    assert fake.closed is True


def test_run_readonly_returns_empty_frame(monkeypatch):
    fake = FakeConnection([])
    monkeypatch.setattr(runner.pymysql, "connect", lambda **kwargs: fake)

    frame = runner.run_readonly("SELECT ts_code FROM daily LIMIT 1")

    assert frame.empty


def test_run_readonly_closes_connection_on_error(monkeypatch):
    class BoomCursor(FakeCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith("SELECT"):
                raise RuntimeError("boom")

    fake = FakeConnection([])
    fake.cursor_obj = BoomCursor([])
    monkeypatch.setattr(runner.pymysql, "connect", lambda **kwargs: fake)

    with pytest.raises(RuntimeError, match="boom"):
        runner.run_readonly("SELECT 1")

    assert fake.closed is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_runner.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot.runner'`

- [ ] **Step 3: 实现**

`src/quant/bot/runner.py`：

```python
from __future__ import annotations

import pandas as pd
import pymysql

from quant.config import get_settings

SQL_TIMEOUT_SECONDS = 15


def run_readonly(sql: str, timeout: int = SQL_TIMEOUT_SECONDS) -> pd.DataFrame:
    """在只读事务中执行单条 SELECT，返回结果 DataFrame（报错/超时直接抛出）。"""
    settings = get_settings()
    conn = pymysql.connect(
        host=settings.aliyun_rds_host,
        port=settings.aliyun_rds_port,
        user=settings.aliyun_rds_user,
        password=settings.aliyun_rds_passport,
        database=settings.aliyun_rds_database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=timeout,
        read_timeout=timeout,
        write_timeout=timeout,
    )
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("START TRANSACTION READ ONLY")
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.rollback()
        conn.close()
    return pd.DataFrame(rows)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_runner.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 真库冒烟（一次性验证只读事务可用）**

Run: `uv run python -c "from quant.bot.runner import run_readonly; print(run_readonly('SELECT COUNT(*) AS n FROM trade_cal LIMIT 1'))"`
Expected: 打印一行 `n` 的计数，无异常

- [ ] **Step 6: 提交**

```bash
git add src/quant/bot/runner.py tests/test_bot_runner.py
git commit -m "feat: add read-only query runner for bot"
```

---

### Task 5: DeepSeek 客户端 `bot/llm.py`

**Files:**
- Create: `src/quant/bot/llm.py`
- Test: `tests/test_bot_llm.py`

**Interfaces:**
- Produces: `LlmError(RuntimeError)`、`parse_completion(payload: dict) -> str`、`DeepSeekClient(api_key, base_url, model, timeout).complete(messages, max_tokens=4096, temperature=0.0) -> str`（Task 6、Task 9 使用）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_llm.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_llm.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot.llm'`

- [ ] **Step 3: 实现**

`src/quant/bot/llm.py`：

```python
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
    text = (message.get("content") or "").strip()
    if not text:
        text = (message.get("reasoning_content") or "").strip()
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
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    ssl.SSLError) as exc:
                last_error = str(exc)
            else:
                return parse_completion(body)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
        raise LlmError(f"大模型请求失败：{last_error}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_llm.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 真实接口冒烟**

Run: `uv run python -c "from quant.config import get_settings; from quant.bot.llm import DeepSeekClient; s=get_settings(); print(DeepSeekClient(s.deepseek_api_key, s.deepseek_base_url, s.deepseek_model).complete([{'role':'user','content':'只回复：收到'}], max_tokens=200))"`
Expected: 打印 `收到`（证明 `.env` 里的 key 与模型名可用）

- [ ] **Step 6: 提交**

```bash
git add src/quant/bot/llm.py tests/test_bot_llm.py
git commit -m "feat: add deepseek client for bot"
```

---

### Task 6: 问答编排 `bot/qa.py`

**Files:**
- Create: `src/quant/bot/qa.py`
- Test: `tests/test_bot_qa.py`

**Interfaces:**
- Consumes: `schema.schema_prompt()`、`sql_guard.validate_sql`、`SqlRejected`、`llm.LlmError`
- Produces: `Answer(text, sql, row_count, ok, note)`、`answer(question, llm, runner, schema_text=None) -> Answer`、`parse_sql_json(text) -> str`、`render_rows(rows, max_rows=200, max_chars=8000) -> str`、`build_sql_messages(question, schema_text) -> list[dict]`、`build_summary_messages(question, sql, rows_text) -> list[dict]`（Task 7、8、9 使用）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_qa.py`：

```python
import pandas as pd
import pytest

from quant.bot import qa
from quant.bot.llm import LlmError
from quant.bot.sql_guard import SqlRejected


class ScriptedLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, max_tokens=qa.MAX_ROWS, temperature=0.0):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def fake_runner(rows):
    def run(sql):
        return rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    return run


def test_parse_sql_json_handles_fences_and_noise():
    assert qa.parse_sql_json('```json\n{"sql": "SELECT 1"}\n```') == "SELECT 1"
    assert qa.parse_sql_json('好的：{"sql": "SELECT 2"} 以上') == "SELECT 2"


def test_parse_sql_json_rejects_missing_sql():
    with pytest.raises(SqlRejected, match="没有返回 JSON"):
        qa.parse_sql_json("我无法回答")
    with pytest.raises(SqlRejected, match="没有给出 SQL"):
        qa.parse_sql_json('{"explain": "无"}')


def test_render_rows_truncates_by_rows():
    frame = pd.DataFrame({"a": range(5)})

    text = qa.render_rows(frame, max_rows=3)

    assert "仅显示前 3 行" in text
    assert len(text.splitlines()) == 5


def test_render_rows_handles_empty():
    assert qa.render_rows(pd.DataFrame()) == "（无数据行）"


def test_answer_happy_path():
    llm = ScriptedLlm(['{"sql": "SELECT name FROM hit_ma_volume"}', "博敏电子命中 2 次"])
    rows = pd.DataFrame({"name": ["博敏电子", "博敏电子"]})

    result = qa.answer("博敏电子命中情况", llm, fake_runner(rows))

    assert result.ok is True
    assert result.text == "博敏电子命中 2 次"
    assert result.row_count == 2
    assert result.sql == "SELECT name FROM hit_ma_volume LIMIT 200"
    assert len(llm.calls) == 2


def test_answer_rejects_unsafe_sql():
    llm = ScriptedLlm(['{"sql": "DELETE FROM hit_ma_volume"}'])

    result = qa.answer("删掉数据", llm, fake_runner([]))

    assert result.ok is False
    assert "拒绝执行" in result.text
    assert result.sql == "DELETE FROM hit_ma_volume"
    assert len(llm.calls) == 1


def test_answer_reports_llm_failure():
    llm = ScriptedLlm([LlmError("超时")])

    result = qa.answer("问题", llm, fake_runner([]))

    assert result.ok is False
    assert "大模型暂时不可用" in result.text
    assert result.note == "超时"


def test_answer_reports_query_failure():
    def boom(sql):
        raise RuntimeError("Table doesn't exist")

    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}'])

    result = qa.answer("问题", llm, boom)

    assert result.ok is False
    assert "查询执行失败" in result.text


def test_answer_reports_empty_result_without_second_call():
    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}'])

    result = qa.answer("问题", llm, fake_runner([]))

    assert result.ok is True
    assert result.text == "没有查到数据。"
    assert len(llm.calls) == 1


def test_answer_reports_summary_failure():
    llm = ScriptedLlm(['{"sql": "SELECT 1 FROM hit_ma_volume"}', LlmError("boom")])

    result = qa.answer("问题", llm, fake_runner([{"a": 1}]))

    assert result.ok is False
    assert "总结失败" in result.text
    assert result.row_count == 1


def test_build_sql_messages_includes_schema_and_question():
    messages = qa.build_sql_messages("博敏电子", "### daily\nCREATE TABLE ...")

    assert messages[0]["role"] == "system"
    assert "只读" in messages[0]["content"]
    assert "### daily" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "博敏电子"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_qa.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot.qa'`

- [ ] **Step 3: 实现**

`src/quant/bot/qa.py`：

```python
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from quant.bot import schema
from quant.bot.llm import LlmError
from quant.bot.sql_guard import SqlRejected, validate_sql

MAX_ROWS = 200
MAX_RESULT_CHARS = 8000

SQL_SYSTEM_PROMPT = """你是 A 股量化数据库的 SQL 助手。根据用户问题写一条只读 SELECT 查询。

规则：
- 只使用下面给出的表，只输出一条 SELECT/WITH 语句，不要分号，不要写操作与 DDL。
- 日期列是 CHAR(8) 字符串，格式 YYYYMMDD；"最近几天"用 trade_date >= 'YYYYMMDD' 之类条件。
- 最新交易日：SELECT MAX(cal_date) FROM trade_cal WHERE is_open = 1 AND cal_date <= '今天'。
- 股票用 ts_code 或 name 匹配；不确定代码时用 stock_basic.name LIKE '%关键词%'。
- 口径：daily.amount 为千元且不复权；hit_* 表 close 为后复权、raw_close 为不复权，
  name/industry 为入库快照；命中表水位线在 ingest_log（task_name = 'hit_<策略>'）。
- 只输出 JSON：{"sql": "...", "explain": "一句话说明"}"""

SUMMARY_SYSTEM_PROMPT = """你是 A 股量化助手。根据查询结果用简洁中文回答用户问题。

要求：先给结论，再列关键数据（日期、策略、名称、数值）；不要编造结果里没有的数据；
结果为空就直说没有查到；不要输出与问题无关的推测。"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass(frozen=True)
class Answer:
    text: str
    sql: str = ""
    row_count: int = 0
    ok: bool = True
    note: str = ""


def parse_sql_json(text: str) -> str:
    """从模型输出里取出 SQL（容忍 ``` 围栏与前后说明文字）。"""
    candidate = (text or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    payload = None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise SqlRejected("模型没有返回 JSON") from None
        try:
            payload = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc:
            raise SqlRejected("模型返回的 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise SqlRejected("模型返回的 JSON 不是对象")
    sql = str(payload.get("sql") or "").strip()
    if not sql:
        raise SqlRejected("模型没有给出 SQL")
    return sql


def render_rows(rows: pd.DataFrame, max_rows: int = MAX_ROWS,
                max_chars: int = MAX_RESULT_CHARS) -> str:
    """把结果渲染成紧凑文本（列名 + 制表符分隔行），超限截断并注明。"""
    if rows.empty:
        return "（无数据行）"
    frame = rows.head(max_rows)
    lines = ["\t".join(str(column) for column in frame.columns)]
    for _, row in frame.iterrows():
        lines.append("\t".join("" if pd.isna(value) else str(value)
                               for value in row))
    text = "\n".join(lines)
    if len(text) > max_chars:
        return f"{text[:max_chars]}\n…（结果过长已截断）"
    if len(rows) > max_rows:
        return f"{text}\n…（仅显示前 {max_rows} 行，共 {len(rows)} 行）"
    return text


def build_sql_messages(question: str, schema_text: str) -> list[dict]:
    return [{"role": "system",
             "content": f"{SQL_SYSTEM_PROMPT}\n\n可用表：\n{schema_text}"},
            {"role": "user", "content": question}]


def build_summary_messages(question: str, sql: str, rows_text: str) -> list[dict]:
    return [{"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user",
             "content": f"用户问题：{question}\n\nSQL：{sql}\n\n"
                        f"查询结果（制表符分隔）：\n{rows_text}"}]


def answer(question: str, llm, runner: Callable[[str], pd.DataFrame],
           schema_text: str | None = None) -> Answer:
    """生成 SQL → 只读校验 → 执行 → 总结；任何一步失败都返回带原因的 Answer。"""
    schema_text = schema_text or schema.schema_prompt()
    try:
        raw = llm.complete(build_sql_messages(question, schema_text))
    except LlmError as exc:
        return Answer(text="大模型暂时不可用，请稍后再问。", ok=False,
                      note=str(exc))

    try:
        sql = parse_sql_json(raw)
    except SqlRejected as exc:
        return Answer(text=f"没能生成可执行的查询：{exc}", ok=False,
                      note=raw[:200])

    try:
        safe_sql = validate_sql(sql)
    except SqlRejected as exc:
        return Answer(text=f"拒绝执行：{exc}", sql=sql, ok=False)

    try:
        rows = runner(safe_sql)
    except Exception as exc:  # noqa: BLE001 - 查询失败要回给用户而不是崩掉进程
        return Answer(text=f"查询执行失败：{exc}", sql=safe_sql, ok=False)

    if rows.empty:
        return Answer(text="没有查到数据。", sql=safe_sql, ok=True)

    rows_text = render_rows(rows)
    try:
        summary = llm.complete(build_summary_messages(question, safe_sql, rows_text))
    except LlmError as exc:
        return Answer(text=f"已查到 {len(rows)} 行数据，但总结失败：{exc}",
                      sql=safe_sql, row_count=len(rows), ok=False,
                      note=rows_text[:200])
    return Answer(text=summary, sql=safe_sql, row_count=len(rows), ok=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_qa.py -q`
Expected: PASS（11 passed）

- [ ] **Step 5: 本地真实链路冒烟（不连飞书）**

Run: `uv run python -c "from quant.config import get_settings; from quant.bot.llm import DeepSeekClient; from quant.bot.runner import run_readonly; from quant.bot import qa; s=get_settings(); a=qa.answer('博敏电子最近几天命中策略的情况是怎么样的', DeepSeekClient(s.deepseek_api_key, s.deepseek_base_url, s.deepseek_model), run_readonly); print(a.text); print('SQL:', a.sql); print('rows:', a.row_count, 'ok:', a.ok)"`
Expected: 打印一段中文回答 + 生成的 SQL + 行数，`ok: True`

- [ ] **Step 6: 提交**

```bash
git add src/quant/bot/qa.py tests/test_bot_qa.py
git commit -m "feat: add text-to-sql qa pipeline"
```

---

### Task 7: 卡片渲染 `bot/cards.py`

**Files:**
- Create: `src/quant/bot/cards.py`
- Test: `tests/test_bot_cards.py`

**Interfaces:**
- Consumes: `qa.Answer`
- Produces: `build_answer_card(question, answer, at_open_id=None) -> dict`、`build_hint_card(text) -> dict`（Task 8 使用）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_cards.py`：

```python
import json

from quant.bot import cards
from quant.bot.qa import Answer


def _content(card, index=0):
    return card["elements"][index]["text"]["content"]


def test_build_answer_card_has_title_body_and_sql():
    answer = Answer("博敏电子近 3 日命中 2 次", "SELECT 1 LIMIT 200", 2, True)

    card = cards.build_answer_card("博敏电子最近几天命中策略的情况", answer)

    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "【问数】博敏电子最近几天命中策略的情况"
    assert _content(card) == "博敏电子近 3 日命中 2 次"
    assert "返回 2 行" in _content(card, 2)
    assert _content(card, 3) == "```sql\nSELECT 1 LIMIT 200\n```"
    json.dumps(card, ensure_ascii=False)


def test_build_answer_card_uses_grey_on_failure():
    answer = Answer("拒绝执行：只允许 SELECT / WITH 查询",
                    "DELETE FROM daily", 0, False)

    card = cards.build_answer_card("问题", answer)

    assert card["header"]["template"] == "grey"
    assert _content(card, 2) == "查询依据：返回 0 行"


def test_build_answer_card_mentions_sender_in_group():
    answer = Answer("命中 1 次", "SELECT 1", 1, True)

    card = cards.build_answer_card("问题", answer, at_open_id="ou_123")

    assert _content(card).startswith("<at id=ou_123></at>\n")


def test_build_answer_card_truncates_long_question():
    card = cards.build_answer_card("问" * 200, Answer("答"))

    assert len(card["header"]["title"]["content"]) <= cards.MAX_TITLE_CHARS + 1


def test_build_answer_card_includes_note():
    answer = Answer("失败", ok=False, note="大模型超时")

    card = cards.build_answer_card("问题", answer)

    assert "大模型超时" in _content(card, 2)


def test_build_hint_card_is_grey():
    card = cards.build_hint_card("请把问题写在 @我 之后")

    assert card["header"]["template"] == "grey"
    assert _content(card) == "请把问题写在 @我 之后"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_cards.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot.cards'`

- [ ] **Step 3: 实现**

`src/quant/bot/cards.py`：

```python
from __future__ import annotations

from quant.bot.qa import Answer

MAX_TITLE_CHARS = 80


def _card(title: str, elements: list[dict], template: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title},
                   "template": template},
        "elements": elements,
    }


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def build_hint_card(text: str) -> dict:
    """一句提示（灰头），用于空问题等场景。"""
    return _card("【问数】", [_div(text)], "grey")


def build_answer_card(question: str, answer: Answer,
                      at_open_id: str | None = None) -> dict:
    """问答结果卡片：正文为总结，末尾附行数与 SQL。"""
    title = f"【问数】{question.strip()}"
    if len(title) > MAX_TITLE_CHARS:
        title = f"{title[:MAX_TITLE_CHARS]}…"

    body = []
    if at_open_id:
        body.append(f"<at id={at_open_id}></at>")
    body.append(answer.text)

    evidence = f"查询依据：返回 {answer.row_count} 行"
    if answer.note:
        evidence = f"{evidence} · {answer.note}"

    elements = [_div("\n".join(body)), {"tag": "hr"}, _div(evidence)]
    if answer.sql:
        elements.append(_div(f"```sql\n{answer.sql}\n```"))
    return _card(title, elements, "blue" if answer.ok else "grey")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_cards.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/quant/bot/cards.py tests/test_bot_cards.py
git commit -m "feat: add feishu answer cards for bot"
```

---

### Task 8: 长连接服务 `bot/feishu.py`

**Files:**
- Create: `src/quant/bot/feishu.py`
- Test: `tests/test_bot_feishu.py`

**Interfaces:**
- Consumes: `cards.build_answer_card`、`cards.build_hint_card`、`qa.answer`、`llm.DeepSeekClient`、`runner.run_readonly`、`get_settings()`
- Produces: `Question`、`strip_mentions(text) -> str`、`parse_event(data) -> Question | None`、`reply_card(client, message_id, card) -> None`、`BotService(client, llm, runner=run_readonly, reply=reply_card, workers=2)`（含 `handle_event(data)`）、`run_bot() -> None`（Task 9 使用）

- [ ] **Step 1: 写失败测试**

`tests/test_bot_feishu.py`：

```python
import json

import pytest
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from quant.bot import feishu
from quant.bot.qa import Answer


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_bot_feishu.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'quant.bot.feishu'`

- [ ] **Step 3: 实现**

`src/quant/bot/feishu.py`：

```python
from __future__ import annotations

import json
import logging
import re
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
    """解析消息事件；非文本消息或机器人自己发的消息返回 None。"""
    event = getattr(data, "event", None)
    if event is None or event.message is None or event.sender is None:
        return None
    if event.sender.sender_type == "app":
        return None
    message = event.message
    if message.message_type != "text":
        return None
    try:
        text = json.loads(message.content or "{}").get("text", "")
    except json.JSONDecodeError:
        text = ""
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

    def _is_new(self, message_id: str) -> bool:
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_bot_feishu.py -q`
Expected: PASS（12 passed）

- [ ] **Step 5: 提交**

```bash
git add src/quant/bot/feishu.py tests/test_bot_feishu.py
git commit -m "feat: add feishu long-connection bot service"
```

---

### Task 9: CLI 命令 `quant bot serve` / `quant bot ask`

**Files:**
- Modify: `src/quant/cli.py`
- Test: `tests/test_cli_bot.py`

**Interfaces:**
- Consumes: `feishu.run_bot()`、`qa.answer()`、`DeepSeekClient`、`run_readonly`、`get_settings()`
- Produces: `quant bot serve`、`quant bot ask "<问题>"`（Task 10 的手动验收用）

- [ ] **Step 1: 写失败测试**

`tests/test_cli_bot.py`：

```python
from types import SimpleNamespace

from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_bot_ask_prints_answer_and_sql(monkeypatch):
    from quant.bot.qa import Answer

    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        deepseek_api_key="sk-x", deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash"))
    monkeypatch.setattr("quant.bot.qa.answer",
                        lambda question, llm, runner_: Answer(
                            "博敏电子近 3 日命中 2 次", "SELECT 1 LIMIT 200", 2, True))

    result = runner.invoke(app, ["bot", "ask", "博敏电子最近几天命中策略的情况"])

    assert result.exit_code == 0
    assert "博敏电子近 3 日命中 2 次" in result.output
    assert "SELECT 1 LIMIT 200" in result.output
    assert "返回 2 行" in result.output


def test_bot_ask_exits_nonzero_on_failure(monkeypatch):
    from quant.bot.qa import Answer

    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        deepseek_api_key="sk-x", deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash"))
    monkeypatch.setattr("quant.bot.qa.answer",
                        lambda question, llm, runner_: Answer(
                            "拒绝执行：不允许的关键字：DELETE",
                            "DELETE FROM daily", 0, False))

    result = runner.invoke(app, ["bot", "ask", "删掉数据"])

    assert result.exit_code == 1
    assert "拒绝执行" in result.output


def test_bot_ask_requires_api_key(monkeypatch):
    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        deepseek_api_key=None, deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-flash"))

    result = runner.invoke(app, ["bot", "ask", "问题"])

    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output


def test_bot_serve_requires_credentials(monkeypatch):
    monkeypatch.setattr("quant.config.get_settings", lambda: SimpleNamespace(
        feishu_app_id=None, feishu_app_secret=None, deepseek_api_key=None))

    result = runner.invoke(app, ["bot", "serve"])

    assert result.exit_code == 1
    assert "FEISHU_APP_ID" in result.output
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cli_bot.py -q`
Expected: FAIL —— `Error: No such command 'bot'`

- [ ] **Step 3: 实现**

`src/quant/cli.py` 顶部子命令组处（`app.add_typer(hits_app, name="hits")` 之后）追加：

```python
bot_app = typer.Typer(help="飞书问数机器人", no_args_is_help=True)
app.add_typer(bot_app, name="bot")
```

并在文件末尾追加两个命令：

```python
@bot_app.command("serve")
def bot_serve() -> None:
    """启动飞书长连接问数机器人（前台常驻，Ctrl+C 退出）。"""
    from quant.bot import feishu
    from quant.utils.logging import setup_logging

    setup_logging()
    try:
        feishu.run_bot()
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc


@bot_app.command("ask")
def bot_ask(question: str = typer.Argument(..., help="自然语言问题")) -> None:
    """本地跑一遍问答链路（不连飞书），打印回答与 SQL。"""
    from quant.bot import qa
    from quant.bot.llm import DeepSeekClient
    from quant.bot.runner import run_readonly
    from quant.config import get_settings
    from quant.utils.logging import setup_logging

    setup_logging()
    settings = get_settings()
    if not settings.deepseek_api_key:
        typer.echo("未配置 DEEPSEEK_API_KEY（.env）")
        raise typer.Exit(code=1)
    llm = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url,
                         settings.deepseek_model)
    result = qa.answer(question, llm, run_readonly)
    typer.echo(result.text)
    if result.sql:
        typer.echo(f"\nSQL：{result.sql}")
    typer.echo(f"返回 {result.row_count} 行（{'成功' if result.ok else '失败'}）")
    if not result.ok:
        raise typer.Exit(code=1)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cli_bot.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/quant/cli.py tests/test_cli_bot.py
git commit -m "feat: add quant bot cli commands"
```

---

### Task 10: 文档与端到端验收

**Files:**
- Modify: `README.md`
- Modify: `docs/my-strategies.md`（仅在需要说明"机器人可查哪些表"时加一节，无需要则跳过）

**Interfaces:**
- Consumes: 前 9 个任务的全部产出
- Produces: 用户可照做的使用说明 + 已实测的端到端结论

- [ ] **Step 1: 补 README**

在「交易日盘后流程」之后新增一节：

```markdown
## 飞书问数机器人（@机器人提问）

在飞书群 @机器人 提问，例如"博敏电子最近几天命中策略的情况"，机器人会用大模型生成只读 SQL、
查库后以卡片回复（正文为中文总结，末尾附返回行数与 SQL）。

```bash
uv run quant bot ask "博敏电子最近几天命中策略的情况"   # 本地跑通链路，不连飞书
uv run quant bot serve                                  # 启动长连接常驻（前台，Ctrl+C 退出）
```

- 需要 `.env` 配置 `feishu_app_id`、`feishu_app_secret`、`deepseek_api_key`（可选 `deepseek_base_url`、`deepseek_model`）
- 飞书开放平台需把事件订阅方式设为「长连接」、订阅 `im.message.receive_v1`，并开通 `im:message`（群聊含 `im:message.group_at_msg:readonly`）与 `im:message:send_as_bot` 权限后发布版本
- 安全边界：只允许单条 `SELECT`/`WITH`、表白名单（`schemas` 中全部表）、`START TRANSACTION READ ONLY`、结果最多 200 行、查询超时 15 秒
```

- [ ] **Step 2: 全量测试**

Run: `uv run pytest -q`
Expected: 全部通过（新增 bot 相关测试 + 既有测试；integration 标记的用例按原样 skip/deselect）

- [ ] **Step 3: 本地链路实测**

Run: `uv run quant bot ask "博敏电子最近几天命中策略的情况"`
Expected: 打印中文回答 + SQL + 行数，退出码 0；回答中的日期与 `hit_*` 表数据一致

- [ ] **Step 4: 长连接实测（需要用户配合）**

Run: `uv run quant bot serve`
然后在飞书群里 @机器人 发送「博敏电子最近几天命中策略的情况」
Expected: 数十秒内收到挂在提问下的卡片回复（含总结、行数、SQL）；群里再发一条普通消息（不 @）机器人不响应
异常排查：若日志提示未收到事件，检查控制台「事件订阅方式=长连接」与权限是否已发布生效

- [ ] **Step 5: 提交**

```bash
git add README.md docs/my-strategies.md
git commit -m "docs: document the feishu qa bot"
```

---

## Self-Review

**Spec 覆盖检查**

| Spec 章节 | 对应任务 |
|---|---|
| §3 架构数据流 | Task 2–8 |
| §4 模块与接口 | Task 2–8（`schema/sql_guard/runner/llm/qa/cards/feishu` 一一对应） |
| §4 CLI（serve / ask） | Task 9 |
| §5 SQL 安全四层 | Task 3（白名单/形态/LIMIT）+ Task 4（只读事务/超时）+ Task 6（截断） |
| §6 配置 | Task 1 |
| §7 飞书控制台前置 | Task 10 Step 1/4 |
| §8 错误处理与失败路径 | Task 6（LLM/SQL/执行/空结果/总结失败）、Task 8（忽略非文本、空问题提示、去重、回复失败记日志） |
| §9 测试策略 | 各任务单测 + Task 10 Step 2–4 |
| §10 验收标准 1–5 | Task 10 Step 3（1）、Step 4（2）、Task 3+6（3）、Task 6/8（4）、Task 10 Step 2（5） |
| §11 依赖变更 | Task 1 |

**类型一致性**：`Answer(text, sql, row_count, ok, note)` 在 Task 6 定义，Task 7/9/10 按同一顺序使用；`validate_sql(sql, allowed=None, max_rows=200)`、`run_readonly(sql, timeout=15)`、`answer(question, llm, runner, schema_text=None)`、`parse_event(data)`、`BotService.handle_event(data)` 全程一致。

**已知偏差**：schema 提示词改用代码内 DDL 常量而非 `information_schema`（理由见「文件结构」末尾说明）。
