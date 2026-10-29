# AGENTS.md

个人 A 股量化系统。README.md 是权威操作指南（中文）；本文件只记录容易踩的坑。

## 环境与验证

- Python 3.12 由 `uv` 管理。始终用 `uv run ...`；初始化用 `uv sync`。
- 涉及 MySQL/Tushare 的操作需要 `.env`（从 `.env.example` 复制）。单元测试离线运行，无需 `.env`。
- 仓库未配置 lint、格式化、类型检查或 CI。用以下命令验证：
  - `uv run pytest -q`（全部离线测试）
  - `uv run pytest tests/test_hits.py::test_name -q`（单个测试）
  - `$env:RUN_INTEGRATION=1; uv run pytest -m integration`（真实 MySQL/Tushare）
- 保持现有风格：CLI/帮助/日志/用户文案和文档用中文；提交信息用英文 Conventional Commits（`fix: ...`、`feat: ...`、`docs: ...`）。
- 本地网络环境：有时会开启代理

## 入口与数据流

- `quant` 命令 -> `src/quant/cli.py:app`。CLI 会切换到仓库根目录，因此 `.env` 和 `configs/*` 能正确解析。直接调用库函数时也必须在仓库根目录运行。
- Tushare -> MySQL（数据真源）-> `data_cache/` 下的 Parquet 镜像（`QUANT_CACHE_DIR` 可覆盖）-> `engine/loader.py` -> 策略/选股/回测。
- `quant data update` 只写 MySQL。以下命令会通过 `ensure_all` / `ensure=True` 刷新镜像：`select`、`hits update`、`limit update`、`backtest`。`data status`、`recommend`、`notify` 不会刷新镜像。运行 `recommend` / `notify` 前先执行 `hits update`；默认 `notify` 的“今日最终推荐”还依赖本地 `index_daily` 缓存，缓存缺失时先执行 `quant data update -t index_daily`。
- 日期在 MySQL 中是 `CHAR(8)` 的 `YYYYMMDD`；CLI 也接受 `YYYY-MM-DD`。`amount` 单位是千元，`circ_mv` 单位是万元。行情面板包含后复权 `open/high/low/close` 和 `raw_*`；涨跌停判断用 `raw_close` 对 `raw_pre_close`（交易所除权后昨收，或 `up_limit/down_limit`），停牌判断用 `suspended`。
- `src/quant/data/schemas.py` 是表注册表：`TABLES`（入库表）、`HIT_TABLES`（由 `HIT_STRATEGIES` 派生）、`LIMIT_TABLES`（涨跌停派生表）。`quant data init-db` 幂等。已有表加列需要在 `MIGRATIONS`/`WIDENINGS`（hit 表用 `HIT_ADDED_COLUMNS`）登记；新增源表需要 `ingest.SPECS` 条目；否则现有库或 `data update` 会漏掉。
- `tmp_scripts/` 被 gitignore；持久逻辑应放在 `src/quant`。重建 CLI 逻辑在 `maintenance.py`（`parse_rebuild_args` / `run_rebuild_cli`），报告辅助函数在 `engine/`；`tests/test_rebuild_script.py`、`tests/test_idea_backtest_report.py`、`tests/test_multi_strategy_report.py` 只测库函数，不依赖本地 `tmp_scripts/`，全新克隆也能通过。

## 策略与命中表

- 在 `src/quant/strategies/` 放非下划线开头的模块并用 `@register_strategy`，会自动发现；`_name.py` 表示禁用发现。
- 实际生效参数来自 `configs/strategies/<name>.yaml`；`quant list` 只打印代码默认值。修改 `Params` 字段时必须同步 YAML。
- 因果性：策略拿到的是整段已加载历史，包含信号日之后的行。只能使用当前/过去行；禁止负向 `shift`、全序列归一化、未来窗口。选股和部分策略有因果性测试。横截面策略实现 `prepare(market)` 且必须返回副本；`rank()` 可覆盖默认的 `amount` 排序分。
- 每个已注册策略都必须出现在 `HIT_STRATEGIES`，否则 `hits update -s all` 会指向不存在的表。增删名字时同步更新 `tests/test_schemas.py` 的元组断言。
- `hit_<策略>` 表是派生数据：不参与 Tushare 抓取、缓存或夜间全量重建清空。`hits update -s X` 从 `ingest_log` 增量写入；缺省从 2018-01-01 起，显式 `--from-date` 会先删除再重算该区间。改策略逻辑或参数后必须传 `--from-date`（如 2018-01-01），否则旧行会残留。
- 新增策略还需要 `tests/test_strategy_<name>.py`、README 策略表一行，然后执行 `data init-db` 和 `hits update -s <name> --from-date 2018-01-01`。完整清单见 README「策略变更标准操作流程」。
- `limit_up_daily` / `limit_down_daily` 是行情派生表：只统计收盘封板，严格按 `60`/`00` 开头选主板，非当日 ST、非次新（上市满 60 个交易日）、非停牌；`raw_close` 对 `stk_limit.up_limit/down_limit` 判定。`quant limit update` 从 `ingest_log` 增量写入，显式 `--from-date` 先删区间再重算；水位线只前进。
- 入库水位线只前进；显式 `--from-date` 会重拉更早日期，但不会回退水位线。

## 回测与选股不变量

- 信号在调仓日收盘产生，默认次日开盘成交；强制 T+1；涨停买不进；跌停卖不掉会排队重试；主板 100/100 股整手，科创板 200/1 股；退市持仓强制结算。
- 成交和估值用复权价；涨跌停判断用原始价对 `up_limit/down_limit`，停牌判断用 `suspended`。`top_n` 为空表示等权买入全部信号股（`-n 0` 也是全部）。周调仓 = 每周第一个交易日。
- 默认股票池：主板、非 ST、上市满 60 日、非停牌。`select --boards all`（或逗号列表）可放宽。`loader` 以 `stock_basic` 的 `ts_code` 为准过滤旧代码别名（如 `000022.SZ`、`000043.SZ`、`300114.SZ`）。
- `recommend` 和默认 `notify` 读取 `hit_*` 表，并要求所有 `HIT_STRATEGIES` 水位线 >= 目标日期，否则报错。`recommend` 使用 `ignore_gate=True`，闸门关闭时仍列出股票；展示的 7 个指数状态是真实的。规则在 `configs/recommend.yaml`（`max_picks` 是顶层字段，分层配置在 `tiers` 下）。
- `notify` 默认发 2 张卡（今日最终推荐 + 多策略共振）；`-s <策略名>` 发该策略卡 + 多策略共振，`-s all` 发全部策略卡 + 多策略共振 + 5日复合共振；`--no-co` 跳过共振类卡片。`--dry-run` 不需要 webhook；真实发送需要 `FEISHU_WEBHOOK_URL`。

## 飞书机器人

- `quant bot ask` 本地跑通链路；`quant bot serve` 需要飞书应用凭据和 DeepSeek key。
- 模型生成的 SQL 不可信：必须通过 `bot/sql_guard.py`（单条 SELECT/WITH、关键字黑名单、表白名单）和 `bot/runner.py`（只读事务、15 秒超时、最多 200 行）。不要绕过校验。
- 机器人白名单由 `TABLES + HIT_TABLES + LIMIT_TABLES` 自动生成。

## 参考文档

- `README.md`：命令、每日流程、策略新增/修改/禁用/删除、回测约定、数据升级与重建。
- `docs/my-strategies.md`：各策略公式与参数（以 YAML 为准）。
- `docs/TushareAPI.md`：Tushare 上游字段参考。
- 历史 `docs/superpowers/` 方案/规格已删除，不再维护；以 README、`docs/my-strategies.md` 和当前代码为准。
