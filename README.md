# my-a-stock-quant

个人 A 股量化选股与回测系统：Tushare 日线数据 → 阿里云 MySQL → 本地 Parquet 缓存 → 策略选股 / 回测报告。

## 环境准备

1. 安装 [uv](https://docs.astral.sh/uv/)，执行 `uv sync`
2. 复制 `.env.example` 为 `.env`，填入阿里云 RDS 与 Tushare token（飞书通知可选：填 `feishu_webhook_url`，机器人开启签名校验再加 `feishu_webhook_secret`，见「飞书通知」）
3. `uv run quant data init-db` 建表
4. `uv run quant data update` 首次全量入库（缺省按交易日历增量，可中断续跑）

## 常用命令

```bash
uv run quant data status                         # 查看各表行数/水位线/缓存状态
uv run quant data update -t daily --from-date 2024-01-02 --to-date 2024-01-31
uv run quant list                                # 已注册策略
uv run quant select -s ma_volume                 # 选股（默认最新交易日、只选主板、输出全部命中）
uv run quant select -s ma_volume -n 50           # 只输出前 50 只
uv run quant select -s ma_volume --boards all    # 不限板块（main,gem,star,bse 可逗号组合）
uv run quant backtest -s ma_volume --start 2021-01-01 --end 2026-09-17   # 默认等权买入全部信号股
uv run quant hits update -s all                  # 回填/增量写入 hit_<策略> 历史命中表
uv run quant notify                              # 飞书推送当日卡片（7 策略 + 同日共振 + 5日复合共振）
uv run quant notify -s ma_volume --dry-run       # 只打印不发送，先预览
```

## 交易日盘后流程

```bash
uv run quant data update     # 1. 行情增量入库（建议交易日 18:00 后执行）
uv run quant hits update     # 2. 各策略命中表增量写入（可 -s 指定策略）
uv run quant data status     # 3. 校验各 hit_<策略> 水位线已追平最新交易日
uv run quant notify          # 4. 飞书推送当日卡片（可选）
```

- 更新与推送彼此独立：`notify` 只读命中表；推送已入库的历史日期用 `-d`，无需先跑数据更新；推送前可用 `--dry-run` 预览
- 本地 `scripts/daily_update.py` 一键执行前 3 步（等价命令；`scripts/` 未纳入版本管理）

## 策略历史命中（hit_&lt;策略&gt;）

- `uv run quant hits update -s all` 把每个策略历史上每日全部命中的股票写入对应 `hit_<策略名>` 表；默认从 2024-01-01 回填，水位线存 `ingest_log`，可增量续跑、幂等重跑
- 显式传 `--from-date` 视为重算：先清空该区间旧命中再重新计算（策略口径调整后用它刷新历史）
- 口径与 `select` 一致：沪深主板、非ST、上市≥60日、非停牌；`rank` 按 score 降序编号，`score` 为该策略排序分（成交额 / 流通市值 / RPS），`params` 保存当次运行的策略参数快照
- 字段：`industry`（行业快照）、`prev_hit`（同策略上一交易日是否命中）、`hit_3d/hit_5d/hit_10d`（同策略前 3/5/10 个交易日命中天数，不含当日、停牌日占窗口）、`streak`（同策略连续命中天数，含当日）
- 历史列只统计**表内已记录的命中**（即 2024-01-02 起），每行都可用表自身复核；命中计算加载完整历史面板，结果与水位线、重跑次数无关（单日 `select` 对暖机期内长期停牌股可能略有差异）
- 命中表是派生数据：不参与 Tushare 抓取、本地 Parquet 缓存与夜间全量重建清空；`quant data status` 可查看行数与水位线

## 飞书通知

- 在 `.env` 配置自定义机器人 webhook：`feishu_webhook_url`（必填）；机器人开启"签名校验"时再加 `feishu_webhook_secret`（可选，自动加签）
- `uv run quant notify`：读取各 `hit_<策略>` 表当日记录，每策略一条交互卡片推送飞书群（无命中也会发"今日无命中"，卡片头置灰）；`-s` 选策略（默认全部）、`-d` 指定交易日（默认最新）、`--dry-run` 只打印不发送、`--no-co` 跳过两张共振类卡片
- 附带的「多策略共振」卡片：当日被 ≥2 个策略命中的股票（始终按全部已注册策略统计），按策略数降序、成交额降序，每行附命中策略，橙色卡片头
- 附带的「5日复合共振（当日含 rise_shrink_pullback）」卡片：当日命中 `rise_shrink_pullback`、且近 5 个交易日（含当日）命中过其他策略的股票，按其他策略数降序、成交额降序，行内列出策略名与命中日期（如 `rps_breakout(09-15、09-16)`），紫色卡片头
- 命中表未追平目标日期时报错并提示先执行 `quant hits update`，不会把"未更新"误报成"无命中"；单条发送失败不阻断其余策略（网络类错误自动重试 2 次、间隔 1 秒），有失败时退出码为 1
- 策略卡片内容：标题为策略名+日期+命中数，正文按 rank 排序，每行「序号、名称（加粗）、不复权收盘价、行业」

## 数据表

- 行情：`daily`（日线）、`adj_factor`（复权因子）、`daily_basic`（每日指标，含 `limit_status` 涨跌停状态）、`suspend_d`（停牌）、`stk_limit`（涨跌停价）、`index_daily`（指数日线，覆盖沪深300、上证指数、深证成指、创业板指、中证500、中证1000、科创50、北证50 共 8 个宽基指数，按指数逐只区间抓取）
- 参考：`stock_basic`（股票基础信息）、`trade_cal`（交易日历）、`namechange`（名称变更）
- 新增：`stock_company`（上市公司基本信息）、`new_share`（IPO 新股列表）、`stk_holdertrade`（股东增减持，按公告日逐自然日抓取）

### 升级/补数据

```bash
uv run quant data init-db                                  # 自动补新表/新列（幂等）
uv run quant data update -t stock_company
uv run quant data update -t new_share
uv run quant data update -t stk_holdertrade --from-date 2015-01-01   # 限流 90 次/分钟，2015 至今约 45-50 分钟；可先用较近区间试跑
uv run quant data update -t daily_basic --from-date 2020-01-01       # 补 limit_status 历史（该字段历史深度未逐一验证，建议先小范围试跑）
```

中断后断点续跑：直接重跑 `uv run quant data update -t stk_holdertrade`（不带 `--from-date`，程序从水位线继续）。

## 夜间全量重建

长时间增量更新后若出现数据空洞，或接口/字段变更需要彻底重刷，可在夜间（非交易时段）执行：

```bash
uv run python scripts/rebuild_data.py --yes              # 无值守执行，约 2 小时
uv run python scripts/rebuild_data.py                    # 列出待清空表并输入 YES 确认
uv run python scripts/rebuild_data.py --skip-truncate    # 中断后不清空，从水位线续跑补齐
```

- 默认清空 `schemas.TABLES` 全部系统表的数据（含 `ingest_log` 水位线）后全量重拉，行情/交易类数据从 2018-01-01 起（`--market-start` 可调）
- 日期参数、`--rate` 与 Tushare 客户端在清空之前校验/构造，参数写错不会清库
- 不传 `--yes` 时会列出将被清空的表并要求输入 `YES` 确认；`--all-tables` 会清空当前库全部基础表（含非本系统表），请谨慎使用
- `--skip-truncate` 不清空，按 `ingest_log` 水位线只补未入库的日期（没有水位线的表从起始日期拉取），快照表仍整体刷新，可安全重跑续传
- 脚本位于本地 `scripts/` 目录（未纳入版本管理），按需本地维护
- 预计耗时约 2 小时（行情约 1.2 万次调用、150 次/分钟；股东增减持按自然日抓取、90 次/分钟）
- 全量数据需预留数 GB MySQL 存储空间，请确认实例容量后再执行

## 策略参数

内置策略（默认只选沪深主板，非ST、非次新、非停牌）：

| 策略 | 一句话逻辑 | 排序 |
|---|---|---|
| `ma_volume` | 5 日均线上穿 20 日均线 + 放量 | 成交额 |
| `turtle_trade` | 20 日新高 + 成交额过亿 + 阳线真涨 | 流通市值 |
| `high_tight_flag` | 强动量后高位窄幅缩量整理 | 成交额 |
| `limit_up_shakeout` | 昨日涨停、今日放量收阴不破昨收 | 成交额 |
| `uptrend_limit_down` | 上升趋势中放量跌停（错杀） | 成交额 |
| `rps_breakout` | 120 日 RPS≥90 且接近 120 日高点 | RPS |
| `rise_shrink_pullback` | 近 10 日涨超 20% 后缩量阴线回调 | 成交额 |

- `configs/strategies/<策略名>.yaml`：策略参数（CLI 会自动读取同名文件）
- `uv run quant list` 展示的是代码默认参数；实际生效值以 `configs/strategies/<策略名>.yaml` 为准
- `configs/backtest/default.yaml`：回测默认参数（费用、调仓、持仓数、股票池过滤等）；`top_n` 留空 = 等权买入当日全部信号股，填数字（或 `-n`，`0` 表示全部）则限制持仓数量
- 新增/修改/暂时禁用/删除策略：见下方「策略变更标准操作流程」
- 因果约定（重要）：`generate_signals` 收到的是该股票整段已加载历史（含信号日之后的行情），策略必须只使用每行 `trade_date` 及之前的数据，禁止负向 `shift`、全序列归一化、反向窗口等引用未来行情的写法

## 策略变更标准操作流程

策略的派生数据只有一张表：`hit_<策略>`（`select`/`backtest`/报告均为运行时现算，无需重建）。以下操作全部幂等、可重跑。

### 新增策略

1. 新建 `src/quant/strategies/<名称>.py`：`@register_strategy("<名称>")` + `Params(BaseModel)` + `warmup_days` + `generate_signals(bars)`（需要横截面数据时实现 `prepare(market)`），可参考 `ma_volume.py`；策略文件会被自动扫描注册，无需改注册代码
2. 新建 `configs/strategies/<名称>.yaml`（`strategy` + `params`，字段与 `Params` 一致）
3. `src/quant/data/schemas.py`：把名称加入 `HIT_STRATEGIES`（`HIT_TABLES` 自动派生；顺序影响 `-s all` 与报告中的排列）
4. `tests/test_schemas.py` 的策略元组断言同步；新增 `tests/test_strategy_<名称>.py`；README 策略表加一行
5. 建表并回填历史：

   ```bash
   uv run quant data init-db                                  # 创建 hit_<名称>（幂等）
   uv run quant hits update -s <名称> --from-date 2024-01-01  # 全历史回填；只想从某天开始就改成该日期
   ```

6. 验证：`uv run quant list`、`uv run quant select -s <名称>`、`uv run quant data status`

### 修改策略

- 只改参数：编辑 `configs/strategies/<名称>.yaml`
- 改逻辑：编辑 `src/quant/strategies/<名称>.py`，先过 `uv run pytest tests/test_strategy_<名称>.py -q`
- 重算命中表（**必须显式传 `--from-date`**，否则只从水位线增量追加、旧口径数据残留）：

  ```bash
  uv run quant hits update -s <名称> --from-date 2024-01-01
  ```

- 重算会先删除区间旧行；想保留旧口径结果做对比，先备份：`CREATE TABLE hit_<名称>_bak AS SELECT * FROM hit_<名称>;`
- 重算后可复核：用交易日历重算历史列自洽性，并与 `uv run quant select -s <名称>` 当日结果逐代码比对；依赖命中表的报告/回测按需重跑
- 改了 `Params` 字段需同步 yaml；`warmup_days` 变化无需特殊处理（命中计算总是加载完整历史面板）

### 暂时禁用策略

与删除的区别：不删任何数据，保留配置与水位线，随时可恢复；命中表和历史命中继续留在库中。

1. 策略文件改名：`src/quant/strategies/<名称>.py` → `_<名称>.py`（下划线开头会被自动发现跳过，策略即从 `quant list`、`hits update -s all` 中消失）
2. 测试文件同步改名：`tests/test_strategy_<名称>.py` → `_test_strategy_<名称>.py`（pytest 不收集下划线开头的文件，避免因策略未注册而失败）
3. `src/quant/data/schemas.py` 的 `HIT_STRATEGIES` 移除该名；`tests/test_schemas.py` 元组断言同步——共振报告、通用回测、本地 `scripts/daily_update.py` 随即不再包含该策略
4. 以下内容都不要动：`configs/strategies/<名称>.yaml`、数据库表 `hit_<名称>`、`ingest_log` 中 `hit_<名称>` 的水位线
5. 验证：`uv run quant list` 不再出现、`uv run pytest -q` 通过；本地 `scripts/daily_update.py` 不会把该表误报为"未追平"
6. 恢复启用：文件改回原名、名字加回 `HIT_STRATEGIES`（测试同步），然后从旧水位线自动补齐禁用期间缺失的数据：

   ```bash
   uv run quant hits update -s <名称>     # 不带 --from-date：从旧水位线续算，自动补缺口
   ```

   若禁用期间还改过参数或逻辑，改为 `--from-date 2024-01-01` 全量重算。

### 删除策略

1. 删 `src/quant/strategies/<名称>.py`、`configs/strategies/<名称>.yaml`、`tests/test_strategy_<名称>.py`
2. `src/quant/data/schemas.py` 的 `HIT_STRATEGIES` 移除该名（否则下次 `data init-db` 会把表建回来）；`tests/test_schemas.py` 元组断言同步；README 策略表删行
3. 数据库清理：

   ```sql
   DROP TABLE `hit_<名称>`;
   DELETE FROM ingest_log WHERE task_name = 'hit_<名称>';
   ```

4. 验证：`uv run quant data status` 不再出现该表、`uv run quant list` 不再列出、`uv run pytest -q` 通过

> 表删除后数据不可恢复，想留档先备份：`CREATE TABLE hit_<名称>_bak AS SELECT * FROM hit_<名称>;`

## 回测约定（重要）

- 信号在调仓日收盘产生，次日开盘成交；涨停买不进、跌停卖不掉顺延、T+1、整手规则均已实现
- 默认等权买入当日全部信号股（每只目标金额 = 总权益 / 信号数量），可用 `configs/backtest/default.yaml` 的 `top_n` 或 `-n` 限制持仓数量
- 成交与估值使用后复权价，等效分红再投资；涨跌停/停牌判定使用原始价
- 后复权价 = 原始价 × Tushare `adj_factor`（以数据起点为基准、逐次含分红送转）；不同软件的后复权绝对值因基准与事件口径不同可能不同，跨平台比较请用涨跌幅/区间收益率
- 回测基准由 `configs/backtest/default.yaml` 的 `benchmark:` 配置，默认 `000300.SH`（沪深300）；`index_daily` 已入库的 8 个指数均可切换为基准

## 输出位置

- `outputs/select/<日期>_<策略>.csv`：`quant select` 选股结果
- `outputs/backtest/<策略>_<时间戳>/`：回测权益 `equity.csv`、成交 `trades.csv`、指标 `metrics.json` 与净值图 `equity.png`
- `outputs/reports/`：命中表现、共振拆分、通用命中回测等报告与明细（由本地 `scripts/` 下脚本生成）
- 本地 Parquet 缓存 `data_cache/`、日志 `logs/`；`outputs/`、`data_cache/`、`logs/`、`scripts/` 均已 gitignore

## 测试

```bash
uv run pytest                              # 单元测试
$env:RUN_INTEGRATION=1; uv run pytest -m integration   # 真实 MySQL/Tushare 集成测试
```

## 风险提示

本系统仅用于个人研究与学习，不构成任何投资建议。回测结果不代表未来收益。
