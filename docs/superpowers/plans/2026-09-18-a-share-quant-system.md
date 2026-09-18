# A 股量化选股与回测系统 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成一个本地命令行量化系统：从 Tushare 增量同步日线数据到阿里云 MySQL，经本地 Parquet 缓存，支持注册式策略选股与符合 A 股交易规则的可信回测。

**Architecture:** 单一 Python 包 `quant`（src 布局），分四层：data（Tushare→MySQL→Parquet）、strategies（插件注册）、engine（loader/rules/backtest/metrics/report）、cli（typer）。计算内核 pandas，回测按调仓日推进、行情用后复权价。

**Tech Stack:** Python 3.12、uv、pandas、pyarrow、pymysql、tushare、pydantic / pydantic-settings、PyYAML、typer、matplotlib、pytest。

**Spec:** `docs/superpowers/specs/2026-09-18-a-share-quant-system-design.md`

## Global Constraints

- Python 3.12；所有依赖写入 `pyproject.toml`，不得引入 spec 第 13 节之外的框架级依赖（不引入 SQLAlchemy、backtrader、TA-Lib 等）。
- 代码标识符、文件名用英文；CLI 帮助、日志、报错、报告列名用中文。
- 日期在内部与数据库统一为 `"YYYYMMDD"` 字符串；CLI 接受 `"YYYY-MM-DD"` 或 `"YYYYMMDD"`，入口处统一转换。
- 密钥与连接信息只从 `.env` 读取（pydantic-settings），禁止硬编码；`.env` 永远保持 gitignored。
- MySQL 表 InnoDB / utf8mb4，价格 DECIMAL，主键与 `trade_date` 索引按 spec 5.1。
- Tushare 参数名用 `start_date` / `end_date` 时格式为 YYYYMMDD。
- 测试用 pytest；集成测试必须 `@pytest.mark.integration` 且 `RUN_INTEGRATION` 未设置时 skip；单元测试禁止访问网络或真实 MySQL。
- 策略信号必须因果：只允许 rolling/shift 正向使用历史，任何测试发现未来函数视为缺陷。
- 每个 Task 结束时 commit 一次，提交信息用 `feat:` / `test:` / `chore:` / `docs:` 前缀。
- 运行命令统一 `uv run ...`；测试统一 `uv run pytest`。

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 依赖、`quant` 命令入口、pytest 配置 |
| `.env` / `.env.example` | 密钥（不提交）/ 键名模板 |
| `src/quant/__init__.py` | 包标记 |
| `src/quant/config.py` | `Settings`：读 `.env`，`get_settings()` |
| `src/quant/utils/logging.py` | 控制台 + `logs/quant.log` 轮转日志 |
| `src/quant/utils/dates.py` | 日期格式转换与交易日工具 |
| `src/quant/data/db.py` | MySQL 连接、`read_df`、`upsert_df`、upsert SQL 构建 |
| `src/quant/data/schemas.py` | 10 张表 DDL 与列清单、`create_all()` |
| `src/quant/data/tushare_client.py` | 限流、重试、按接口封装 |
| `src/quant/data/ingest.py` | 全量/增量入库、水位线管理 |
| `src/quant/data/cache.py` | Parquet 镜像与增量刷新、按需读取 |
| `src/quant/strategies/base.py` | `Strategy` ABC、注册表、YAML 加载 |
| `src/quant/strategies/__init__.py` | 自动发现策略模块 |
| `src/quant/strategies/ma_volume.py` | 均线+放量策略 |
| `src/quant/strategies/high_tight_flag.py` | 高位紧缩旗形策略 |
| `src/quant/engine/loader.py` | 行情装载、复权、股票池过滤、面板工具 |
| `src/quant/engine/selection.py` | 信号计算、选股 |
| `src/quant/engine/rules.py` | 板块/整手/涨跌停/费用/滑点 |
| `src/quant/engine/backtest.py` | 调仓推进回测引擎 |
| `src/quant/engine/metrics.py` | 业绩指标与交易配对 |
| `src/quant/engine/report.py` | 净值图、CSV、控制台输出 |
| `src/quant/cli.py` | typer 入口与全部子命令 |
| `configs/strategies/*.yaml` | 策略参数 |
| `configs/backtest/default.yaml` | 回测默认参数 |
| `tests/...` | 单元测试与集成测试 |

里程碑映射：M1 = Task 1–3；M2 = Task 4–7；M3 = Task 8–11；M4 = Task 12–15；M5 = Task 16–17。

---

### Task 1: 项目骨架与配置

**Files:**
- Modify: `pyproject.toml`、`.gitignore`
- Create: `.env.example`、`src/quant/__init__.py`、`src/quant/config.py`、`tests/test_config.py`
- Delete: `test_db.py`（硬编码数据库密码，必须删除）

**Interfaces:**
- Consumes: 无
- Produces: `quant.config.Settings`（字段 `aliyun_rds_host/port/user/passport/database`、`tushare_token`）、`quant.config.get_settings() -> Settings`；`quant` 命令入口

- [ ] **Step 1: 更新 pyproject.toml**

```toml
[project]
name = "my-a-stock-quant"
version = "0.1.0"
description = "A股量化选股与回测系统"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "pandas>=2.2",
    "pyarrow>=16.0",
    "pymysql>=1.1",
    "tushare>=1.4",
    "pydantic>=2.7",
    "pydantic-settings>=2.2",
    "pyyaml>=6.0",
    "typer>=0.12",
    "matplotlib>=3.8",
]

[project.scripts]
quant = "quant.cli:app"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/quant"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: 需要真实 MySQL/Tushare 的集成测试"]
```

- [ ] **Step 2: 写失败的测试 tests/test_config.py**

```python
import pytest

from quant.config import Settings

ENV_KEYS = [
    "ALIYUN_RDS_HOST", "ALIYUN_RDS_PORT", "ALIYUN_RDS_USER",
    "ALIYUN_RDS_PASSPORT", "ALIYUN_RDS_DATABASE", "TUSHARE_TOKEN",
]


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("ALIYUN_RDS_HOST", "db.example.com")
    monkeypatch.setenv("ALIYUN_RDS_USER", "user1")
    monkeypatch.setenv("ALIYUN_RDS_PASSPORT", "pw")
    monkeypatch.setenv("ALIYUN_RDS_DATABASE", "quant")
    monkeypatch.setenv("TUSHARE_TOKEN", "tok")
    monkeypatch.delenv("ALIYUN_RDS_PORT", raising=False)

    s = Settings(_env_file=None)

    assert s.aliyun_rds_host == "db.example.com"
    assert s.aliyun_rds_port == 3306
    assert s.tushare_token == "tok"


def test_settings_missing_required_raises(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(Exception):
        Settings(_env_file=None)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL（ModuleNotFoundError: quant.config）

- [ ] **Step 4: 实现 config.py 与包标记**

`src/quant/__init__.py`：

```python
"""A股量化选股与回测系统。"""
```

`src/quant/config.py`：

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从 .env 读取的数据库与 Tushare 配置。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    aliyun_rds_host: str
    aliyun_rds_port: int = 3306
    aliyun_rds_user: str
    aliyun_rds_passport: str
    aliyun_rds_database: str
    tushare_token: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 5: 更新 .gitignore、创建 .env.example、删除 test_db.py**

`.gitignore` 追加：

```
.env
data_cache/
outputs/
logs/
.pytest_cache/
.coverage
```

`.env.example`：

```
aliyun_rds_host=your-rds-host.mysql.rds.aliyuncs.com
aliyun_rds_port=3306
aliyun_rds_user=your-user
aliyun_rds_passport=your-password
aliyun_rds_database=your-database
tushare_token=your-tushare-token
```

PowerShell 删除脚本：`Remove-Item test_db.py`

- [ ] **Step 6: 同步依赖并运行测试确认通过**

Run: `uv sync; uv run pytest tests/test_config.py -v`
Expected: 2 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore .env.example src/quant/__init__.py src/quant/config.py tests/test_config.py uv.lock
git commit -m "chore: project skeleton and env config"
```

---

### Task 2: MySQL 连接与 upsert 工具

**Files:**
- Create: `src/quant/data/__init__.py`、`src/quant/data/db.py`、`tests/test_db.py`

**Interfaces:**
- Consumes: `quant.config.get_settings`
- Produces:
  - `db.get_connection() -> pymysql.connections.Connection`
  - `db.build_upsert_sql(table: str, columns: list[str]) -> str`
  - `db.upsert_rows(table: str, columns: list[str], rows: Iterable[Sequence]) -> int`
  - `db.upsert_df(table: str, df: pd.DataFrame, columns: list[str] | None = None) -> int`
  - `db.read_df(sql: str, params: Sequence | None = None) -> pd.DataFrame`
  - `db.scalar(sql: str, params: Sequence | None = None)`
  - `db.execute(sql: str, params: Sequence | None = None) -> int`

- [ ] **Step 1: 写失败的测试 tests/test_db.py**

```python
import pandas as pd

from quant.data import db


def test_build_upsert_sql():
    sql = db.build_upsert_sql("daily", ["ts_code", "trade_date", "close"])
    assert sql == (
        "INSERT INTO `daily` (`ts_code`, `trade_date`, `close`) "
        "VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE `ts_code`=VALUES(`ts_code`), "
        "`trade_date`=VALUES(`trade_date`), `close`=VALUES(`close`)"
    )


def test_upsert_df_converts_nan_to_none(monkeypatch):
    captured = {}

    def fake_upsert_rows(table, columns, rows):
        captured["table"] = table
        captured["columns"] = columns
        captured["rows"] = list(rows)
        return len(captured["rows"])

    monkeypatch.setattr(db, "upsert_rows", fake_upsert_rows)
    df = pd.DataFrame({"ts_code": ["000001.SZ", "600000.SH"],
                       "trade_date": ["20240102", "20240102"],
                       "close": [10.5, float("nan")]})

    n = db.upsert_df("daily", df)

    assert n == 2
    assert captured["columns"] == ["ts_code", "trade_date", "close"]
    assert captured["rows"][1][2] is None


def test_upsert_df_respects_column_order(monkeypatch):
    captured = {}
    monkeypatch.setattr(db, "upsert_rows",
                        lambda table, columns, rows: captured.update(columns=columns) or 1)
    df = pd.DataFrame({"b": [1], "a": [2]})
    db.upsert_df("t", df, columns=["a", "b"])
    assert captured["columns"] == ["a", "b"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL（ModuleNotFoundError: quant.data）

- [ ] **Step 3: 实现 db.py**

`src/quant/data/__init__.py`：空文件。

`src/quant/data/db.py`：

```python
from __future__ import annotations

from collections.abc import Iterable, Sequence

import pandas as pd
import pymysql

from quant.config import get_settings


def get_connection() -> pymysql.connections.Connection:
    s = get_settings()
    return pymysql.connect(
        host=s.aliyun_rds_host,
        port=s.aliyun_rds_port,
        user=s.aliyun_rds_user,
        password=s.aliyun_rds_passport,
        database=s.aliyun_rds_database,
        charset="utf8mb4",
        autocommit=False,
    )


def build_upsert_sql(table: str, columns: list[str]) -> str:
    cols = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in columns)
    return (
        f"INSERT INTO `{table}` ({cols}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


def upsert_rows(table: str, columns: list[str], rows: Iterable[Sequence],
                chunk_size: int = 2000) -> int:
    sql = build_upsert_sql(table, columns)
    total = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            batch: list[tuple] = []
            for row in rows:
                batch.append(tuple(row))
                if len(batch) >= chunk_size:
                    total += cur.executemany(sql, batch)
                    batch = []
            if batch:
                total += cur.executemany(sql, batch)
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_df(table: str, df: pd.DataFrame, columns: list[str] | None = None) -> int:
    cols = list(columns) if columns is not None else list(df.columns)
    data = df[cols].astype(object).where(pd.notna(df[cols]), None)
    rows = data.itertuples(index=False, name=None)
    return upsert_rows(table, cols, rows)


def read_df(sql: str, params: Sequence | None = None) -> pd.DataFrame:
    conn = get_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.DataFrame(rows)


def scalar(sql: str, params: Sequence | None = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def execute(sql: str, params: Sequence | None = None) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            n = cur.execute(sql, params)
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_db.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/data/__init__.py src/quant/data/db.py tests/test_db.py
git commit -m "feat: mysql connection and upsert helpers"
```

---

### Task 3: 表结构 DDL 与 init-db

**Files:**
- Create: `src/quant/data/schemas.py`、`tests/test_schemas.py`
- Modify: `src/quant/cli.py`（本任务创建雏形，只含 app 与 `data init-db`）

**Interfaces:**
- Consumes: `quant.data.db.execute`
- Produces:
  - `schemas.TableSpec(name, columns: tuple[str, ...], ddl: str, date_column: str | None)`
  - `schemas.TABLES: dict[str, TableSpec]`
  - `schemas.columns_of(table: str) -> list[str]`
  - `schemas.date_column(table: str) -> str | None`
  - `schemas.create_all() -> list[str]`
  - `quant.cli.app`（typer.Typer，后续任务继续加子命令）

- [ ] **Step 1: 写失败的测试 tests/test_schemas.py**

```python
from quant.data import schemas


def test_all_expected_tables_exist():
    expected = {
        "stock_basic", "trade_cal", "daily", "adj_factor", "daily_basic",
        "suspend_d", "stk_limit", "index_daily", "namechange", "ingest_log",
    }
    assert set(schemas.TABLES) == expected


def test_daily_primary_key_and_index():
    ddl = schemas.TABLES["daily"].ddl
    assert "PRIMARY KEY (ts_code, trade_date)" in ddl
    assert "KEY idx_trade_date (trade_date)" in ddl


def test_columns_of_order():
    cols = schemas.columns_of("daily")
    assert cols[:4] == ["ts_code", "trade_date", "open", "high"]
    assert "amount" in cols


def test_date_column_flags():
    assert schemas.date_column("daily") == "trade_date"
    assert schemas.date_column("stock_basic") is None


def test_create_all_calls_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(schemas.db, "execute", lambda sql, params=None: calls.append(sql))
    names = schemas.create_all()
    assert set(names) == set(schemas.TABLES)
    assert len(calls) == len(schemas.TABLES)
    assert all("CREATE TABLE IF NOT EXISTS" in sql for sql in calls)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 schemas.py**

```python
from __future__ import annotations

from dataclasses import dataclass

from quant.data import db


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    ddl: str
    date_column: str | None = "trade_date"


TABLES: dict[str, TableSpec] = {
    "stock_basic": TableSpec(
        name="stock_basic",
        columns=("ts_code", "symbol", "name", "area", "industry", "market",
                 "exchange", "list_status", "list_date", "delist_date", "is_hs"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS stock_basic (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  symbol VARCHAR(10) NOT NULL COMMENT '股票代码',
  name VARCHAR(32) NOT NULL COMMENT '名称',
  area VARCHAR(16) COMMENT '地域',
  industry VARCHAR(32) COMMENT '行业',
  market VARCHAR(16) COMMENT '市场(主板/创业板/科创板/北交所)',
  exchange VARCHAR(8) COMMENT '交易所',
  list_status CHAR(1) COMMENT 'L上市 D退市 P暂停',
  list_date CHAR(8) COMMENT '上市日期',
  delist_date CHAR(8) COMMENT '退市日期',
  is_hs CHAR(1) COMMENT '是否沪深港通标的',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息'
""",
    ),
    "trade_cal": TableSpec(
        name="trade_cal",
        columns=("exchange", "cal_date", "is_open", "pretrade_date"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS trade_cal (
  exchange VARCHAR(8) NOT NULL COMMENT '交易所',
  cal_date CHAR(8) NOT NULL COMMENT '日历日期',
  is_open TINYINT NOT NULL COMMENT '是否交易 1开市 0休市',
  pretrade_date CHAR(8) COMMENT '上一个交易日',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (exchange, cal_date),
  KEY idx_cal_date (cal_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易日历'
""",
    ),
    "daily": TableSpec(
        name="daily",
        columns=("ts_code", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"),
        ddl="""
CREATE TABLE IF NOT EXISTS daily (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  open DECIMAL(12,4) COMMENT '开盘价',
  high DECIMAL(12,4) COMMENT '最高价',
  low DECIMAL(12,4) COMMENT '最低价',
  close DECIMAL(12,4) COMMENT '收盘价',
  pre_close DECIMAL(12,4) COMMENT '昨收价',
  `change` DECIMAL(12,4) COMMENT '涨跌额',
  pct_chg DECIMAL(10,4) COMMENT '涨跌幅%',
  vol DECIMAL(20,2) COMMENT '成交量(手)',
  amount DECIMAL(20,4) COMMENT '成交额(千元)',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情'
""",
    ),
    "adj_factor": TableSpec(
        name="adj_factor",
        columns=("ts_code", "trade_date", "adj_factor"),
        ddl="""
CREATE TABLE IF NOT EXISTS adj_factor (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  adj_factor DECIMAL(20,8) COMMENT '复权因子',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='复权因子'
""",
    ),
    "daily_basic": TableSpec(
        name="daily_basic",
        columns=("ts_code", "trade_date", "turnover_rate", "turnover_rate_f",
                 "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
                 "dv_ratio", "dv_ttm", "total_share", "float_share",
                 "free_share", "total_mv", "circ_mv"),
        ddl="""
CREATE TABLE IF NOT EXISTS daily_basic (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  turnover_rate DECIMAL(16,6) COMMENT '换手率%',
  turnover_rate_f DECIMAL(16,6) COMMENT '自由流通换手率%',
  volume_ratio DECIMAL(16,6) COMMENT '量比',
  pe DECIMAL(16,6) COMMENT '市盈率',
  pe_ttm DECIMAL(16,6) COMMENT '市盈率TTM',
  pb DECIMAL(16,6) COMMENT '市净率',
  ps DECIMAL(16,6) COMMENT '市销率',
  ps_ttm DECIMAL(16,6) COMMENT '市销率TTM',
  dv_ratio DECIMAL(16,6) COMMENT '股息率%',
  dv_ttm DECIMAL(16,6) COMMENT '股息率TTM%',
  total_share DECIMAL(20,4) COMMENT '总股本(万股)',
  float_share DECIMAL(20,4) COMMENT '流通股本(万股)',
  free_share DECIMAL(20,4) COMMENT '自由流通股本(万股)',
  total_mv DECIMAL(20,4) COMMENT '总市值(万元)',
  circ_mv DECIMAL(20,4) COMMENT '流通市值(万元)',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日指标'
""",
    ),
    "suspend_d": TableSpec(
        name="suspend_d",
        columns=("ts_code", "trade_date", "suspend_timing", "suspend_type"),
        ddl="""
CREATE TABLE IF NOT EXISTS suspend_d (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  suspend_timing VARCHAR(16) COMMENT '日内停牌时段',
  suspend_type VARCHAR(8) COMMENT 'S停牌 R复牌',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日停牌信息'
""",
    ),
    "stk_limit": TableSpec(
        name="stk_limit",
        columns=("ts_code", "trade_date", "up_limit", "down_limit"),
        ddl="""
CREATE TABLE IF NOT EXISTS stk_limit (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  up_limit DECIMAL(12,4) COMMENT '涨停价',
  down_limit DECIMAL(12,4) COMMENT '跌停价',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日涨跌停价格'
""",
    ),
    "index_daily": TableSpec(
        name="index_daily",
        columns=("ts_code", "trade_date", "open", "high", "low", "close",
                 "pre_close", "change", "pct_chg", "vol", "amount"),
        ddl="""
CREATE TABLE IF NOT EXISTS index_daily (
  ts_code VARCHAR(12) NOT NULL COMMENT '指数代码',
  trade_date CHAR(8) NOT NULL COMMENT '交易日期',
  open DECIMAL(12,4) COMMENT '开盘点位',
  high DECIMAL(12,4) COMMENT '最高点位',
  low DECIMAL(12,4) COMMENT '最低点位',
  close DECIMAL(12,4) COMMENT '收盘点位',
  pre_close DECIMAL(12,4) COMMENT '昨收点位',
  `change` DECIMAL(12,4) COMMENT '涨跌点位',
  pct_chg DECIMAL(10,4) COMMENT '涨跌幅%',
  vol DECIMAL(20,2) COMMENT '成交量(手)',
  amount DECIMAL(20,4) COMMENT '成交额(千元)',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, trade_date),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数日线'
""",
    ),
    "namechange": TableSpec(
        name="namechange",
        columns=("ts_code", "name", "start_date", "end_date", "ann_date",
                 "change_reason"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS namechange (
  ts_code VARCHAR(12) NOT NULL COMMENT 'TS代码',
  name VARCHAR(32) NOT NULL COMMENT '证券名称',
  start_date CHAR(8) COMMENT '开始日期',
  end_date CHAR(8) COMMENT '结束日期',
  ann_date CHAR(8) COMMENT '公告日期',
  change_reason VARCHAR(64) COMMENT '变更原因',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (ts_code, start_date),
  KEY idx_start_date (start_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票名称变更'
""",
    ),
    "ingest_log": TableSpec(
        name="ingest_log",
        columns=("task_name", "last_trade_date"),
        date_column=None,
        ddl="""
CREATE TABLE IF NOT EXISTS ingest_log (
  task_name VARCHAR(32) NOT NULL COMMENT '任务名(表名)',
  last_trade_date CHAR(8) COMMENT '已入库最大日期',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='入库水位线'
""",
    ),
}


def columns_of(table: str) -> list[str]:
    return list(TABLES[table].columns)


def date_column(table: str) -> str | None:
    return TABLES[table].date_column


def create_all() -> list[str]:
    for spec in TABLES.values():
        db.execute(spec.ddl)
    return list(TABLES)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: 5 passed

- [ ] **Step 5: 创建 cli.py 并实现 init-db 命令**

`src/quant/cli.py`：

```python
import typer

app = typer.Typer(help="A股量化选股与回测系统", no_args_is_help=True)
data_app = typer.Typer(help="数据管理", no_args_is_help=True)
app.add_typer(data_app, name="data")


@data_app.command("init-db")
def data_init_db() -> None:
    """在 MySQL 中创建全部数据表（幂等）。"""
    from quant.data import schemas

    names = schemas.create_all()
    typer.echo(f"已创建/确认 {len(names)} 张表：{', '.join(names)}")
```

- [ ] **Step 6: 冒烟验证 CLI**

Run: `uv run quant data init-db --help`
Expected: 显示帮助，无异常

- [ ] **Step 7: Commit**

```bash
git add src/quant/data/schemas.py src/quant/cli.py tests/test_schemas.py
git commit -m "feat: mysql schemas and init-db command"
```
---

### Task 4: Tushare 客户端（限流 + 重试）

**Files:**
- Create: `src/quant/data/tushare_client.py`、`tests/test_tushare_client.py`

**Interfaces:**
- Consumes: `quant.config.get_settings`
- Produces:
  - `tushare_client.RateLimiter(per_minute: int, sleep=time.sleep, clock=time.monotonic)`，方法 `acquire()`
  - `tushare_client.TushareClient(token=None, calls_per_minute=150, max_retries=3, pro=None)`
  - `client.call(api_name: str, **kwargs) -> pd.DataFrame`
  - `client.fetch_daily/fetch_adj_factor/fetch_daily_basic/fetch_suspend_d/fetch_stk_limit(trade_date) -> pd.DataFrame`
  - `client.fetch_index_daily(ts_code, trade_date)`、`client.fetch_stock_basic()`、`client.fetch_trade_cal(start_date, end_date)`、`client.fetch_namechange()`

- [ ] **Step 1: 写失败的测试 tests/test_tushare_client.py**

```python
import pandas as pd
import pytest

from quant.data.tushare_client import RateLimiter, TushareClient


def test_rate_limiter_first_call_no_sleep_then_waits():
    clock_time = {"t": 0.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock_time["t"] += seconds

    rl = RateLimiter(120, sleep=fake_sleep, clock=lambda: clock_time["t"])
    rl.acquire()
    assert sleeps == []
    rl.acquire()
    assert sleeps == pytest.approx([0.5], abs=1e-6)


def test_call_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("quant.data.tushare_client.time.sleep", lambda s: None)

    class FakePro:
        def __init__(self):
            self.calls = 0

        def query(self, api, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("限流")
            return pd.DataFrame({"x": [1]})

    pro = FakePro()
    client = TushareClient(token="t", pro=pro)
    df = client.call("daily", trade_date="20240102")
    assert len(df) == 1
    assert pro.calls == 3


def test_call_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr("quant.data.tushare_client.time.sleep", lambda s: None)

    class FailPro:
        def query(self, api, **kwargs):
            raise RuntimeError("boom")

    client = TushareClient(token="t", pro=FailPro(), max_retries=2)
    with pytest.raises(RuntimeError, match="daily"):
        client.call("daily", trade_date="20240102")


def test_fetch_helpers_pass_kwargs():
    calls = []

    class FakePro:
        def query(self, api, **kwargs):
            calls.append((api, kwargs))
            return pd.DataFrame()

    client = TushareClient(token="t", pro=FakePro())
    client.fetch_daily("20240102")
    client.fetch_index_daily("000300.SH", "20240102")
    client.fetch_trade_cal("20240101", "20240131")

    assert calls[0] == ("daily", {"trade_date": "20240102"})
    assert calls[1] == ("index_daily", {"ts_code": "000300.SH", "trade_date": "20240102"})
    assert calls[2] == ("trade_cal", {"exchange": "SSE", "start_date": "20240101", "end_date": "20240131"})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_tushare_client.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 tushare_client.py**

```python
from __future__ import annotations

import time
from typing import Any, Callable

import pandas as pd

from quant.config import get_settings


class RateLimiter:
    """简单间隔限流：保证两次调用之间至少间隔 60/per_minute 秒。"""

    def __init__(self, per_minute: int, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._interval = 60.0 / per_minute
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def acquire(self) -> None:
        now = self._clock()
        if self._last is not None:
            wait = self._last + self._interval - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last = now


class TushareClient:
    """Tushare 封装：限流、指数退避重试、常用接口快捷方法。"""

    def __init__(self, token: str | None = None, calls_per_minute: int = 150,
                 max_retries: int = 3, pro: Any | None = None):
        self._token = token or get_settings().tushare_token
        self._limiter = RateLimiter(calls_per_minute)
        self._max_retries = max_retries
        self._pro = pro

    @property
    def pro(self):
        if self._pro is None:
            import tushare as ts

            self._pro = ts.pro_api(self._token)
        return self._pro

    def call(self, api_name: str, **kwargs) -> pd.DataFrame:
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            self._limiter.acquire()
            try:
                return self.pro.query(api_name, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 需要重试所有接口异常
                last_err = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Tushare 接口 {api_name} 调用失败: {last_err}")

    def fetch_daily(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily", trade_date=trade_date)

    def fetch_adj_factor(self, trade_date: str) -> pd.DataFrame:
        return self.call("adj_factor", trade_date=trade_date)

    def fetch_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily_basic", trade_date=trade_date)

    def fetch_suspend_d(self, trade_date: str) -> pd.DataFrame:
        return self.call("suspend_d", trade_date=trade_date)

    def fetch_stk_limit(self, trade_date: str) -> pd.DataFrame:
        return self.call("stk_limit", trade_date=trade_date)

    def fetch_index_daily(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        return self.call("index_daily", ts_code=ts_code, trade_date=trade_date)

    def fetch_stock_basic(self) -> pd.DataFrame:
        return self.call("stock_basic", exchange="", list_status="L")

    def fetch_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self.call("trade_cal", exchange="SSE",
                         start_date=start_date, end_date=end_date)

    def fetch_namechange(self) -> pd.DataFrame:
        return self.call("namechange")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_tushare_client.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/data/tushare_client.py tests/test_tushare_client.py
git commit -m "feat: tushare client with rate limiting and retry"
```

---

### Task 5: 全量/增量入库与水位线

**Files:**
- Create: `src/quant/data/ingest.py`、`tests/test_ingest.py`
- Create: `src/quant/utils/__init__.py`、`src/quant/utils/dates.py`、`tests/test_dates.py`

**Interfaces:**
- Consumes: `db.scalar/execute/read_df/upsert_df`、`schemas.columns_of`、`TushareClient`
- Produces:
  - `dates.to_yyyymmdd(value) -> str`（接受 `date` / `"YYYY-MM-DD"` / `"YYYYMMDD"`，非法抛 `ValueError`）
  - `ingest.SPECS: dict[str, ApiSpec]`、`ingest.BENCHMARK_INDEXES`
  - `ingest.get_watermark(table) -> str | None`、`ingest.set_watermark(table, trade_date)`
  - `ingest.trade_dates_between(start, end) -> list[str]`
  - `ingest.update(table, from_date=None, to_date=None, client=None) -> int`
  - `ingest.update_all(from_date=None, to_date=None, client=None) -> dict[str, int]`

- [ ] **Step 1: 写失败的测试 tests/test_dates.py**

```python
from datetime import date

import pytest

from quant.utils.dates import to_yyyymmdd


def test_to_yyyymmdd_accepts_multiple_formats():
    assert to_yyyymmdd("2024-01-02") == "20240102"
    assert to_yyyymmdd("20240102") == "20240102"
    assert to_yyyymmdd(date(2024, 1, 2)) == "20240102"


def test_to_yyyymmdd_rejects_bad_input():
    with pytest.raises(ValueError):
        to_yyyymmdd("2024/01/02")
```

- [ ] **Step 2: 写失败的测试 tests/test_ingest.py**

```python
import pandas as pd
import pytest

from quant.data import ingest


class FakeClient:
    def __init__(self, fail_on_date=None):
        self.calls = []
        self.fail_on_date = fail_on_date

    def call(self, api, **kwargs):
        self.calls.append((api, kwargs))
        if self.fail_on_date and kwargs.get("trade_date") == self.fail_on_date:
            raise RuntimeError("mock failure")
        return pd.DataFrame({"ts_code": ["000001.SZ"],
                             "trade_date": [kwargs.get("trade_date", "20240102")],
                             "close": [10.0]})


def _patch_db(monkeypatch):
    state = {"upserts": [], "watermarks": []}
    monkeypatch.setattr(ingest.db, "upsert_df",
                        lambda table, df: state["upserts"].append(df) or len(df))
    monkeypatch.setattr(ingest, "set_watermark",
                        lambda table, d: state["watermarks"].append((table, d)))
    return state


def test_prepare_keeps_only_schema_columns():
    df = pd.DataFrame({"ts_code": ["a"], "close": [1.0], "extra": ["x"]})
    out = ingest._prepare(df, "adj_factor")
    assert list(out.columns) == ["ts_code"]


def test_update_skips_dates_at_or_before_watermark(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240103")
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240103", "20240104", "20240105"])
    client = FakeClient()

    n = ingest.update("daily", to_date="20240105", client=client)

    assert [kw["trade_date"] for _, kw in client.calls] == ["20240104", "20240105"]
    assert state["watermarks"][-1] == ("daily", "20240105")
    assert n == 2


def test_update_respects_from_date_override(monkeypatch):
    _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: "20240103")
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240102", "20240103"])
    client = FakeClient()

    ingest.update("daily", from_date="20240102", to_date="20240103", client=client)

    assert [kw["trade_date"] for _, kw in client.calls] == ["20240102", "20240103"]


def test_watermark_not_advanced_for_failed_batch(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "BATCH_DATES", 2)
    monkeypatch.setattr(ingest, "get_watermark", lambda table: None)
    monkeypatch.setattr(ingest, "trade_dates_between",
                        lambda start, end: ["20240101", "20240102", "20240103", "20240104"])
    client = FakeClient(fail_on_date="20240103")

    with pytest.raises(RuntimeError):
        ingest.update("daily", to_date="20240104", client=client)

    assert state["watermarks"] == [("daily", "20240102")]


def test_full_refresh_upserts_and_sets_watermark(monkeypatch):
    state = _patch_db(monkeypatch)
    monkeypatch.setattr(ingest, "latest_trade_date", lambda: "20240105")
    client = FakeClient()

    ingest.update("stock_basic", client=client)

    assert state["watermarks"] == [("stock_basic", "20240105")]
    assert len(state["upserts"]) == 1
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/test_dates.py tests/test_ingest.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 4: 实现 utils/dates.py 与 ingest.py**

`src/quant/utils/__init__.py`：空文件。

`src/quant/utils/dates.py`：

```python
from __future__ import annotations

from datetime import date


def to_yyyymmdd(value: str | date) -> str:
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text.replace("-", "")
    if len(text) == 8 and text.isdigit():
        return text
    raise ValueError(f"无法识别的日期格式：{value!r}（应为 YYYY-MM-DD 或 YYYYMMDD）")


def to_iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
```

`src/quant/data/ingest.py`：

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from quant.data import db, schemas
from quant.data.tushare_client import TushareClient

BENCHMARK_INDEXES = ("000300.SH",)
BATCH_DATES = 20


@dataclass(frozen=True)
class ApiSpec:
    table: str
    api: str
    mode: str  # "by_date" | "full"
    index_codes: tuple[str, ...] = ()


SPECS: dict[str, ApiSpec] = {
    "daily": ApiSpec("daily", "daily", "by_date"),
    "adj_factor": ApiSpec("adj_factor", "adj_factor", "by_date"),
    "daily_basic": ApiSpec("daily_basic", "daily_basic", "by_date"),
    "suspend_d": ApiSpec("suspend_d", "suspend_d", "by_date"),
    "stk_limit": ApiSpec("stk_limit", "stk_limit", "by_date"),
    "index_daily": ApiSpec("index_daily", "index_daily", "by_date",
                           index_codes=BENCHMARK_INDEXES),
    "stock_basic": ApiSpec("stock_basic", "stock_basic", "full"),
    "trade_cal": ApiSpec("trade_cal", "trade_cal", "full"),
    "namechange": ApiSpec("namechange", "namechange", "full"),
}

FULL_REFRESH_ORDER = ("trade_cal", "stock_basic", "namechange")


def get_watermark(table: str) -> str | None:
    return db.scalar(
        "SELECT last_trade_date FROM ingest_log WHERE task_name=%s", (table,)
    )


def set_watermark(table: str, trade_date: str) -> None:
    db.execute(
        "INSERT INTO ingest_log (task_name, last_trade_date) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE last_trade_date=VALUES(last_trade_date)",
        (table, trade_date),
    )


def trade_dates_between(start: str, end: str) -> list[str]:
    df = db.read_df(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 "
        "AND cal_date>=%s AND cal_date<=%s ORDER BY cal_date",
        (start, end),
    )
    return df["cal_date"].tolist()


def latest_trade_date() -> str | None:
    today = date.today().strftime("%Y%m%d")
    return db.scalar(
        "SELECT MAX(cal_date) FROM trade_cal WHERE exchange='SSE' "
        "AND is_open=1 AND cal_date<=%s",
        (today,),
    )


def _prepare(df: pd.DataFrame, table: str) -> pd.DataFrame:
    cols = [c for c in schemas.columns_of(table) if c in df.columns]
    return df[cols].copy()


def _fetch_by_date(client: TushareClient, spec: ApiSpec, trade_date: str) -> pd.DataFrame:
    if spec.index_codes:
        frames = [client.call(spec.api, ts_code=code, trade_date=trade_date)
                  for code in spec.index_codes]
        return pd.concat(frames, ignore_index=True)
    return client.call(spec.api, trade_date=trade_date)


def update(table: str, from_date: str | None = None, to_date: str | None = None,
           client: TushareClient | None = None) -> int:
    """更新单张表；by_date 表按水位线增量，full 表整体 upsert。幂等、可续跑。"""
    if table not in SPECS:
        raise KeyError(f"未知数据表：{table}")
    spec = SPECS[table]
    client = client or TushareClient()

    if spec.mode == "full":
        if table == "trade_cal":
            df = client.call(spec.api, exchange="SSE",
                             start_date="19900101", end_date="20301231")
        else:
            df = client.call(spec.api)
        prepared = _prepare(df, table)
        n = db.upsert_df(table, prepared)
        set_watermark(table, to_date or latest_trade_date() or "")
        return n

    watermark = None if from_date else get_watermark(table)
    start = from_date or watermark or "19900101"
    end = to_date or latest_trade_date()
    if end is None:
        return 0
    dates = trade_dates_between(start, end)
    if watermark and not from_date:
        dates = [d for d in dates if d > watermark]

    total = 0
    for i in range(0, len(dates), BATCH_DATES):
        batch = dates[i:i + BATCH_DATES]
        frames = [_prepare(_fetch_by_date(client, spec, d), table) for d in batch]
        frames = [f for f in frames if not f.empty]
        if frames:
            total += db.upsert_df(table, pd.concat(frames, ignore_index=True))
        set_watermark(table, batch[-1])
    return total


def update_all(from_date: str | None = None, to_date: str | None = None,
               client: TushareClient | None = None) -> dict[str, int]:
    """先刷参考表（trade_cal/stock_basic/namechange），再增量日频表。"""
    client = client or TushareClient()
    results: dict[str, int] = {}
    for table in FULL_REFRESH_ORDER:
        results[table] = update(table, client=client)
    for table in SPECS:
        if table not in FULL_REFRESH_ORDER:
            results[table] = update(table, from_date=from_date, to_date=to_date,
                                    client=client)
    return results
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_dates.py tests/test_ingest.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/quant/utils/__init__.py src/quant/utils/dates.py src/quant/data/ingest.py tests/test_dates.py tests/test_ingest.py
git commit -m "feat: incremental tushare ingestion with watermarks"
```

---

### Task 6: Parquet 缓存

**Files:**
- Create: `src/quant/data/cache.py`、`tests/test_cache.py`

**Interfaces:**
- Consumes: `db.read_df`、`ingest.get_watermark`、`schemas.date_column`
- Produces:
  - `cache.cache_dir() -> Path`（环境变量 `QUANT_CACHE_DIR` 覆盖，默认 `data_cache`）
  - `cache.cache_path(table) -> Path`
  - `cache.ensure_cache(table) -> Path`、`cache.ensure_all(tables=None) -> None`
  - `cache.load_table(table, columns=None, start=None, end=None, ts_codes=None) -> pd.DataFrame`

- [ ] **Step 1: 写失败的测试 tests/test_cache.py**

```python
import json

import pandas as pd
import pytest

from quant.data import cache


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_CACHE_DIR", str(tmp_path))
    return tmp_path


def _fake_db(monkeypatch, frames):
    calls = {"n": 0}

    def fake_read_df(sql, params=None):
        calls["n"] += 1
        return frames(sql, params)

    monkeypatch.setattr(cache.db, "read_df", fake_read_df)
    return calls


def test_ensure_cache_creates_parquet_and_meta(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "close": [10.0]}))

    path = cache.ensure_cache("daily")

    assert path.exists()
    df = pd.read_parquet(path)
    assert len(df) == 1
    meta = json.loads((cache.cache_dir() / "daily.meta.json").read_text(encoding="utf-8"))
    assert meta["watermark"] == "20240102"


def test_ensure_cache_noop_when_watermark_unchanged(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    calls = _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "close": [10.0]}))
    cache.ensure_cache("daily")
    cache.ensure_cache("daily")
    assert calls["n"] == 1


def test_ensure_cache_appends_delta(monkeypatch):
    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240102")
    _fake_db(monkeypatch, lambda sql, params: pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "close": [10.0]}))
    cache.ensure_cache("daily")

    monkeypatch.setattr(cache.ingest, "get_watermark", lambda t: "20240103")
    seen = {}

    def delta(sql, params):
        seen["params"] = params
        return pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240103"], "close": [11.0]})

    monkeypatch.setattr(cache.db, "read_df", delta)
    cache.ensure_cache("daily")

    df = pd.read_parquet(cache.cache_path("daily"))
    assert list(df["trade_date"]) == ["20240102", "20240103"]
    assert seen["params"] == ("20240102",)


def test_load_table_filters_range_and_codes(monkeypatch):
    df = pd.DataFrame({
        "ts_code": ["A", "A", "B"],
        "trade_date": ["20240101", "20240103", "20240102"],
        "close": [1.0, 2.0, 3.0],
    })
    cache.cache_dir().mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache.cache_path("daily"), index=False)

    out = cache.load_table("daily", start="20240102", end="20240102", ts_codes=["B"])
    assert list(out["close"]) == [3.0]


def test_load_table_missing_cache_raises(tmp_cache):
    with pytest.raises(FileNotFoundError, match="缓存缺失"):
        cache.load_table("daily")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_cache.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 cache.py**

```python
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from quant.data import db, ingest, schemas


def cache_dir() -> Path:
    return Path(os.environ.get("QUANT_CACHE_DIR", "data_cache"))


def cache_path(table: str) -> Path:
    return cache_dir() / f"{table}.parquet"


def _meta_path(table: str) -> Path:
    return cache_dir() / f"{table}.meta.json"


def _read_meta(table: str) -> dict:
    path = _meta_path(table)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_cache(table: str, df: pd.DataFrame, watermark: str | None) -> Path:
    path = cache_path(table)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    meta = {"watermark": watermark, "rows": len(df)}
    _meta_path(table).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return path


def ensure_cache(table: str) -> Path:
    """比对 MySQL 水位线，必要时拉取增量并重写本地镜像。"""
    watermark = ingest.get_watermark(table)
    path = cache_path(table)
    meta = _read_meta(table)
    if path.exists() and watermark is not None and meta.get("watermark") == watermark:
        return path

    existing = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    date_col = schemas.date_column(table)
    if path.exists() and date_col and meta.get("watermark"):
        delta = db.read_df(
            f"SELECT * FROM `{table}` WHERE `{date_col}` > %s ORDER BY `{date_col}`",
            (meta["watermark"],),
        )
    else:
        delta = db.read_df(f"SELECT * FROM `{table}`")

    if existing.empty:
        df = delta
    elif delta.empty:
        df = existing
    else:
        df = pd.concat([existing, delta], ignore_index=True)
    return _write_cache(table, df, watermark)


def ensure_all(tables: list[str] | None = None) -> None:
    names = tables or [n for n in schemas.TABLES if n != "ingest_log"]
    for name in names:
        ensure_cache(name)


def load_table(table: str, columns: list[str] | None = None,
               start: str | None = None, end: str | None = None,
               ts_codes: list[str] | None = None) -> pd.DataFrame:
    path = cache_path(table)
    if not path.exists():
        raise FileNotFoundError(f"缓存缺失：{path}，请先运行 quant data update")

    filters = None
    date_col = schemas.date_column(table)
    if date_col and (start or end):
        filters = []
        if start:
            filters.append((date_col, ">=", start))
        if end:
            filters.append((date_col, "<=", end))

    df = pd.read_parquet(path, columns=columns, filters=filters)
    if ts_codes is not None and "ts_code" in df.columns:
        df = df[df["ts_code"].isin(ts_codes)].reset_index(drop=True)
    return df
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_cache.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/data/cache.py tests/test_cache.py
git commit -m "feat: parquet cache with incremental refresh"
```

---

### Task 7: data CLI（update / status）与日志

**Files:**
- Create: `src/quant/utils/logging.py`、`tests/test_cli_data.py`
- Modify: `src/quant/cli.py`

**Interfaces:**
- Consumes: `ingest.update_all/update`、`cache.cache_path/ensure_cache`、`db.scalar`、`schemas.TABLES`、`dates.to_yyyymmdd`
- Produces: `quant data update`、`quant data status` 命令；`utils.logging.setup_logging()`

- [ ] **Step 1: 写失败的测试 tests/test_cli_data.py**

```python
from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_help_lists_data_commands():
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "update" in result.output
    assert "status" in result.output


def test_data_update_calls_ingest(monkeypatch):
    called = {}

    def fake_update_all(from_date=None, to_date=None, client=None):
        called["from_date"] = from_date
        called["to_date"] = to_date
        return {"daily": 10}

    monkeypatch.setattr("quant.data.ingest.update_all", fake_update_all)
    result = runner.invoke(app, ["data", "update", "--from-date", "2024-01-02"])
    assert result.exit_code == 0
    assert called["from_date"] == "20240102"
    assert "daily" in result.output


def test_data_status_prints_table_rows(monkeypatch):
    monkeypatch.setattr("quant.data.db.scalar",
                        lambda sql, params=None: 123)
    result = runner.invoke(app, ["data", "status"])
    assert result.exit_code == 0
    assert "daily" in result.output
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_cli_data.py -v`
Expected: FAIL（命令不存在 / 输出不符）

- [ ] **Step 3: 实现 logging 与 CLI 命令**

`src/quant/utils/logging.py`：

```python
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    Path("logs").mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        "logs/quant.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
```

在 `src/quant/cli.py` 中追加（保留已有 init-db）：

```python
@data_app.command("update")
def data_update(
    table: str = typer.Option(None, "--table", "-t", help="只更新指定表，缺省全部"),
    from_date: str = typer.Option(None, "--from-date", help="起始日期，如 2024-01-02"),
    to_date: str = typer.Option(None, "--to-date", help="结束日期，如 2024-01-31"),
) -> None:
    """从 Tushare 增量入库（可中断续跑）。"""
    from quant.data import ingest
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    start = to_yyyymmdd(from_date) if from_date else None
    end = to_yyyymmdd(to_date) if to_date else None
    if table:
        results = {table: ingest.update(table, from_date=start, to_date=end)}
    else:
        results = ingest.update_all(from_date=start, to_date=end)
    for name, rows in results.items():
        typer.echo(f"{name}: 已写入 {rows} 行")


@data_app.command("status")
def data_status() -> None:
    """显示各表行数、水位线与本地缓存状态。"""
    import pandas as pd

    from quant.data import cache, db, schemas

    rows = []
    for name in schemas.TABLES:
        try:
            count = db.scalar(f"SELECT COUNT(*) FROM `{name}`")
        except Exception:  # noqa: BLE001 - 表不存在时提示即可
            count = "表不存在"
        if name == "ingest_log":
            watermark = "-"
        else:
            watermark = db.scalar(
                "SELECT last_trade_date FROM ingest_log WHERE task_name=%s", (name,)
            ) or "-"
        rows.append({
            "表": name,
            "行数": count,
            "水位线": watermark,
            "缓存": "有" if cache.cache_path(name).exists() else "无",
        })
    typer.echo(pd.DataFrame(rows).to_string(index=False))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_cli_data.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/utils/logging.py src/quant/cli.py tests/test_cli_data.py
git commit -m "feat: data update/status cli with logging"
```

---

### Task 8: 策略基类与注册表

**Files:**
- Create: `src/quant/strategies/base.py`、`src/quant/strategies/__init__.py`、`tests/test_strategies_base.py`
- Modify: `src/quant/cli.py`（新增 `list` 命令）

**Interfaces:**
- Consumes: 无
- Produces:
  - `base.Strategy`（类属性 `name: str`、`Params: type[BaseModel]`、`warmup_days: int`；实例属性 `p`；方法 `generate_signals(bars) -> pd.Series`、`rank(bars) -> pd.Series | None`）
  - `base.register_strategy(name)` 装饰器、`base.get_strategy(name, **params) -> Strategy`、`base.list_strategies() -> dict[str, type[Strategy]]`
  - `base.load_strategy_config(path) -> tuple[str, dict]`（读 `{strategy: 名称, params: {...}}`）
  - `quant.strategies` 导入时自动发现所有策略模块

- [ ] **Step 1: 写失败的测试 tests/test_strategies_base.py**

```python
import pytest
from pydantic import BaseModel

from quant.strategies.base import (EmptyParams, get_strategy, list_strategies,
                                   load_strategy_config, register_strategy,
                                   Strategy)


class DummyParams(BaseModel):
    threshold: float = 1.0


@register_strategy("dummy")
class DummyStrategy(Strategy):
    Params = DummyParams

    def generate_signals(self, bars):
        return bars["close"] > self.p.threshold


def test_registry_contains_dummy():
    assert "dummy" in list_strategies()


def test_get_strategy_builds_instance_with_params():
    s = get_strategy("dummy", threshold=2.0)
    assert isinstance(s, DummyStrategy)
    assert s.p.threshold == 2.0


def test_unknown_strategy_raises():
    with pytest.raises(KeyError, match="未注册"):
        get_strategy("nope")


def test_invalid_params_raise():
    with pytest.raises(Exception):
        get_strategy("dummy", threshold="not-a-number")


def test_load_strategy_config(tmp_path):
    path = tmp_path / "dummy.yaml"
    path.write_text("strategy: dummy\nparams:\n  threshold: 3.5\n", encoding="utf-8")
    name, params = load_strategy_config(path)
    assert name == "dummy"
    assert params == {"threshold": 3.5}


def test_empty_params_default():
    assert EmptyParams().model_dump() == {}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_strategies_base.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 base.py 与包自动发现**

`src/quant/strategies/base.py`：

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

import pandas as pd
import yaml
from pydantic import BaseModel


class EmptyParams(BaseModel):
    pass


StrategyRegistry: dict[str, type["Strategy"]] = {}


def register_strategy(name: str):
    def decorator(cls: type["Strategy"]) -> type["Strategy"]:
        if name in StrategyRegistry:
            raise ValueError(f"策略名重复注册：{name}")
        cls.name = name
        StrategyRegistry[name] = cls
        return cls
    return decorator


def list_strategies() -> dict[str, type["Strategy"]]:
    return dict(StrategyRegistry)


def get_strategy(name: str, **params) -> "Strategy":
    if name not in StrategyRegistry:
        raise KeyError(f"未注册的策略：{name}")
    return StrategyRegistry[name](**params)


def load_strategy_config(path: str | Path) -> tuple[str, dict]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not raw or "strategy" not in raw:
        raise ValueError(f"策略配置缺少 strategy 字段：{path}")
    return raw["strategy"], raw.get("params", {}) or {}


class Strategy(ABC):
    """策略基类：输入单只股票行情（按日期升序、后复权），输出逐日布尔信号。"""

    name: ClassVar[str] = ""
    Params: ClassVar[type[BaseModel]] = EmptyParams
    warmup_days: ClassVar[int] = 0

    def __init__(self, **params):
        self.p = self.Params(**params)

    @abstractmethod
    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        """bars 列至少含 trade_date/open/high/low/close/vol/amount；返回等长 bool Series。"""

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        """信号数超过持仓数时的排序分，默认 None（由引擎按 rank_by 排序）。"""
        return None
```

`src/quant/strategies/__init__.py`：

```python
from __future__ import annotations

import importlib
import pkgutil

from quant.strategies.base import (EmptyParams, Strategy, get_strategy,
                                   list_strategies, load_strategy_config,
                                   register_strategy)


def _discover() -> None:
    for module in pkgutil.iter_modules(__path__):
        if module.name not in ("base",) and not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")


_discover()
```

- [ ] **Step 5: 在 cli.py 增加 list 命令并运行测试**

在 `src/quant/cli.py` 追加：

```python
@app.command("list")
def list_strategies_cmd() -> None:
    """列出已注册策略及其默认参数。"""
    from quant.strategies.base import list_strategies

    for name, cls in sorted(list_strategies().items()):
        schema = cls.Params.model_json_schema().get("properties", {})
        params = "，".join(
            f"{key}={spec.get('default', '?')}" for key, spec in schema.items()
        ) or "无参数"
        typer.echo(f"{name}: {params}")
```

在 `tests/test_strategies_base.py` 末尾追加：

```python
def test_list_command_shows_registered_strategies():
    from typer.testing import CliRunner

    from quant.cli import app

    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0
    assert "dummy" in result.output
```

Run: `uv run pytest tests/test_strategies_base.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/quant/strategies/__init__.py src/quant/strategies/base.py src/quant/cli.py tests/test_strategies_base.py
git commit -m "feat: strategy base class and plugin registry"
```

---
---

### Task 9: MaVolume 策略

**Files:**
- Create: `src/quant/strategies/ma_volume.py`、`configs/strategies/ma_volume.yaml`、`tests/test_strategy_ma_volume.py`

**Interfaces:**
- Consumes: `strategies.base.Strategy/register_strategy`、`strategies.get_strategy`
- Produces: 注册名 `"ma_volume"`，参数 `ma_short=5, ma_long=20, vol_ma=20, vol_ratio=2.0`；实例属性 `warmup_days`

- [ ] **Step 1: 写失败的测试 tests/test_strategy_ma_volume.py**

```python
import pandas as pd

from quant.strategies import get_strategy


def make_bars(closes, vols):
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(len(closes))],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_ma_volume_signal_on_cross_with_volume():
    closes = [10, 10, 10, 9, 11, 11, 11]
    vols = [100, 100, 100, 100, 100, 500, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert list(signals) == [False, False, False, False, False, True, False]


def test_ma_volume_requires_volume_spike():
    closes = [10, 10, 10, 9, 11, 11, 11]
    vols = [100, 100, 100, 100, 100, 150, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_ma_volume_warmup():
    s = get_strategy("ma_volume")
    assert s.warmup_days == 21


def test_ma_volume_no_signal_when_short_warmup_rows():
    closes = [10, 11]
    s = get_strategy("ma_volume")
    signals = s.generate_signals(make_bars(closes, [100, 100]))
    assert not signals.any()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_strategy_ma_volume.py -v`
Expected: FAIL（KeyError: 未注册的策略）

- [ ] **Step 3: 实现 ma_volume.py 与 YAML**

`src/quant/strategies/ma_volume.py`：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class MaVolumeParams(BaseModel):
    ma_short: int = 5
    ma_long: int = 20
    vol_ma: int = 20
    vol_ratio: float = 2.0


@register_strategy("ma_volume")
class MaVolume(Strategy):
    """短均线上穿长均线，且当日放量。"""

    Params = MaVolumeParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = max(self.p.ma_long, self.p.vol_ma) + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"]
        vol = bars["vol"]
        ma_short = close.rolling(self.p.ma_short).mean()
        ma_long = close.rolling(self.p.ma_long).mean()
        vol_ma = vol.rolling(self.p.vol_ma).mean()
        cross_up = (ma_short > ma_long) & (ma_short.shift(1) <= ma_long.shift(1))
        signal = (close > ma_long) & cross_up & (vol > self.p.vol_ratio * vol_ma)
        return signal.fillna(False)
```

`configs/strategies/ma_volume.yaml`：

```yaml
strategy: ma_volume
params:
  ma_short: 5
  ma_long: 20
  vol_ma: 20
  vol_ratio: 2.0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_strategy_ma_volume.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/strategies/ma_volume.py configs/strategies/ma_volume.yaml tests/test_strategy_ma_volume.py
git commit -m "feat: ma_volume strategy"
```

---

### Task 10: 数据装载、复权与股票池过滤

**Files:**
- Create: `src/quant/utils/codes.py`、`src/quant/engine/__init__.py`、`src/quant/engine/loader.py`、`tests/test_codes.py`、`tests/test_loader.py`

**Interfaces:**
- Consumes: `cache.load_table`、`cache.ensure_all`
- Produces:
  - `codes.board_of(ts_code) -> str`（`"main"`/`"gem"`/`"star"`/`"bse"`）
  - `loader.UniverseFilters(exclude_st=True, min_list_days=60, exclude_suspended=True, allowed_boards=None)`
  - `loader.build_market(daily, adj_factor, daily_basic, suspend_d, stk_limit, stock_basic, namechange, trade_cal, filters) -> pd.DataFrame`
  - `loader.load_market_data(start, end, warmup_days=0, filters=None, ensure=False) -> pd.DataFrame`
  - `loader.eligibility_mask(market, filters) -> pd.Series`
  - `loader.apply_universe(market, filters) -> pd.DataFrame`
  - `loader.resolve_trade_date(date: str | None = None) -> str`
  - `loader.load_stock_names() -> pd.DataFrame`（列 `ts_code`, `name`）
  - `loader.load_benchmark(ts_code, start, end) -> pd.Series`（index=trade_date）

- [ ] **Step 1: 写失败的测试 tests/test_codes.py 与 tests/test_loader.py**

`tests/test_codes.py`：

```python
from quant.utils.codes import board_of


def test_board_of_prefixes():
    assert board_of("600000.SH") == "main"
    assert board_of("000001.SZ") == "main"
    assert board_of("300750.SZ") == "gem"
    assert board_of("301001.SZ") == "gem"
    assert board_of("688981.SH") == "star"
    assert board_of("430047.BJ") == "bse"
```

`tests/test_loader.py`：

```python
import pandas as pd

from quant.engine import loader
from quant.engine.loader import UniverseFilters


def _frames():
    dates = ["20240101", "20240102", "20240103"]
    daily = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "open": [10.0] * 6, "high": [11.0] * 6, "low": [9.0] * 6,
        "close": [10.0] * 6, "pre_close": [10.0] * 6,
        "vol": [100.0] * 6, "amount": [1000.0] * 6,
    })
    adj = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "adj_factor": [2.0] * 6,
    })
    basic = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "turnover_rate": [1.0] * 6, "volume_ratio": [1.0] * 6,
        "pe_ttm": [10.0] * 6, "pb": [1.0] * 6,
        "total_mv": [1000.0] * 6, "circ_mv": [800.0] * 6,
    })
    suspend = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20240102"],
        "suspend_timing": ["全天"], "suspend_type": ["S"],
    })
    limits = pd.DataFrame({
        "ts_code": ["600000.SH"] * 3 + ["000001.SZ"] * 3,
        "trade_date": dates * 2,
        "up_limit": [11.0] * 6, "down_limit": [9.0] * 6,
    })
    stock_basic = pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "list_date": ["20230101", "20240103"],
    })
    namechange = pd.DataFrame({
        "ts_code": ["000001.SZ"], "name": ["*ST平安"],
        "start_date": ["20240103"], "end_date": [""],
    })
    trade_cal = pd.DataFrame({
        "exchange": ["SSE"] * 3, "cal_date": dates, "is_open": [1, 1, 1],
    })
    return daily, adj, basic, suspend, limits, stock_basic, namechange, trade_cal


def test_build_market_adjusts_and_flags():
    frames = _frames()
    filters = UniverseFilters(min_list_days=2)
    market = loader.build_market(*frames, filters)

    row = market[(market["ts_code"] == "600000.SH") & (market["trade_date"] == "20240101")].iloc[0]
    assert row["close"] == 20.0          # 后复权
    assert row["raw_close"] == 10.0
    assert row["board"] == "main"
    assert row["up_limit"] == 11.0

    st_rows = market[(market["ts_code"] == "000001.SZ") & (market["trade_date"] == "20240103")]
    assert st_rows.iloc[0]["is_st"] is True or st_rows.iloc[0]["is_st"]
    assert st_rows.iloc[0]["suspended"] is False

    susp = market[(market["ts_code"] == "000001.SZ") & (market["trade_date"] == "20240102")]
    assert susp.iloc[0]["suspended"]

    new = market[(market["ts_code"] == "000001.SZ") & (market["trade_date"] == "20240103")]
    assert new.iloc[0]["is_new"]  # 上市第 1 个交易日


def test_apply_universe_filters():
    frames = _frames()
    market = loader.build_market(*frames, UniverseFilters(min_list_days=0))
    kept = loader.apply_universe(market, UniverseFilters(min_list_days=0))
    # 000001.SZ 20240102 停牌、20240103 为 ST，均被剔除；600000.SH 保留
    assert ("000001.SZ", "20240103") not in set(zip(kept["ts_code"], kept["trade_date"]))
    assert ("000001.SZ", "20240102") not in set(zip(kept["ts_code"], kept["trade_date"]))
    assert ("600000.SH", "20240101") in set(zip(kept["ts_code"], kept["trade_date"]))


def test_apply_universe_excludes_new_stock():
    frames = _frames()
    market = loader.build_market(*frames, UniverseFilters(min_list_days=2))
    kept = loader.apply_universe(market, UniverseFilters(min_list_days=2))
    # 000001.SZ 于 20240103 上市，前 2 个交易日内视为次新
    assert ("000001.SZ", "20240103") not in set(zip(kept["ts_code"], kept["trade_date"]))


def test_apply_universe_board_filter():
    frames = _frames()
    market = loader.build_market(*frames, UniverseFilters())
    kept = loader.apply_universe(market, UniverseFilters(allowed_boards=("star",)))
    assert kept.empty
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_codes.py tests/test_loader.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 codes.py 与 loader.py**

`src/quant/utils/codes.py`：

```python
from __future__ import annotations

STAR_PREFIX = "688"
GEM_PREFIXES = ("300", "301")
BSE_PREFIXES = ("43", "83", "87", "88", "92")


def board_of(ts_code: str) -> str:
    code = ts_code.split(".")[0]
    if code.startswith(STAR_PREFIX):
        return "star"
    if code.startswith(GEM_PREFIXES):
        return "gem"
    if code.startswith(BSE_PREFIXES):
        return "bse"
    return "main"
```

`src/quant/engine/__init__.py`：空文件。

`src/quant/engine/loader.py`：

```python
from __future__ import annotations

import bisect
from dataclasses import dataclass

import pandas as pd

from quant.data import cache
from quant.utils.codes import board_of


@dataclass(frozen=True)
class UniverseFilters:
    exclude_st: bool = True
    min_list_days: int = 60
    exclude_suspended: bool = True
    allowed_boards: tuple[str, ...] | None = None


def _date_ranks(trade_cal: pd.DataFrame) -> tuple[dict[str, int], list[str]]:
    open_dates = sorted(trade_cal.loc[trade_cal["is_open"] == 1, "cal_date"].astype(str))
    return {d: i for i, d in enumerate(open_dates)}, open_dates


def _st_flags(daily: pd.DataFrame, namechange: pd.DataFrame) -> pd.Series:
    if namechange is None or namechange.empty:
        return pd.Series(False, index=daily.index)
    st = namechange[
        namechange["name"].astype(str).str.upper().str.contains("ST")]
    if st.empty:
        return pd.Series(False, index=daily.index)

    merged = daily[["ts_code", "trade_date"]].merge(
        st[["ts_code", "start_date", "end_date"]], on="ts_code", how="inner")
    start = merged["start_date"].fillna("")
    end = merged["end_date"].fillna("")
    within = (merged["trade_date"] >= start) & (
        end.eq("") | (merged["trade_date"] <= end))
    keys = set(zip(merged.loc[within, "ts_code"], merged.loc[within, "trade_date"]))

    index = pd.MultiIndex.from_arrays([daily["ts_code"], daily["trade_date"]])
    return pd.Series(index.isin(keys), index=daily.index)


def build_market(daily: pd.DataFrame, adj_factor: pd.DataFrame,
                 daily_basic: pd.DataFrame, suspend_d: pd.DataFrame,
                 stk_limit: pd.DataFrame, stock_basic: pd.DataFrame,
                 namechange: pd.DataFrame, trade_cal: pd.DataFrame,
                 filters: UniverseFilters) -> pd.DataFrame:
    """合并各表：输出后复权 OHLC（open/high/low/close）+ raw_* 原始价 + 过滤标记。"""
    df = daily.copy()
    for col in ("open", "high", "low", "close", "pre_close"):
        df[f"raw_{col}"] = df[col]

    df = df.merge(adj_factor[["ts_code", "trade_date", "adj_factor"]],
                  on=["ts_code", "trade_date"], how="left")
    factor = df["adj_factor"].fillna(1.0)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * factor

    basic_cols = ["ts_code", "trade_date", "turnover_rate", "volume_ratio",
                  "pe_ttm", "pb", "total_mv", "circ_mv"]
    df = df.merge(daily_basic[[c for c in basic_cols if c in daily_basic.columns]],
                  on=["ts_code", "trade_date"], how="left")

    limits = stk_limit[["ts_code", "trade_date", "up_limit", "down_limit"]]
    df = df.merge(limits, on=["ts_code", "trade_date"], how="left")

    suspended_keys = set(zip(
        suspend_d.loc[suspend_d["suspend_type"].astype(str) == "S", "ts_code"],
        suspend_d.loc[suspend_d["suspend_type"].astype(str) == "S", "trade_date"].astype(str),
    ))
    market_index = pd.MultiIndex.from_arrays([df["ts_code"], df["trade_date"].astype(str)])
    df["suspended"] = market_index.isin(suspended_keys)

    df["is_st"] = _st_flags(df, namechange)

    rank, open_dates = _date_ranks(trade_cal)
    list_dates = dict(zip(stock_basic["ts_code"], stock_basic["list_date"].astype(str)))
    first_rank = {}
    for code, list_date in list_dates.items():
        pos = bisect.bisect_left(open_dates, list_date)
        first_rank[code] = pos if pos < len(open_dates) else len(open_dates)
    rank_series = df["trade_date"].astype(str).map(rank)
    first_rank_series = df["ts_code"].map(first_rank)
    df["is_new"] = ((rank_series - first_rank_series) < filters.min_list_days).fillna(False)

    df["board"] = df["ts_code"].map(board_of)
    return df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def eligibility_mask(market: pd.DataFrame, filters: UniverseFilters) -> pd.Series:
    mask = ~market["is_new"]
    if filters.exclude_st:
        mask &= ~market["is_st"].astype(bool)
    if filters.exclude_suspended:
        mask &= ~market["suspended"].astype(bool)
    if filters.allowed_boards:
        mask &= market["board"].isin(filters.allowed_boards)
    return mask


def apply_universe(market: pd.DataFrame, filters: UniverseFilters) -> pd.DataFrame:
    return market[eligibility_mask(market, filters)].reset_index(drop=True)


def load_market_data(start: str, end: str, warmup_days: int = 0,
                     filters: UniverseFilters | None = None,
                     ensure: bool = False) -> pd.DataFrame:
    filters = filters or UniverseFilters()
    if ensure:
        cache.ensure_all(["trade_cal", "stock_basic", "namechange", "daily",
                          "adj_factor", "daily_basic", "suspend_d", "stk_limit"])
    trade_cal = cache.load_table("trade_cal")
    rank, open_dates = _date_ranks(trade_cal)
    pos = bisect.bisect_left(open_dates, start)
    load_start = open_dates[max(0, pos - warmup_days)] if open_dates else start

    frame = lambda table: cache.load_table(table, start=load_start, end=end)  # noqa: E731
    daily = frame("daily")
    if daily.empty:
        return daily
    market = build_market(
        daily, frame("adj_factor"), frame("daily_basic"), frame("suspend_d"),
        frame("stk_limit"), cache.load_table("stock_basic"),
        cache.load_table("namechange"), trade_cal, filters,
    )
    return market


def resolve_trade_date(date: str | None = None) -> str:
    trade_cal = cache.load_table("trade_cal")
    open_dates = sorted(trade_cal.loc[trade_cal["is_open"] == 1, "cal_date"].astype(str))
    if date:
        candidates = [d for d in open_dates if d <= date]
    else:
        candidates = open_dates
    if not candidates:
        raise ValueError(f"找不到 <= {date} 的交易日")
    return candidates[-1]


def load_stock_names() -> pd.DataFrame:
    return cache.load_table("stock_basic", columns=["ts_code", "name"])


def load_benchmark(ts_code: str, start: str, end: str) -> pd.Series:
    df = cache.load_table("index_daily", columns=["ts_code", "trade_date", "close"],
                          start=start, end=end, ts_codes=[ts_code])
    if df.empty:
        raise ValueError(f"缓存中没有基准指数 {ts_code} 的行情，请先入库 index_daily")
    series = df.sort_values("trade_date").set_index("trade_date")["close"]
    series.name = ts_code
    return series
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_codes.py tests/test_loader.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/utils/codes.py src/quant/engine/__init__.py src/quant/engine/loader.py tests/test_codes.py tests/test_loader.py
git commit -m "feat: market loader with adjusted prices and universe filters"
```

---

### Task 11: 信号计算、选股命令与选股输出

**Files:**
- Create: `src/quant/engine/selection.py`、`src/quant/engine/report.py`、`tests/test_selection.py`、`tests/test_report.py`、`tests/test_cli_select.py`
- Modify: `src/quant/cli.py`

**Interfaces:**
- Consumes: `loader.load_market_data/apply_universe/load_stock_names/resolve_trade_date`、`Strategy.generate_signals/rank/warmup_days`
- Produces:
  - `selection.compute_signals(strategy, market, rank_by="amount") -> pd.DataFrame`（列 `ts_code/trade_date/signal/score`）
  - `selection.run_selection(strategy, trade_date, top_n=20, market=None, filters=None, rank_by="amount") -> pd.DataFrame`（列 `rank/ts_code/name/trade_date/close/raw_close/amount/score`）
  - `report.save_selection(df, trade_date, strategy_name, out_root="outputs/select") -> Path`
  - CLI `quant select -s <策略> [-d 日期] [-n 数量] [--rank-by 字段]`

- [ ] **Step 1: 写失败的测试 tests/test_selection.py**

```python
import pandas as pd
import pytest
from pydantic import BaseModel

from quant.engine import loader, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import EmptyParams, Strategy


class AboveParams(BaseModel):
    threshold: float = 10.0


class AboveStrategy(Strategy):
    Params = AboveParams

    def generate_signals(self, bars):
        return bars["close"] > self.p.threshold


def make_market():
    rows = []
    for i, code in enumerate(["600000.SH", "600001.SH"]):
        for d in ["20240101", "20240102", "20240103"]:
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": 11.0 + i, "high": 12.0 + i, "low": 10.0 + i,
                "close": 11.0 + i, "raw_close": 11.0 + i,
                "vol": 100.0, "amount": 1000.0 + i, "adj_factor": 1.0,
                "turnover_rate": 1.0, "volume_ratio": 1.0, "pe_ttm": 10.0,
                "pb": 1.0, "total_mv": 100.0, "circ_mv": 80.0,
                "up_limit": 12.0 + i, "down_limit": 10.0 + i,
                "suspended": False, "is_st": False, "is_new": False,
                "board": "main", "raw_open": 11.0 + i,
            })
    return pd.DataFrame(rows)


def test_compute_signals_columns_and_values():
    s = AboveStrategy(threshold=10.5)
    signals = selection.compute_signals(s, make_market())
    assert set(signals.columns) == {"ts_code", "trade_date", "signal", "score"}
    assert signals["signal"].all()
    assert len(signals) == 6


def test_signal_causality_future_data_does_not_change_past(monkeypatch):
    class CrossStrategy(Strategy):
        Params = EmptyParams

        def generate_signals(self, bars):
            ma = bars["close"].rolling(2).mean()
            return bars["close"] > ma

    market = make_market()
    full = selection.compute_signals(CrossStrategy(), market)
    truncated = selection.compute_signals(
        CrossStrategy(), market[market["trade_date"] <= "20240102"])
    common = full[full["trade_date"] <= "20240102"].reset_index(drop=True)
    pd.testing.assert_series_equal(common["signal"], truncated["signal"])


def test_run_selection_ranks_and_filters(monkeypatch):
    market = make_market()
    monkeypatch.setattr(loader, "load_stock_names",
                        lambda: pd.DataFrame({"ts_code": ["600000.SH", "600001.SH"],
                                              "name": ["甲", "乙"]}))
    s = AboveStrategy(threshold=10.5)
    out = selection.run_selection(s, "20240103", top_n=1, market=market, rank_by="amount")
    assert len(out) == 1
    assert out.iloc[0]["ts_code"] == "600001.SH"
    assert out.iloc[0]["name"] == "乙"
    assert out.iloc[0]["rank"] == 1


def test_run_selection_respects_universe(monkeypatch):
    market = make_market()
    market.loc[market["ts_code"] == "600001.SH", "is_st"] = True
    monkeypatch.setattr(loader, "load_stock_names",
                        lambda: pd.DataFrame({"ts_code": ["600000.SH", "600001.SH"],
                                              "name": ["甲", "乙"]}))
    s = AboveStrategy(threshold=10.5)
    out = selection.run_selection(s, "20240103", top_n=5, market=market,
                                  filters=UniverseFilters(exclude_st=True))
    assert list(out["ts_code"]) == ["600000.SH"]


def test_run_selection_raises_on_empty_market(monkeypatch):
    s = AboveStrategy(threshold=10.5)
    monkeypatch.setattr(loader, "load_market_data",
                        lambda **kwargs: pd.DataFrame())
    with pytest.raises(ValueError, match="没有可用行情"):
        selection.run_selection(s, "20240103", market=None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_selection.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 selection.py**

```python
from __future__ import annotations

import pandas as pd

from quant.engine import loader
from quant.engine.loader import UniverseFilters
from quant.strategies.base import Strategy


def compute_signals(strategy: Strategy, market: pd.DataFrame,
                    rank_by: str = "amount") -> pd.DataFrame:
    frames = []
    for ts_code, group in market.groupby("ts_code", sort=False):
        group = group.sort_values("trade_date")
        signals = strategy.generate_signals(group)
        scores = strategy.rank(group)
        if scores is None:
            scores = group[rank_by]
        frames.append(pd.DataFrame({
            "ts_code": ts_code,
            "trade_date": group["trade_date"].values,
            "signal": signals.fillna(False).astype(bool).values,
            "score": pd.to_numeric(scores, errors="coerce").values,
        }))
    if not frames:
        return pd.DataFrame(columns=["ts_code", "trade_date", "signal", "score"])
    return pd.concat(frames, ignore_index=True)


def run_selection(strategy: Strategy, trade_date: str, top_n: int = 20,
                  market: pd.DataFrame | None = None,
                  filters: UniverseFilters | None = None,
                  rank_by: str = "amount") -> pd.DataFrame:
    filters = filters or UniverseFilters()
    if market is None:
        market = loader.load_market_data(
            start=trade_date, end=trade_date,
            warmup_days=strategy.warmup_days, filters=filters)
    if market.empty:
        raise ValueError(f"{trade_date} 没有可用行情，请先更新数据")
    signals = compute_signals(strategy, market, rank_by=rank_by)
    today = market[market["trade_date"] == trade_date]
    eligible = loader.apply_universe(today, filters)
    candidates = signals[(signals["trade_date"] == trade_date) & signals["signal"]]
    merged = eligible.merge(candidates[["ts_code", "score"]], on="ts_code", how="inner")
    merged = merged.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
    merged.insert(0, "rank", merged.index + 1)
    names = loader.load_stock_names()
    merged = merged.merge(names, on="ts_code", how="left")
    return merged[["rank", "ts_code", "name", "trade_date", "close",
                   "raw_close", "amount", "score"]]
```

- [ ] **Step 4: 写失败的测试 tests/test_report.py，然后实现 report.save_selection**

```python
import pandas as pd

from quant.engine import report


def test_save_selection_writes_utf8_csv(tmp_path):
    df = pd.DataFrame({"rank": [1], "ts_code": ["600000.SH"], "name": ["浦发银行"]})
    path = report.save_selection(df, "20240103", "ma_volume", out_root=tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8-sig")
    assert "浦发银行" in text
    assert path.name == "20240103_ma_volume.csv"
```

`src/quant/engine/report.py`：

```python
from __future__ import annotations

from pathlib import Path

import pandas as pd


def save_selection(df: pd.DataFrame, trade_date: str, strategy_name: str,
                   out_root: str | Path = "outputs/select") -> Path:
    folder = Path(out_root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{trade_date}_{strategy_name}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path
```

- [ ] **Step 5: 在 cli.py 增加 select 命令**

```python
@app.command("select")
def select(
    strategy: str = typer.Option(..., "--strategy", "-s", help="策略名"),
    date: str = typer.Option(None, "--date", "-d", help="交易日，缺省最新；非交易日自动回退"),
    top: int = typer.Option(20, "--top", "-n", help="输出数量"),
    rank_by: str = typer.Option("amount", "--rank-by", help="排序字段"),
) -> None:
    """按策略筛选个股并输出 CSV。"""
    from pathlib import Path

    from quant.data import cache
    from quant.engine import loader, selection, report
    from quant.strategies.base import get_strategy, load_strategy_config
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    params: dict = {}
    config_path = Path("configs/strategies") / f"{strategy}.yaml"
    if config_path.exists():
        _, params = load_strategy_config(config_path)
    strat = get_strategy(strategy, **params)

    cache.ensure_all()
    target = loader.resolve_trade_date(to_yyyymmdd(date) if date else None)
    market = loader.load_market_data(
        start=target, end=target, warmup_days=strat.warmup_days)
    result = selection.run_selection(strat, target, top_n=top, market=market,
                                     rank_by=rank_by)
    typer.echo(result.to_string(index=False))
    path = report.save_selection(result, target, strategy)
    typer.echo(f"已保存：{path}")
```

- [ ] **Step 6: 写 tests/test_cli_select.py 并运行全部相关测试**

```python
import pandas as pd
from typer.testing import CliRunner

from quant.cli import app

runner = CliRunner()


def test_select_command_end_to_end_with_fakes(monkeypatch):
    monkeypatch.setattr("quant.data.cache.ensure_all", lambda tables=None: None)
    monkeypatch.setattr("quant.engine.loader.resolve_trade_date", lambda date=None: "20240103")
    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda **kwargs: pd.DataFrame({"dummy": [1]}))
    monkeypatch.setattr("quant.engine.selection.run_selection",
                        lambda *a, **k: pd.DataFrame({
                            "rank": [1], "ts_code": ["600000.SH"], "name": ["浦发银行"]}))
    monkeypatch.setattr("quant.engine.report.save_selection",
                        lambda *a, **k: "outputs/select/20240103_ma_volume.csv")

    result = runner.invoke(app, ["select", "-s", "ma_volume"])

    assert result.exit_code == 0
    assert "浦发银行" in result.output
    assert "已保存" in result.output
```

Run: `uv run pytest tests/test_selection.py tests/test_report.py tests/test_cli_select.py -v`
Expected: 7 passed

- [ ] **Step 7: Commit**

```bash
git add src/quant/engine/selection.py src/quant/engine/report.py src/quant/cli.py tests/test_selection.py tests/test_report.py tests/test_cli_select.py
git commit -m "feat: signal computation, selection command and csv output"
```

---

### Task 12: 交易规则与费用

**Files:**
- Create: `src/quant/engine/rules.py`、`tests/test_rules.py`

**Interfaces:**
- Consumes: `utils.codes.board_of`
- Produces:
  - `rules.lot_rule(ts_code) -> tuple[int, int]`（最小股数、递增单位）
  - `rules.round_lot(shares, ts_code) -> int`
  - `rules.blocked_buy(raw_open, up_limit) -> bool`、`rules.blocked_sell(raw_open, down_limit) -> bool`
  - `rules.FeeConfig(commission_rate=0.00025, min_commission=5.0, stamp_tax_rate=0.0005, transfer_fee_rate=0.00001, slippage=0.001)`
  - `rules.buy_fee(amount, fees) -> float`、`rules.sell_fee(amount, fees) -> float`
  - `rules.apply_slippage(price, side, slippage) -> float`

- [ ] **Step 1: 写失败的测试 tests/test_rules.py**

```python
import pytest

from quant.engine import rules


def test_round_lot_main_board():
    assert rules.round_lot(1234, "600000.SH") == 1200
    assert rules.round_lot(99, "600000.SH") == 0


def test_round_lot_star_board():
    assert rules.round_lot(250, "688981.SH") == 250
    assert rules.round_lot(150, "688981.SH") == 0
    assert rules.round_lot(200, "688981.SH") == 200


def test_blocked_buy_and_sell():
    assert rules.blocked_buy(10.0, 10.0)
    assert not rules.blocked_buy(9.99, 10.0)
    assert not rules.blocked_buy(9.99, None)
    assert rules.blocked_sell(10.0, 10.0)
    assert not rules.blocked_sell(10.01, 10.0)
    assert not rules.blocked_sell(10.0, float("nan"))


def test_fees():
    fees = rules.FeeConfig()
    assert rules.buy_fee(100_000, fees) == pytest.approx(26.0)
    assert rules.sell_fee(100_000, fees) == pytest.approx(76.0)
    assert rules.buy_fee(10_000, fees) == pytest.approx(5.1)  # 触发最低佣金


def test_slippage():
    assert rules.apply_slippage(10.0, "buy", 0.001) == pytest.approx(10.01)
    assert rules.apply_slippage(10.0, "sell", 0.001) == pytest.approx(9.99)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_rules.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 rules.py**

```python
from __future__ import annotations

import math
from dataclasses import dataclass

from quant.utils.codes import board_of


def lot_rule(ts_code: str) -> tuple[int, int]:
    """返回（最小申报股数，递增股数）。科创板 200 股起、1 股递增。"""
    if board_of(ts_code) == "star":
        return 200, 1
    return 100, 100


def round_lot(shares: int, ts_code: str) -> int:
    minimum, increment = lot_rule(ts_code)
    if shares < minimum:
        return 0
    return (shares - minimum) // increment * increment + minimum


def _valid(value) -> bool:
    if value is None:
        return False
    try:
        return not math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def blocked_buy(raw_open: float, up_limit) -> bool:
    return _valid(up_limit) and raw_open >= float(up_limit) - 1e-9


def blocked_sell(raw_open: float, down_limit) -> bool:
    return _valid(down_limit) and raw_open <= float(down_limit) + 1e-9


@dataclass
class FeeConfig:
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.0005      # 仅卖出
    transfer_fee_rate: float = 0.00001  # 双边
    slippage: float = 0.001


def commission_of(amount: float, fees: FeeConfig) -> float:
    return max(amount * fees.commission_rate, fees.min_commission)


def buy_fee(amount: float, fees: FeeConfig) -> float:
    return commission_of(amount, fees) + amount * fees.transfer_fee_rate


def sell_fee(amount: float, fees: FeeConfig) -> float:
    return (commission_of(amount, fees)
            + amount * fees.stamp_tax_rate
            + amount * fees.transfer_fee_rate)


def apply_slippage(price: float, side: str, slippage: float) -> float:
    if side == "buy":
        return price * (1 + slippage)
    return price * (1 - slippage)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_rules.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/engine/rules.py tests/test_rules.py
git commit -m "feat: a-share trading rules and fee model"
```

---

### Task 13: 向量化回测引擎

**Files:**
- Create: `src/quant/engine/backtest.py`、`tests/test_backtest.py`

**Interfaces:**
- Consumes: `selection.compute_signals`、`loader.load_market_data/eligibility_mask`、`rules.*`
- Produces:
  - `backtest.BacktestConfig`（字段见下）
  - `backtest.Position(ts_code, shares, cost_price, buy_date)`
  - `backtest.BacktestResult(equity, trades, config, strategy_name)`；`equity` 列 `trade_date/equity/cash/benchmark`，`trades` 列 `trade_date/ts_code/side/price/shares/amount/fee`
  - `backtest.rebalance_dates(trade_dates, freq) -> list[str]`
  - `backtest.run_backtest(strategy, config, market=None, benchmark=None) -> BacktestResult`

- [ ] **Step 1: 写失败的测试 tests/test_backtest.py**

```python
import pandas as pd
import pytest
from pydantic import BaseModel

from quant.engine import backtest, rules
from quant.engine.backtest import BacktestConfig
from quant.strategies.base import EmptyParams, Strategy

ZERO_FEES = rules.FeeConfig(commission_rate=0.0, min_commission=0.0,
                            stamp_tax_rate=0.0, transfer_fee_rate=0.0,
                            slippage=0.0)


class AlwaysStrategy(Strategy):
    Params = EmptyParams

    def generate_signals(self, bars):
        return pd.Series(True, index=bars.index)


class FirstDayStrategy(Strategy):
    Params = EmptyParams

    def generate_signals(self, bars):
        return bars["trade_date"] == bars.iloc[0]["trade_date"]


def make_market(codes=("600000.SH",), dates=("20240101", "20240102", "20240103",
                                              "20240104", "20240105", "20240108"),
                price=10.0, up_limit=11.0, down_limit=9.0,
                is_st=False, is_new=False):
    rows = []
    for code in codes:
        for d in dates:
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": price, "high": price, "low": price, "close": price,
                "raw_open": price, "raw_close": price,
                "vol": 100.0, "amount": 10000.0, "adj_factor": 1.0,
                "turnover_rate": 1.0, "volume_ratio": 1.0, "pe_ttm": 10.0,
                "pb": 1.0, "total_mv": 100.0, "circ_mv": 80.0,
                "up_limit": up_limit, "down_limit": down_limit,
                "suspended": False, "is_st": is_st, "is_new": is_new,
                "board": "main",
            })
    return pd.DataFrame(rows)


def base_config(**overrides):
    cfg = dict(start="20240101", end="20240108", initial_cash=100_000.0,
               rebalance="daily", top_n=1, fees=ZERO_FEES)
    cfg.update(overrides)
    return BacktestConfig(**cfg)


def test_rebalance_dates_weekly_and_monthly():
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240201", "20240202"]
    assert backtest.rebalance_dates(dates, "weekly") == ["20240101", "20240108", "20240201"]
    assert backtest.rebalance_dates(dates, "monthly") == ["20240101", "20240201"]
    assert backtest.rebalance_dates(dates, "daily") == dates


def test_buy_at_next_open_with_correct_shares():
    market = make_market()
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)

    first_trade = result.trades.iloc[0]
    assert first_trade["trade_date"] == "20240102"
    assert first_trade["side"] == "buy"
    assert first_trade["shares"] == 10000
    assert result.trades["trade_date"].min() != "20240101"

    assert result.equity.iloc[0]["equity"] == pytest.approx(100_000.0)
    assert result.equity.iloc[-1]["equity"] == pytest.approx(100_000.0)


def test_limit_up_blocks_buy_until_tradable():
    market = make_market()
    market.loc[market["trade_date"] == "20240102", "up_limit"] = 10.0  # 次日开盘涨停
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)
    assert result.trades.iloc[0]["trade_date"] == "20240103"


def test_limit_down_postpones_sell():
    market = make_market()
    market.loc[market["trade_date"] == "20240103", "down_limit"] = 10.0  # 卖出日跌停
    result = backtest.run_backtest(FirstDayStrategy(), base_config(), market=market)
    sells = result.trades[result.trades["side"] == "sell"]
    assert len(sells) == 1
    assert sells.iloc[0]["trade_date"] == "20240104"


def test_universe_excludes_st_and_new():
    market = make_market(is_st=True)
    result = backtest.run_backtest(AlwaysStrategy(), base_config(), market=market)
    assert result.trades.empty

    market2 = make_market(is_new=True)
    result2 = backtest.run_backtest(
        AlwaysStrategy(), base_config(min_list_days=60), market=market2)
    assert result2.trades.empty
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_backtest.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 backtest.py**

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from quant.engine import loader, rules, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import Strategy


@dataclass
class BacktestConfig:
    start: str
    end: str
    initial_cash: float = 1_000_000.0
    rebalance: str = "weekly"
    top_n: int = 10
    rank_by: str = "amount"
    execution: str = "next_open"
    benchmark: str = "000300.SH"
    risk_free_rate: float = 0.0
    exclude_st: bool = True
    min_list_days: int = 60
    exclude_suspended: bool = True
    allowed_boards: tuple[str, ...] | None = None
    fees: rules.FeeConfig = field(default_factory=rules.FeeConfig)
    warmup_days: int = 0


@dataclass
class Position:
    ts_code: str
    shares: int
    cost_price: float
    buy_date: str


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    config: BacktestConfig
    strategy_name: str


def rebalance_dates(trade_dates: list[str], freq: str) -> list[str]:
    if freq == "daily":
        return list(trade_dates)
    df = pd.DataFrame({"trade_date": list(trade_dates)})
    dt = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    if freq == "weekly":
        key = dt.dt.to_period("W")
    elif freq == "monthly":
        key = dt.dt.to_period("M")
    else:
        raise ValueError(f"不支持的调仓频率：{freq}")
    df["key"] = key.astype(str)
    return df.groupby("key", sort=True)["trade_date"].first().tolist()


def filters_from_config(config: BacktestConfig) -> UniverseFilters:
    return UniverseFilters(
        exclude_st=config.exclude_st,
        min_list_days=config.min_list_days,
        exclude_suspended=config.exclude_suspended,
        allowed_boards=config.allowed_boards,
    )


def run_backtest(strategy: Strategy, config: BacktestConfig,
                 market: pd.DataFrame | None = None,
                 benchmark: pd.Series | None = None) -> BacktestResult:
    filters = filters_from_config(config)
    warmup = max(config.warmup_days, strategy.warmup_days)
    if market is None:
        market = loader.load_market_data(config.start, config.end,
                                         warmup_days=warmup, filters=filters,
                                         ensure=True)
    if market.empty:
        raise ValueError("回测区间内没有行情数据")

    all_dates = sorted(market["trade_date"].astype(str).unique().tolist())
    dates = [d for d in all_dates if config.start <= d <= config.end]
    if not dates:
        raise ValueError("回测区间内没有交易日")

    signals = selection.compute_signals(strategy, market, rank_by=config.rank_by)
    positive = signals[signals["signal"]]
    signal_by_date = {
        d: group.sort_values("score", ascending=False)["ts_code"].tolist()
        for d, group in positive.groupby("trade_date")
    }

    eligible = loader.eligibility_mask(market, filters)
    eligible_by_date = {
        d: set(group["ts_code"])
        for d, group in market[eligible].groupby("trade_date")
    }

    info = market.set_index(["trade_date", "ts_code"])[
        ["open", "raw_open", "up_limit", "down_limit", "suspended"]]
    close = market.pivot_table(index="trade_date", columns="ts_code",
                               values="close", aggfunc="last")
    close = close.reindex(all_dates).ffill()

    if benchmark is not None and not benchmark.empty:
        bench = benchmark.reindex(all_dates).ffill()
        bench_norm = bench / bench.dropna().iloc[0] * config.initial_cash
    else:
        bench_norm = pd.Series(float("nan"), index=all_dates)

    rebal = set(rebalance_dates(dates, config.rebalance))
    cash = config.initial_cash
    positions: dict[str, Position] = {}
    sell_queue: list[str] = []
    trade_rows: list[dict] = []
    equity_rows: list[dict] = []
    pending: list[str] | None = None

    def record_trade(date: str, code: str, side: str, price: float,
                     shares: int, fee: float) -> None:
        trade_rows.append({
            "trade_date": date, "ts_code": code, "side": side,
            "price": price, "shares": shares,
            "amount": price * shares, "fee": fee,
        })

    def try_sell(date: str, code: str) -> bool:
        nonlocal cash
        position = positions.get(code)
        if position is None or position.buy_date == date:
            return False
        row = info.loc[(date, code)] if (date, code) in info.index else None
        if row is None or bool(row["suspended"]):
            return False
        if rules.blocked_sell(row["raw_open"], row["down_limit"]):
            return False
        price = rules.apply_slippage(row["open"], "sell", config.fees.slippage)
        amount = price * position.shares
        fee = rules.sell_fee(amount, config.fees)
        cash += amount - fee
        record_trade(date, code, "sell", price, position.shares, fee)
        del positions[code]
        return True

    def try_buy(date: str, code: str, target_amount: float) -> None:
        nonlocal cash
        row = info.loc[(date, code)] if (date, code) in info.index else None
        if row is None or bool(row["suspended"]):
            return
        if rules.blocked_buy(row["raw_open"], row["up_limit"]):
            return
        price = rules.apply_slippage(row["open"], "buy", config.fees.slippage)
        if price <= 0:
            return
        desired = rules.round_lot(int(target_amount // price), code)
        cash_cap = rules.round_lot(
            int(cash // (price * (1 + config.fees.commission_rate
                                  + config.fees.transfer_fee_rate))), code)
        shares = min(desired, cash_cap)
        if shares <= 0:
            return
        amount = price * shares
        fee = rules.buy_fee(amount, config.fees)
        cash -= amount + fee
        positions[code] = Position(code, shares, price, date)
        record_trade(date, code, "buy", price, shares, fee)

    def process_sell_queue(date: str) -> None:
        for code in list(sell_queue):
            if code not in positions:
                sell_queue.remove(code)
            elif try_sell(date, code):
                sell_queue.remove(code)

    def execute_pending(date: str) -> None:
        desired = set(pending or [])
        for code in list(positions):
            if code not in desired and code not in sell_queue:
                sell_queue.append(code)
        process_sell_queue(date)

        idx = all_dates.index(date)
        prev_date = all_dates[idx - 1]
        equity_estimate = cash + sum(
            position.shares * float(close.loc[prev_date, position.ts_code])
            for position in positions.values()
        )
        target_amount = equity_estimate / config.top_n
        for code in pending or []:
            if code not in positions:
                try_buy(date, code, target_amount)

    for index, date in enumerate(all_dates):
        if date in dates:
            process_sell_queue(date)
            if pending is not None:
                execute_pending(date)
                pending = None
            if date in rebal and index + 1 < len(all_dates):
                ranked = signal_by_date.get(date, [])
                allowed = eligible_by_date.get(date, set())
                pending = [code for code in ranked if code in allowed][:config.top_n]

        equity = cash + sum(
            position.shares * float(close.loc[date, position.ts_code])
            for position in positions.values()
        )
        equity_rows.append({
            "trade_date": date,
            "equity": equity,
            "cash": cash,
            "benchmark": float(bench_norm.get(date, float("nan"))),
        })

    return BacktestResult(
        equity=pd.DataFrame(equity_rows),
        trades=pd.DataFrame(trade_rows, columns=["trade_date", "ts_code", "side",
                                                 "price", "shares", "amount", "fee"]),
        config=config,
        strategy_name=strategy.name or type(strategy).__name__,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_backtest.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/engine/backtest.py tests/test_backtest.py
git commit -m "feat: vectorized backtest engine with a-share rules"
```

---
---

### Task 14: 业绩指标与交易统计

**Files:**
- Create: `src/quant/engine/metrics.py`、`tests/test_metrics.py`

**Interfaces:**
- Consumes: `backtest.BacktestResult.equity/trades`（列名见 Task 13）
- Produces:
  - `metrics.max_drawdown(equity: pd.Series) -> float`
  - `metrics.annualized_return(equity: pd.Series) -> float`
  - `metrics.sharpe_ratio(returns: pd.Series, risk_free_rate=0.0) -> float`
  - `metrics.trade_stats(trades: pd.DataFrame) -> dict[str, float]`（`胜率`、`交易对数`）
  - `metrics.compute_metrics(equity, trades=None, risk_free_rate=0.0) -> dict[str, float]`

- [ ] **Step 1: 写失败的测试 tests/test_metrics.py**

```python
import pandas as pd
import pytest

from quant.engine import metrics


def test_max_drawdown_hand_computed():
    equity = pd.Series([100.0, 120.0, 90.0, 110.0])
    assert metrics.max_drawdown(equity) == pytest.approx(0.25)


def test_annualized_return_doubling_in_one_year():
    equity = pd.Series([100.0] * 253)
    equity.iloc[-1] = 200.0
    assert metrics.annualized_return(equity) == pytest.approx(1.0, abs=1e-6)


def test_sharpe_zero_volatility_returns_zero():
    returns = pd.Series([0.001] * 10)
    assert metrics.sharpe_ratio(returns) == 0.0


def test_trade_stats_win_rate():
    trades = pd.DataFrame([
        {"trade_date": "20240101", "ts_code": "A", "side": "buy",
         "price": 10.0, "shares": 100, "amount": 1000.0, "fee": 0.0},
        {"trade_date": "20240102", "ts_code": "A", "side": "sell",
         "price": 11.0, "shares": 100, "amount": 1100.0, "fee": 0.0},
    ])
    stats = metrics.trade_stats(trades)
    assert stats["胜率"] == pytest.approx(1.0)
    assert stats["交易对数"] == 1


def test_compute_metrics_keys_and_benchmark():
    equity = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104", "20240105"],
        "equity": [100.0, 101.0, 102.0, 103.0],
        "cash": [0.0, 0.0, 0.0, 0.0],
        "benchmark": [100.0, 100.5, 101.0, 101.5],
    })
    result = metrics.compute_metrics(equity)
    for key in ["累计收益", "年化收益", "最大回撤", "夏普比率", "卡玛比率",
                "月胜率", "基准收益", "超额收益", "超额最大回撤"]:
        assert key in result
    assert result["累计收益"] == pytest.approx(0.03)
    assert result["超额收益"] > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 metrics.py**

```python
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(-drawdown.min()) if len(drawdown) else 0.0


def annualized_return(equity: pd.Series) -> float:
    periods = len(equity) - 1
    if periods <= 0:
        return 0.0
    total = float(equity.iloc[-1]) / float(equity.iloc[0])
    if total <= 0:
        return -1.0
    return float(total ** (TRADING_DAYS / periods) - 1.0)


def sharpe_ratio(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    if returns.empty:
        return 0.0
    excess = returns - risk_free_rate / TRADING_DAYS
    std = float(excess.std(ddof=1))
    if not std or np.isnan(std):
        return 0.0
    return float(excess.mean() / std * np.sqrt(TRADING_DAYS))


def trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    """FIFO 配对买卖，统计胜率与交易对数。"""
    if trades is None or trades.empty:
        return {"胜率": 0.0, "交易对数": 0.0}
    lots: dict[str, list[list[float]]] = {}
    wins = 0
    closed = 0
    for _, trade in trades.sort_values("trade_date").iterrows():
        code = trade["ts_code"]
        if trade["side"] == "buy":
            lots.setdefault(code, []).append(
                [float(trade["price"]), float(trade["shares"]), float(trade["fee"])])
            continue
        remaining = float(trade["shares"])
        proceeds = float(trade["price"]) * remaining - float(trade["fee"])
        cost = 0.0
        while remaining > 0 and lots.get(code):
            lot = lots[code][0]
            used = min(lot[1], remaining)
            ratio = used / lot[1]
            cost += (lot[0] * lot[1] + lot[2]) * ratio
            lot[1] -= used
            remaining -= used
            if lot[1] <= 0:
                lots[code].pop(0)
        if cost > 0:
            closed += 1
            if proceeds > cost:
                wins += 1
    return {
        "胜率": wins / closed if closed else 0.0,
        "交易对数": float(closed),
    }


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame | None = None,
                    risk_free_rate: float = 0.0) -> dict[str, float]:
    series = equity["equity"].astype(float).reset_index(drop=True)
    returns = series.pct_change().dropna()
    result = {
        "累计收益": float(series.iloc[-1] / series.iloc[0] - 1.0),
        "年化收益": annualized_return(series),
        "最大回撤": max_drawdown(series),
        "夏普比率": sharpe_ratio(returns, risk_free_rate),
        "年化波动率": float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))
        if len(returns) > 1 else 0.0,
    }
    result["卡玛比率"] = (result["年化收益"] / result["最大回撤"]
                          if result["最大回撤"] > 0 else 0.0)

    monthly = series.copy()
    monthly.index = pd.to_datetime(equity["trade_date"], format="%Y%m%d")
    monthly_return = monthly.resample("ME").last().pct_change().dropna()
    result["月胜率"] = float((monthly_return > 0).mean()) if len(monthly_return) else 0.0

    if "benchmark" in equity.columns and equity["benchmark"].notna().any():
        bench = equity["benchmark"].astype(float).ffill()
        bench_norm = bench / bench.dropna().iloc[0]
        equity_norm = series / series.iloc[0]
        result["基准收益"] = float(bench_norm.iloc[-1] - 1.0)
        result["超额收益"] = float(equity_norm.iloc[-1] / bench_norm.iloc[-1] - 1.0)
        result["超额最大回撤"] = max_drawdown(equity_norm / bench_norm)

    if trades is not None and not trades.empty:
        result.update(trade_stats(trades))
        average_equity = float(series.mean())
        result["换手率"] = (float(trades["amount"].sum()) / average_equity
                            if average_equity else 0.0)
    return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_metrics.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/engine/metrics.py tests/test_metrics.py
git commit -m "feat: performance metrics and trade statistics"
```

---

### Task 15: 回测报告与 backtest 命令

**Files:**
- Modify: `src/quant/engine/report.py`、`src/quant/cli.py`
- Create: `configs/backtest/default.yaml`、`tests/test_report_backtest.py`、`tests/test_cli_backtest.py`

**Interfaces:**
- Consumes: `backtest.BacktestResult`、`backtest.filters_from_config`、`backtest.run_backtest`、`metrics.compute_metrics`、`loader.load_market_data/load_benchmark`
- Produces:
  - `report.plot_equity(equity: pd.DataFrame, path: Path, title: str) -> None`
  - `report.save_backtest(result, metrics: dict, out_root="outputs/backtest") -> Path`（生成 `equity.csv`/`trades.csv`/`metrics.json`/`equity.png`）
  - CLI `quant backtest -s <策略> --start <日期> --end <日期> [--config YAML] [-n 持仓数]`

- [ ] **Step 1: 写失败的测试 tests/test_report_backtest.py**

```python
from pathlib import Path

import pandas as pd

from quant.engine import report
from quant.engine.backtest import BacktestConfig, BacktestResult
from quant.engine.rules import FeeConfig


def _result():
    equity = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104"],
        "equity": [100.0, 101.0, 102.0],
        "cash": [0.0, 0.0, 0.0],
        "benchmark": [100.0, 100.5, 101.0],
    })
    trades = pd.DataFrame({
        "trade_date": ["20240102"], "ts_code": ["600000.SH"], "side": ["buy"],
        "price": [10.0], "shares": [100], "amount": [1000.0], "fee": [5.0],
    })
    config = BacktestConfig(start="20240102", end="20240104", fees=FeeConfig())
    return BacktestResult(equity=equity, trades=trades, config=config,
                          strategy_name="ma_volume")


def test_save_backtest_writes_all_artifacts(tmp_path):
    folder = report.save_backtest(_result(), {"累计收益": 0.02}, out_root=tmp_path)

    assert (folder / "equity.csv").exists()
    assert (folder / "trades.csv").exists()
    assert (folder / "metrics.json").exists()
    assert (folder / "equity.png").exists()
    assert (folder / "equity.png").stat().st_size > 0
    assert folder.name.startswith("ma_volume_")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_report_backtest.py -v`
Expected: FAIL（AttributeError: module ... has no attribute 'save_backtest'）

- [ ] **Step 3: 扩展 report.py 并新增默认回测配置**

在 `src/quant/engine/report.py` 追加：

```python
import json
from datetime import datetime


def plot_equity(equity: pd.DataFrame, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    x = pd.to_datetime(equity["trade_date"], format="%Y%m%d")
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(x, equity["equity"] / equity["equity"].iloc[0], label="策略净值")
    if "benchmark" in equity.columns and equity["benchmark"].notna().any():
        bench = equity["benchmark"].astype(float).ffill()
        ax.plot(x, bench / bench.dropna().iloc[0], label="基准净值", linestyle="--")
    ax.set_title(title)
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def save_backtest(result, metrics: dict,
                  out_root: str | Path = "outputs/backtest") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(out_root) / f"{result.strategy_name}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(folder / "equity.csv", index=False, encoding="utf-8-sig")
    result.trades.to_csv(folder / "trades.csv", index=False, encoding="utf-8-sig")
    (folder / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    plot_equity(result.equity, folder / "equity.png",
                f"{result.strategy_name} 回测净值")
    return folder
```

`configs/backtest/default.yaml`：

```yaml
initial_cash: 1000000
rebalance: weekly
top_n: 10
rank_by: amount
execution: next_open
benchmark: "000300.SH"
risk_free_rate: 0.0
exclude_st: true
min_list_days: 60
exclude_suspended: true
fees:
  commission_rate: 0.00025
  min_commission: 5.0
  stamp_tax_rate: 0.0005
  transfer_fee_rate: 0.00001
  slippage: 0.001
```

- [ ] **Step 4: 在 cli.py 增加 backtest 命令**

```python
@app.command("backtest")
def backtest_cmd(
    strategy: str = typer.Option(..., "--strategy", "-s", help="策略名"),
    start: str = typer.Option(..., "--start", help="开始日期"),
    end: str = typer.Option(..., "--end", help="结束日期"),
    config: str = typer.Option("configs/backtest/default.yaml", "--config",
                               help="回测参数 YAML"),
    top: int = typer.Option(None, "--top", "-n", help="持仓数量，覆盖配置"),
) -> None:
    """按策略回测并生成报告。"""
    from pathlib import Path

    import yaml

    from quant.engine import backtest as bt
    from quant.engine import loader, report
    from quant.engine.metrics import compute_metrics
    from quant.strategies.base import get_strategy, load_strategy_config
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    start_date, end_date = to_yyyymmdd(start), to_yyyymmdd(end)

    strategy_params: dict = {}
    strategy_config = Path("configs/strategies") / f"{strategy}.yaml"
    if strategy_config.exists():
        _, strategy_params = load_strategy_config(strategy_config)
    strat = get_strategy(strategy, **strategy_params)

    raw: dict = {}
    config_path = Path(config)
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw.update(start=start_date, end=end_date, warmup_days=strat.warmup_days)
    if top is not None:
        raw["top_n"] = top
    backtest_config = bt.BacktestConfig(**raw)

    market = loader.load_market_data(
        backtest_config.start, backtest_config.end,
        warmup_days=strat.warmup_days,
        filters=bt.filters_from_config(backtest_config), ensure=True)
    try:
        benchmark = loader.load_benchmark(backtest_config.benchmark,
                                          backtest_config.start, backtest_config.end)
    except ValueError as exc:
        typer.echo(f"提示：{exc}，将跳过基准对比")
        benchmark = None

    result = bt.run_backtest(strat, backtest_config, market=market, benchmark=benchmark)
    metric_values = compute_metrics(result.equity, result.trades,
                                    backtest_config.risk_free_rate)
    folder = report.save_backtest(result, metric_values)

    if result.trades.empty:
        typer.echo("回测区间内没有成交")
    else:
        typer.echo(f"成交笔数：{len(result.trades)}")
    typer.echo("指标：")
    for key, value in metric_values.items():
        typer.echo(f"  {key}: {value:.4f}" if isinstance(value, float) else f"  {key}: {value}")
    typer.echo(f"报告目录：{folder}")
```

- [ ] **Step 5: 写 tests/test_cli_backtest.py 并运行全部相关测试**

```python
import pandas as pd
from typer.testing import CliRunner

from quant.cli import app
from quant.engine.backtest import BacktestResult, BacktestConfig

runner = CliRunner()


def test_backtest_command_with_fakes(monkeypatch):
    equity = pd.DataFrame({"trade_date": ["20240102"], "equity": [100.0],
                           "cash": [0.0], "benchmark": [100.0]})

    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda *a, **k: pd.DataFrame({"dummy": [1]}))
    monkeypatch.setattr("quant.engine.loader.load_benchmark",
                        lambda *a, **k: pd.Series(dtype=float))
    monkeypatch.setattr("quant.engine.backtest.run_backtest",
                        lambda *a, **k: BacktestResult(
                            equity=equity, trades=pd.DataFrame(),
                            config=BacktestConfig(start="20240102", end="20240102"),
                            strategy_name="ma_volume"))
    monkeypatch.setattr("quant.engine.report.save_backtest",
                        lambda *a, **k: "outputs/backtest/fake")

    result = runner.invoke(app, ["backtest", "-s", "ma_volume",
                                 "--start", "2024-01-02", "--end", "2024-01-03"])

    assert result.exit_code == 0
    assert "报告目录" in result.output
```

Run: `uv run pytest tests/test_report_backtest.py tests/test_cli_backtest.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/quant/engine/report.py src/quant/cli.py configs/backtest/default.yaml tests/test_report_backtest.py tests/test_cli_backtest.py
git commit -m "feat: backtest command with equity/csv/json/png report"
```

---

### Task 16: HighTightFlag 策略

**Files:**
- Create: `src/quant/strategies/high_tight_flag.py`、`configs/strategies/high_tight_flag.yaml`、`tests/test_strategy_high_tight_flag.py`

**Interfaces:**
- Consumes: `strategies.base.Strategy/register_strategy`
- Produces: 注册名 `"high_tight_flag"`，参数 `lookback=120, min_gain=0.3, tight_days=10, max_range=0.08, vol_shrink=0.6, vol_base_days=20, high_days=250, near_high=0.05`

- [ ] **Step 1: 写失败的测试 tests/test_strategy_high_tight_flag.py**

```python
import pandas as pd

from quant.strategies import get_strategy

SMALL_PARAMS = dict(lookback=5, min_gain=0.2, tight_days=3, max_range=0.05,
                    vol_shrink=0.5, vol_base_days=3, high_days=5, near_high=0.05)


def make_bars(closes, vols):
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(len(closes))],
        "open": closes,
        "high": [c + 0.05 for c in closes],
        "low": [c - 0.05 for c in closes],
        "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_high_tight_flag_true_on_last_bar():
    closes = [10, 10, 10, 10, 10, 13, 13.5, 13.2, 13.3, 13.4]
    vols = [100, 100, 100, 100, 100, 300, 300, 300, 20, 20]
    s = get_strategy("high_tight_flag", **SMALL_PARAMS)

    signals = s.generate_signals(make_bars(closes, vols))

    assert list(signals)[-1] is True or signals.iloc[-1]
    assert not signals.iloc[:-1].any()


def test_high_tight_flag_false_without_volume_dry_up():
    closes = [10, 10, 10, 10, 10, 13, 13.5, 13.2, 13.3, 13.4]
    vols = [100, 100, 100, 100, 100, 300, 300, 300, 300, 300]
    s = get_strategy("high_tight_flag", **SMALL_PARAMS)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_high_tight_flag_warmup():
    s = get_strategy("high_tight_flag")
    assert s.warmup_days == 260
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_strategy_high_tight_flag.py -v`
Expected: FAIL（KeyError: 未注册的策略）

- [ ] **Step 3: 实现 high_tight_flag.py 与 YAML**

`src/quant/strategies/high_tight_flag.py`：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class HighTightFlagParams(BaseModel):
    lookback: int = 120
    min_gain: float = 0.3
    tight_days: int = 10
    max_range: float = 0.08
    vol_shrink: float = 0.6
    vol_base_days: int = 20
    high_days: int = 250
    near_high: float = 0.05


@register_strategy("high_tight_flag")
class HighTightFlag(Strategy):
    """高位紧缩旗形：前期大涨 + 近期窄幅缩量整理 + 靠近 52 周高点。"""

    Params = HighTightFlagParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = max(self.p.high_days, self.p.lookback) + self.p.tight_days

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close, high, low, vol = bars["close"], bars["high"], bars["low"], bars["vol"]

        gain = close / close.shift(p.lookback) - 1.0
        uptrend = gain >= p.min_gain

        tight_high = high.rolling(p.tight_days).max()
        tight_low = low.rolling(p.tight_days).min()
        tight = (tight_high - tight_low) / tight_low <= p.max_range

        vol_now = vol.rolling(p.tight_days).mean()
        vol_base = vol.shift(p.tight_days).rolling(p.vol_base_days).mean()
        quiet = vol_now <= p.vol_shrink * vol_base

        high_window = close.rolling(p.high_days).max()
        near = close >= (1 - p.near_high) * high_window

        signal = uptrend & tight & quiet & near
        return signal.fillna(False)
```

`configs/strategies/high_tight_flag.yaml`：

```yaml
strategy: high_tight_flag
params:
  lookback: 120
  min_gain: 0.3
  tight_days: 10
  max_range: 0.08
  vol_shrink: 0.6
  vol_base_days: 20
  high_days: 250
  near_high: 0.05
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_strategy_high_tight_flag.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/quant/strategies/high_tight_flag.py configs/strategies/high_tight_flag.yaml tests/test_strategy_high_tight_flag.py
git commit -m "feat: high_tight_flag strategy"
```

---

### Task 17: 集成测试、README 与人工验收

**Files:**
- Create: `tests/test_integration.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: 全部模块
- Produces: 可复制的使用文档；真实环境集成测试入口

- [ ] **Step 1: 写集成测试 tests/test_integration.py**

```python
import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION"),
                       reason="需要 RUN_INTEGRATION=1 才运行集成测试"),
]


def test_mysql_core_tables_reachable():
    from quant.data import db

    count = db.scalar("SELECT COUNT(*) FROM trade_cal")
    assert count and count > 0


def test_tushare_fetch_trade_cal():
    from quant.data.tushare_client import TushareClient

    df = TushareClient().fetch_trade_cal("20240101", "20240110")
    assert not df.empty
```

- [ ] **Step 2: 运行单元测试全量确认**

Run: `uv run pytest -q`
Expected: 全部 passed（集成测试自动 skip）

- [ ] **Step 3: 编写 README.md**

README 内容（替换空文件）：

```markdown
# my-a-stock-quant

个人 A 股量化选股与回测系统：Tushare 日线数据 → 阿里云 MySQL → 本地 Parquet 缓存 → 策略选股 / 回测报告。

## 环境准备

1. 安装 [uv](https://docs.astral.sh/uv/)，执行 `uv sync`
2. 复制 `.env.example` 为 `.env`，填入阿里云 RDS 与 Tushare token
3. `uv run quant data init-db` 建表
4. `uv run quant data update` 首次全量入库（缺省按交易日历增量，可中断续跑）

## 常用命令

```bash
uv run quant data status                         # 查看各表行数/水位线/缓存状态
uv run quant data update -t daily --from-date 2024-01-02 --to-date 2024-01-31
uv run quant list                                # 已注册策略
uv run quant select -s ma_volume -n 20           # 选股（默认最新交易日）
uv run quant backtest -s ma_volume --start 2021-01-01 --end 2026-09-17
```

## 策略参数

- `configs/strategies/<策略名>.yaml`：策略参数（CLI 会自动读取同名文件）
- `configs/backtest/default.yaml`：回测默认参数（费用、调仓、持仓数、股票池过滤等）
- 新增策略：在 `src/quant/strategies/` 新建文件，用 `@register_strategy("名称")` 装饰类并实现 `generate_signals`

## 回测约定（重要）

- 信号在调仓日收盘产生，次日开盘成交；涨停买不进、跌停卖不掉顺延、T+1、整手规则均已实现
- 成交与估值使用后复权价，等效分红再投资；涨跌停/停牌判定使用原始价
- 输出：`outputs/backtest/<策略>_<时间戳>/`（equity.csv / trades.csv / metrics.json / equity.png）

## 测试

```bash
uv run pytest                              # 单元测试
$env:RUN_INTEGRATION=1; uv run pytest -m integration   # 真实 MySQL/Tushare 集成测试
```

## 风险提示

本系统仅用于个人研究与学习，不构成任何投资建议。回测结果不代表未来收益。
```

- [ ] **Step 4: 真实数据人工验收（按 M1–M4 验收标准）**

依次执行并确认：

1. `uv run quant data init-db` → 输出 10 张表
2. `uv run quant data update -t trade_cal` → 水位线非空
3. `uv run quant data update -t daily --from-date <近一年起点>` → 中断后再执行一次，确认从断点续跑
4. `uv run quant select -s ma_volume -n 10` → 输出真实名单并生成 `outputs/select/*.csv`
5. `uv run quant backtest -s ma_volume --start <一年前> --end <今天>` → 生成 4 个报告文件，指标合理（无负净值、回撤在 0–1 之间）
6. 用 `high_tight_flag` 重跑第 4、5 步

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py README.md
git commit -m "docs: usage readme and integration tests"
```

---

## 验收总表（对应 spec 里程碑）

| 里程碑 | 验收 | 对应 Task |
|---|---|---|
| M1 骨架 | `quant data status` 连库输出 | 1–3 |
| M2 数据 | 全量/增量入库 + 缓存 + 断点续跑 | 4–7 |
| M3 策略+选股 | `quant select` 出真实名单 | 8–11 |
| M4 回测 | 4 个报告文件 + 指标 | 12–15 |
| M5 收尾 | 两个策略 + 集成测试 + README | 16–17 |

## 计划自审记录

- **Spec 覆盖**：数据 9 表 + 水位线（Task 3/5/6）、复权与股票池（Task 10）、策略框架与两策略（Task 8/9/16）、选股（Task 11）、A 股交易规则（Task 12/13）、指标与报告（Task 14/15）、CLI/配置/日志（Task 3/7/15）、安全处理 `.env` 与 `test_db.py`（Task 1）、测试策略与集成测试（各 Task + Task 17）。
- **占位符**：无 TBD/TODO；所有代码步骤含完整实现。
- **类型一致性**：`BacktestConfig` 由 Task 13 定义并在 Task 15 使用；`BacktestResult` 字段在 Task 13/14/15 一致；`filters_from_config` 公开函数供 CLI 使用；`compute_signals` 的列名在 Task 11/13 一致。
- **已知简化**（与 spec 一致）：成交与估值用后复权价（分红再投资）；买入遇涨停/停牌当日放弃（不持续挂单）；现金不计息；科创板整手规则已实现。
