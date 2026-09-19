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
uv run quant select -s ma_volume                 # 选股（默认最新交易日、只选主板、输出全部命中）
uv run quant select -s ma_volume -n 50           # 只输出前 50 只
uv run quant select -s ma_volume --boards all    # 不限板块（main,gem,star,bse 可逗号组合）
uv run quant backtest -s ma_volume --start 2021-01-01 --end 2026-09-17   # 默认等权买入全部信号股
uv run quant hits update -s all                  # 回填/增量写入 hit_<策略> 历史命中表
```

## 策略历史命中（hit_&lt;策略&gt;）

- `uv run quant hits update -s all` 把每个策略历史上每日全部命中的股票写入对应 `hit_<策略名>` 表；默认从 2024-01-01 回填，水位线存 `ingest_log`，可增量续跑、幂等重跑
- 显式传 `--from-date` 视为重算：先清空该区间旧命中再重新计算（策略口径调整后用它刷新历史）
- 口径与 `select` 一致：沪深主板、非ST、上市≥60日、非停牌；`rank` 按 score 降序编号，`score` 为该策略排序分（成交额 / 流通市值 / RPS），`params` 保存当次运行的策略参数快照
- 字段：`industry`（行业快照）、`prev_hit`（同策略上一交易日是否命中）、`hit_3d/hit_5d/hit_10d`（同策略前 3/5/10 个交易日命中天数，不含当日、停牌日占窗口）、`streak`（同策略连续命中天数，含当日）
- 历史列只统计**表内已记录的命中**（即 2024-01-02 起），每行都可用表自身复核；命中计算加载完整历史面板，结果与水位线、重跑次数无关（单日 `select` 对暖机期内长期停牌股可能略有差异）
- 命中表是派生数据：不参与 Tushare 抓取、本地 Parquet 缓存与夜间全量重建清空；`quant data status` 可查看行数与水位线

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

- `configs/strategies/<策略名>.yaml`：策略参数（CLI 会自动读取同名文件）
- `configs/backtest/default.yaml`：回测默认参数（费用、调仓、持仓数、股票池过滤等）；`top_n` 留空 = 等权买入当日全部信号股，填数字（或 `-n`，`0` 表示全部）则限制持仓数量
- 新增策略：在 `src/quant/strategies/` 新建文件，用 `@register_strategy("名称")` 装饰类并实现 `generate_signals`
- 因果约定（重要）：`generate_signals` 收到的是该股票整段已加载历史（含信号日之后的行情），策略必须只使用每行 `trade_date` 及之前的数据，禁止负向 `shift`、全序列归一化、反向窗口等引用未来行情的写法

## 回测约定（重要）

- 信号在调仓日收盘产生，次日开盘成交；涨停买不进、跌停卖不掉顺延、T+1、整手规则均已实现
- 默认等权买入当日全部信号股（每只目标金额 = 总权益 / 信号数量），可用 `configs/backtest/default.yaml` 的 `top_n` 或 `-n` 限制持仓数量
- 成交与估值使用后复权价，等效分红再投资；涨跌停/停牌判定使用原始价
- 回测基准由 `configs/backtest/default.yaml` 的 `benchmark:` 配置，默认 `000300.SH`（沪深300）；`index_daily` 已入库的 8 个指数均可切换为基准
- 输出：`outputs/backtest/<策略>_<时间戳>/`（equity.csv / trades.csv / metrics.json / equity.png）

## 测试

```bash
uv run pytest                              # 单元测试
$env:RUN_INTEGRATION=1; uv run pytest -m integration   # 真实 MySQL/Tushare 集成测试
```

## 风险提示

本系统仅用于个人研究与学习，不构成任何投资建议。回测结果不代表未来收益。
