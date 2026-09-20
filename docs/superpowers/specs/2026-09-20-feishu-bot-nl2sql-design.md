# 飞书机器人自然语言查询（text-to-SQL）设计

日期：2026-09-20
状态：待用户审阅

## 1. 背景与目标

在飞书群里 @机器人 提一个自然语言问题（如"博敏电子最近几天命中策略的情况是怎么样的"），机器人：

1. 用大模型把问题翻译成一条只读 SQL；
2. 在 MySQL 上执行（白名单表、只读事务、限行限时）；
3. 用大模型把查询结果总结成中文回答；
4. 以交互卡片回复在该提问下（正文为总结，末尾附"查询依据"= 返回行数 + SQL）。

一次问答即结束，不需要多轮上下文。

## 2. 范围与非目标

范围内：

- 飞书**长连接**（WebSocket）事件订阅，无需公网地址
- `im.message.receive_v1` 文本消息，私聊直接提问、群聊 @机器人
- 前台常驻命令 `quant bot serve`；本地免飞书自测命令 `quant bot ask "<问题>"`
- 大模型：DeepSeek OpenAI 兼容接口，模型 `deepseek-flash`

非目标（本次不做）：

- 多轮对话 / 上下文记忆 / 会话状态
- 任何写操作（INSERT/UPDATE/DELETE/DDL）与建库建表
- 定时任务自启、进程守护（先手动前台运行）
- 用户白名单、按人限流、图表/图片输出、流式输出

## 3. 架构与数据流

```
飞书长连接 (lark-oapi ws.Client)
        │  im.message.receive_v1
        ▼
bot/feishu.py 解析事件 → Question(question, chat_id, message_id, sender_open_id, is_group)
        │  （非文本忽略；去 @占位符；空问题回提示；message_id 去重；机器人自己发的忽略）
        ▼
bot/qa.py  answer(question, llm, db)
        ├─ ① bot/schema.py  : 白名单 DDL 文本（进程内缓存）
        ├─ ② bot/llm.py     : 生成 SQL（JSON: {"sql", "explain"}）
        ├─ ③ bot/sql_guard.py: validate_sql → 安全 SQL（或拒绝原因）
        ├─ ④ bot/exec.py    : 只读事务执行，read_timeout，返回 DataFrame
        └─ ⑤ bot/llm.py     : 结果 → 中文总结
        ▼
bot/cards.py Answer → 飞书交互卡片 → bot/feishu.py 回复消息 API
```

并发：事件回调里只做解析，问答丢到 `ThreadPoolExecutor(max_workers=2)` 执行后立即返回，避免阻塞长连接心跳；同一 `message_id` 只答一次（LRU 200）。

## 4. 模块与接口

新增包 `src/quant/bot/`：

| 文件 | 接口 | 职责 |
|---|---|---|
| `schema.py` | `allowed_tables() -> list[str]`、`schema_prompt() -> str` | 白名单 = `schemas.TABLES` + `schemas.HIT_TABLES` + `ingest_log`（随代码自动同步）；从 `information_schema.COLUMNS` 读列名/类型/注释拼成 DDL 文本，进程内缓存 |
| `llm.py` | `class DeepSeekClient(api_key, base_url, model)`、`.complete(messages, max_tokens) -> str` | stdlib `urllib` 调 `/chat/completions`（非流式）；超时 60s；网络错误重试 2 次；取 `choices[0].message.content`，为空时回退 `reasoning_content`；仍为空则抛错 |
| `sql_guard.py` | `validate_sql(sql) -> str`、`class SqlRejected(ValueError)` | 见第 5 节 |
| `exec.py` | `run_readonly(sql, timeout=15) -> pd.DataFrame` | 独立 pymysql 连接：`START TRANSACTION READ ONLY` → `execute` → `fetchall` → `rollback`；`read_timeout`/`connect_timeout` |
| `qa.py` | `Answer(text, sql, row_count, ok, note)`、`answer(question, llm, runner) -> Answer` | 编排 ①~⑤，LLM 与执行器依赖注入（`runner(sql) -> pd.DataFrame`，便于测试）；把结果渲染成紧凑文本（列名 + 制表符分隔行），超过 200 行或 8000 字符截断并注明 |
| `cards.py` | `build_answer_card(question, answer, at_open_id=None) -> dict` | 交互卡片：蓝色头（失败灰头）`【问数】<问题>`；正文 `lark_md` 总结；末尾小字"查询依据：返回 N 行 · SQL"（SQL 用代码块）；群聊在正文前加 `<at id=ou_xxx></at>` |
| `feishu.py` | `parse_event(data) -> Question \| None`、`strip_mentions(text) -> str`、`reply_card(client, message_id, card) -> None`、`run_bot() -> None` | lark-oapi 封装：事件解析与清洗、`message_id` 去重（LRU 200）、`im.v1.message.reply`、组装 `ws.Client` 并阻塞运行 |

`parse_event` 只在"非文本消息 / 机器人自己发的消息"时返回 `None`（直接忽略）；文本消息一律返回 `Question`，其中 `question` 清洗后可能为空字符串，由调用方回复"请把问题写在 @我 之后"。

CLI（`src/quant/cli.py` 新增子命令组）：

- `quant bot serve`：校验配置 → 启动长连接常驻（日志 INFO 到控制台）
- `quant bot ask "<问题>"`：不连飞书，直接跑 ①②③④⑤ 并打印回答 + SQL（本地联调用）

## 5. SQL 安全模型

四层防护：

1. **表白名单**：提示词里只给白名单表的 DDL；校验时提取 SQL 中出现的所有表名（`FROM`/`JOIN` 后的标识符，含反引号），不在白名单内即拒绝（含 `information_schema`、`mysql` 等系统库）。
2. **语句形态**：去掉注释后必须是**单条**语句（不允许 `;` 分隔多条）；必须以 `SELECT` 或 `WITH` 开头；黑名单关键字（不区分大小写、按词边界匹配）：`INSERT UPDATE DELETE REPLACE DROP ALTER CREATE TRUNCATE RENAME GRANT REVOKE SET CALL LOAD OUTFILE DUMPFILE SLEEP BENCHMARK GET_LOCK INTO`。
3. **行数上限**：无 `LIMIT` 时追加 `LIMIT 200`；有 `LIMIT` 且大于 200 时收紧为 200。
4. **执行层**：`START TRANSACTION READ ONLY`（InnoDB 层强制只读）+ `read_timeout=15` + 结果只取前 200 行。

提示词口径说明（避免模型误用）：日期为 `CHAR(8)` 字符串 `YYYYMMDD`；`daily` 为不复权行情、`amount` 单位为千元；`hit_*` 表中 `close` 为后复权价、`raw_close` 为不复权价、`name`/`industry` 为入库快照；`hit_*` 表水位线在 `ingest_log`（`task_name='hit_<策略>'`）；最新交易日从 `trade_cal`（`is_open=1`）取。

## 6. 配置（`.env`，已在 .gitignore）

```
feishu_app_id=cli_xxx
feishu_app_secret=xxx
deepseek_api_key=sk-xxx
deepseek_base_url=https://api.deepseek.com
deepseek_model=deepseek-flash
```

`config.Settings` 新增上述 5 个字段（均可选，缺省时 `quant bot serve` 报错并提示；`deepseek_base_url` 默认 `https://api.deepseek.com`，`deepseek_model` 默认 `deepseek-flash`）。`.env.example` 同步占位符（不含真实值）。

已实测：`GET /models` 返回 `deepseek-flash`、`deepseek-v4-pro`；`deepseek-flash` 为推理模型（响应含 `reasoning_content`），故 `max_tokens` 取 4096，`temperature=0`。

## 7. 飞书控制台前置条件（用户一次性操作）

1. 事件订阅方式选择「长连接」，订阅事件 `im.message.receive_v1`（接收消息）
2. 权限：`im:message`（读消息，群聊还需 `im:message.group_at_msg:readonly`）、`im:message:send_as_bot`（回复）
3. 发布应用版本使其生效；机器人需被拉进目标群

## 8. 错误处理与失败路径

| 情况 | 行为 |
|---|---|
| 非文本消息（图片/文件等） | 忽略 |
| 清洗后问题为空 | 回复提示"请把问题写在 @我 之后" |
| 生成 SQL 失败 / JSON 解析失败 | 回复失败卡片（含模型原文片段），进程继续 |
| 校验拒绝（越权表/写操作/多语句） | 回复"拒绝执行"+ 原因 + 模型生成的 SQL |
| SQL 执行报错（语法/超时） | 回复失败卡片 + SQL + 错误摘要 |
| 结果 0 行 | 正常回复"没有查到数据"+ SQL |
| LLM 超时/网络错误 | 回复"大模型暂时不可用，请稍后再问" |
| 飞书回复失败 | 记日志，不影响进程与后续消息 |
| 长连接断开 | 交给 lark-oapi 自动重连 |

## 9. 测试策略

TDD，全部离线（fake LLM / fake runner / 假事件对象）：

- `tests/test_bot_sql_guard.py`：放行（`SELECT`/`WITH`/带 `LIMIT`/带反引号与子查询）；拒绝（多语句、写操作、DDL、越权表、系统库、`INTO OUTFILE`、`SLEEP`）；`LIMIT` 追加与收紧
- `tests/test_bot_qa.py`：正常链路（生成→校验→执行→总结）；校验拒绝；执行异常；空结果；行/字符截断；LLM 返回带 ```json 围栏的容错
- `tests/test_bot_feishu.py`：事件解析（文本/非文本/机器人自己/群聊与私聊）、`strip_mentions`、去重、卡片结构（含 @、SQL 代码块、失败态）
- `tests/test_bot_llm.py`：响应解析（`content` 空回退 `reasoning_content`）、超时重试、错误码
- `tests/test_cli_bot.py`：`bot ask` 输出、缺配置时报错
- 集成（手动）：`quant bot ask` 本地跑通 → `quant bot serve` + 群里 @机器人 实测

## 10. 验收标准

1. `uv run quant bot ask "博敏电子最近几天命中策略的情况"` 打印合理回答与 SQL
2. 群里 @机器人 提问，数十秒内收到回复卡片（含总结、行数、SQL），且挂在提问下
3. 越权/写操作类 SQL 被拒绝并回复原因，数据库无任何写动作
4. 模型产出坏 SQL 或网络异常时，回复失败提示且进程不退出
5. 既有测试全部通过（380 passed / 3 skipped），新增测试全绿

## 11. 依赖变更

新增 `lark-oapi`（含 `websockets`、`protobuf` 等传递依赖），其余全部使用标准库与现有依赖（`pymysql`、`pandas`、`pydantic-settings`、`typer`）。实现第一步：安装 `lark-oapi` 并核对其真实 API（`ws.Client`、`EventDispatcherHandler`、`im.v1.message.reply`）后再编码。
