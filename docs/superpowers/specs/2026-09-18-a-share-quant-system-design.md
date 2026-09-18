# A 股个人量化选股与回测系统 — 设计文档

- 日期：2026-09-18
- 状态：已与用户逐节确认，待用户最终审阅
- 项目：`my-a-stock-quant`

## 1. 背景与目标

搭建个人量化选股/回测系统，用于：

1. 按自定义策略（如 MaVolume、HighTightFlag 等）筛选个股；
2. 基于历史日线数据对策略做可信回测。

已有资源与约束：

- 阿里云 MySQL RDS（公网）作为数据仓库，连接信息在 `.env`；
- Tushare 2000 积分年度会员，token 在 `.env`，可稳定获取日线级数据；
- 不涉及实盘/实时交易，**只需日线数据**；
- 个人使用，运行环境为本地 Windows，Python 3.12，uv 管理依赖。

## 2. 已确认的关键决策

| 决策点 | 选择 |
|---|---|
| 使用方式 | 命令行（typer）+ YAML 配置文件 |
| 策略定义 | Python 插件类（注册式）+ YAML 参数 |
| 回测引擎 | 自研轻量向量化引擎 |
| 数据读取 | MySQL 为唯一数据源；本地 Parquet 缓存加速迭代 |
| 默认股票池 | 全 A（含主板/创业板/科创板/北交所），默认剔除 ST、次新股、停牌 |
| 计算内核 | pandas |
| 回测执行模型 | 全市场 panel 向量化 + 逐调仓日推进 |
| 包结构 | 单一 Python 包 `quant`，src 布局 |

## 3. 第一期范围

**做：** 日线行情与基础指标的采集入库、增量更新、本地缓存；策略注册框架与两个内置策略；CLI 选股；A 股规则下的向量化回测、业绩指标与报告。

**不做（第一版非目标）：** 分钟级/实时数据、自动下单交易、财务三表与基本面因子、参数网格搜索/组合优化、Web UI、多用户支持。

## 4. 总体架构

分层，每层只依赖下一层：

```
Tushare API
    │  (拉取，限流+重试)
    ▼
[数据入库层] ──► 阿里云 MySQL（唯一数据源）
    │  (批量 SQL 增量同步)
    ▼
[本地 Parquet 缓存] ──► [数据装载层: 股票池过滤/复权/panel 对齐]
                              │
                              ▼
                        [策略层: 注册式插件]
                              │  信号
                    ┌─────────┴─────────┐
                    ▼                   ▼
              [选股输出]          [向量化回测引擎]
              CLI 表格/CSV        A股规则+费用+指标+报告
```

### 4.1 目录结构

```
my-a-stock-quant/
├── .env                      # 密钥（必须 gitignored）
├── .env.example              # 键名模板（可提交）
├── pyproject.toml
├── src/quant/
│   ├── config.py             # 读 .env + YAML，pydantic 校验
│   ├── cli.py                # typer 入口：data / select / backtest 子命令
│   ├── data/
│   │   ├── db.py             # MySQL 连接、批量 upsert
│   │   ├── tushare_client.py # Tushare 封装（限流、重试）
│   │   ├── schemas.py        # 全部表 DDL + DataFrame 字段定义
│   │   ├── ingest.py         # 全量/增量入库
│   │   └── cache.py          # Parquet 缓存读写与增量刷新
│   ├── strategies/
│   │   ├── base.py           # Strategy 基类 + 注册表
│   │   ├── ma_volume.py
│   │   ├── high_tight_flag.py
│   │   └── __init__.py       # 自动发现并注册所有策略模块
│   ├── engine/
│   │   ├── loader.py         # 读缓存→过滤股票池→后复权→构建 panel
│   │   ├── rules.py          # 涨跌停/停牌/T+1/整手判定
│   │   ├── backtest.py       # 逐调仓日推进的向量化回测
│   │   ├── metrics.py        # 年化/回撤/夏普/超额等指标
│   │   └── report.py         # 净值图、CSV、选股名单输出
│   └── utils/dates.py        # 交易日历工具
├── configs/
│   ├── strategies/*.yaml     # 每策略一份参数文件
│   └── backtest/default.yaml # 回测默认参数
├── tests/                    # pytest
├── outputs/                  # 回测/选股结果（gitignored）
├── data_cache/               # Parquet 缓存（gitignored）
└── logs/                     # 日志（gitignored）
```

### 4.2 解耦原则

- 策略只负责「给定一只股票的行情表，返回逐日布尔信号（可选打分）」；
- 股票池、复权、仓位、费用、调仓、T+1 等全部由引擎统一处理；
- 数据入库与计算解耦，回测/选股默认只读本地缓存。

## 5. 数据层

### 5.1 MySQL 表（9 张数据表 + 1 张水位线表）

均 InnoDB / utf8mb4，价格类字段用 DECIMAL 防精度丢失。每张含 `trade_date` 的表额外建 `trade_date` 普通索引。

| 表 | 关键字段 | 主键 | Tushare 接口 | 更新方式 |
|---|---|---|---|---|
| `stock_basic` | ts_code, symbol, name, area, industry, market, exchange, list_status, list_date, delist_date | ts_code | stock_basic | 全量 upsert（周更） |
| `trade_cal` | exchange, cal_date, is_open, pretrade_date | exchange+cal_date | trade_cal | 全量 upsert |
| `daily` | ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount | ts_code+trade_date | daily（按交易日） | 增量 |
| `adj_factor` | ts_code, trade_date, adj_factor | ts_code+trade_date | adj_factor（按交易日） | 增量 |
| `daily_basic` | ts_code, trade_date, turnover_rate, turnover_rate_f, volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share, float_share, free_share, total_mv, circ_mv | ts_code+trade_date | daily_basic（按交易日） | 增量 |
| `suspend_d` | ts_code, trade_date, suspend_timing, suspend_type | ts_code+trade_date | suspend_d（按交易日） | 增量 |
| `stk_limit` | ts_code, trade_date, up_limit, down_limit | ts_code+trade_date | stk_limit（按交易日） | 增量 |
| `index_daily` | ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount | ts_code+trade_date | index_daily（按指数代码） | 增量 |
| `namechange` | ts_code, name, start_date, end_date, ann_date, change_reason | ts_code+start_date | namechange | 全量 upsert |
| `ingest_log` | task_name, last_trade_date, updated_at | task_name | —— | 入库后推进 |

说明：

- `namechange` 用于回测中还原历史 ST 状态；
- `ingest_log` 是增量更新的水位线表，不是数据表；9 张数据表为其余各表。

### 5.2 Tushare 封装

- 按交易日循环拉取全市场（一次调用覆盖全市场约 5400 只，`daily` 单次 6000 行以内）；
- 令牌桶限流，默认 150 次/分钟（可配），适配 2000 积分档；
- 失败重试 3 次、指数退避；区分「无数据（非交易日/停牌）」与真实错误；
- 全量首刷估算：约 1200 个交易日 × 4 个日频接口 ≈ 4800 次调用，按 150 次/分钟约 35 分钟；
- `index_daily` 按指数代码拉取，默认基准指数为沪深 300（`000300.SH`），指数列表写在回测默认配置中。

### 5.3 增量入库流程（幂等、可续跑）

1. 读 `ingest_log` 取水位线，从 `trade_cal` 取其后所有开市日；
2. 按交易日拉取、校验（代码集合、字段完整性）；
3. 批量 `INSERT ... ON DUPLICATE KEY UPDATE`，每 20 个交易日一个事务；事务成功后才推进水位线；
4. 中断可续跑；支持 `--from-date/--to-date` 手工重灌指定区间。

### 5.4 复权约定

- 信号与收益计算使用**后复权价**：`hfq = 原始价 × adj_factor`（量纲不影响收益率，且不会出现前复权的负价格）；
- 涨跌停、停牌判定使用**原始价 + stk_limit + suspend_d**；
- loader 同时保留原始价与后复权价两组列。

### 5.5 Parquet 缓存

- `data_cache/<table>.parquet` 各一张全历史镜像（5 年日线约 300MB），旁挂 `meta.json` 记录水位线与行数；
- 跑选股/回测时只查一次 MySQL `ingest_log`：水位一致用缓存；落后则拉取缺失日期段、合并重写 parquet；
- 构建 panel 时用 pyarrow 谓词下推按日期段过滤，只读取所需列。

## 6. 策略层

### 6.1 接口

```python
class Strategy(ABC):
    name: str
    Params: type[BaseModel]        # pydantic；YAML 参数校验
    warmup_days: int = 0           # 指标预热所需的历史天数

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        """bars: 单只股票按 trade_date 升序的【后复权】OHLCV
        + daily_basic 字段（含 ST/次新标记）。
        返回等长 bool Series：True = 该日收盘后产生买入信号。"""

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        """可选：信号数超过持仓数时的排序分，默认 None（按 rank_by 排序）。"""
```

- `@register_strategy("ma_volume")` 装饰器注册；`strategies/__init__.py` 自动发现所有模块，新增策略 = 新增文件；
- loader 按 `warmup_days + 回测区间` 装载数据，预热期的信号不参与交易；
- 默认排序 `rank_by` 可选 `amount`/`vol_ratio`/`turnover_rate`/`circ_mv`，默认 `amount` 降序。

### 6.2 内置策略（阈值均为初始默认值，全部可在 YAML 调整）

**ma_volume**（均线 + 放量突破）：

- 参数：`ma_short=5`、`ma_long=20`、`vol_ma=20`、`vol_ratio=2.0`；
- 条件：收盘价 > 长均线；短均线上穿长均线（今日短>长且昨日短≤长）；当日成交量 > `vol_ratio` × 20 日均量；
- `warmup_days = max(ma_long, vol_ma) + 1`。

**high_tight_flag**（高位紧缩旗形）：

- 参数：`lookback=120`、`min_gain=0.3`、`tight_days=10`、`max_range=0.08`、`vol_shrink=0.6`、`near_high=0.05`；
- 条件：过去 `lookback` 日区间涨幅 ≥ `min_gain`；最近 `tight_days` 日振幅 (高-低)/低 ≤ `max_range`；最近 `tight_days` 日均量 ≤ `vol_shrink` × 之前 20 日均量；收盘价距 52 周高点 ≤ `near_high`；
- `warmup_days = max(250, lookback) + tight_days`。

## 7. 回测引擎

### 7.1 交易规则

| 项目 | 规则 |
|---|---|
| 信号→成交 | 调仓日收盘出信号，**次日开盘价成交**（可配 `next_open`/`next_close`） |
| 调仓频率 | daily / weekly（每周首个交易日）/ monthly（每月首个交易日） |
| 股票池 | 全 A；剔除当日 ST（由 namechange 还原历史名称）、上市不足 `min_list_days`（默认 60）个交易日、当日停牌 |
| 涨跌停 | 用 stk_limit 实际价格：买入日开盘价 ≥ 涨停价 → 放弃本次买入，资金留现金；卖出日开盘价 ≤ 跌停价 → 顺延到下一可卖日 |
| 停牌 | 买入日停牌 → 放弃本次买入；卖出日停牌 → 顺延 |
| T+1 | 买入后最早次日可卖；卖出旧仓所得资金当日可用于再买 |
| 整手 | 主板/创业板 100 股整数倍；科创板（688）最低 200 股、1 股递增 |
| 费用 | 佣金万 2.5 双边（最低 5 元，可配）；印花税卖出 0.05%；过户费 0.001% 双边；滑点默认 0.1%（买加卖减） |
| 空闲资金 | 不计息 |

### 7.2 组合推进

- 每调仓日：目标持仓 = 信号名单按排序取前 N（默认 10）等权；
- 已在目标名单中的持仓保留（不重复交易）；名单外的持仓卖出；卖出资金再买入新标的；
- 等权目标金额按调仓日**前收盘**估算的总权益计算；目标金额与成交价换算出的股数按整手规则向下取整；
- 现金不足时按排序依次买入直至不足一手；目标持仓不足 N 只时剩余资金留现金。

### 7.3 防未来函数

- 策略输入数据的最后一个交易日不晚于信号日；
- 回测按调仓日推进，执行价只取信号日之后的行情；
- 单元测试专项校验：策略接收窗口严格截止当日。

### 7.4 指标与输出

- 指标：累计收益、年化收益、最大回撤、夏普（无风险利率可配）、卡玛、胜率（按买卖配对）、换手率、月胜率、基准（沪深 300）同期收益、超额收益、超额最大回撤；
- 输出目录 `outputs/backtest/<策略>_<时间戳>/`：
  - `equity.csv`：每日净值 + 基准净值；
  - `trades.csv`：每笔买卖（日期、代码、方向、价格、数量、费用）；
  - `metrics.json`：全部指标；
  - `equity.png`：净值曲线（处理 matplotlib 中文字体）；
- 选股命令：控制台表格 + `outputs/select/<日期>_<策略>.csv`。

## 8. CLI 与配置

```bash
quant data init-db                 # 建表（幂等）
quant data update                  # 增量入库（默认续跑，可 --from-date/--to-date）
quant data status                  # 各表水位线、缓存状态
quant select -s ma_volume -d 2026-09-17 -n 20   # 选股；非交易日自动回退到最近交易日
quant backtest -s ma_volume --start 2021-01-01 --end 2026-09-17
quant list                         # 列出已注册策略及参数
```

配置优先级：CLI 参数 > YAML > 内置默认。

- `.env`：仅密钥与连接信息（`aliyun_rds_*`、`tushare_token`），pydantic-settings 启动即校验；
- `configs/strategies/*.yaml`：策略参数；
- `configs/backtest/default.yaml`：费用、滑点、调仓频率、执行时点、持仓数、`rank_by`、`min_list_days`、基准指数、初始资金（默认 100 万）、回测起止默认值。

## 9. 错误处理与日志

- stdlib logging：控制台 + `logs/` 轮转文件；
- Tushare / MySQL 调用统一重试 3 次 + 指数退避；
- 入库事务失败回滚且不推进水位线，下次运行自动续跑；
- 回测输入校验（区间无数据、策略参数非法、缓存缺表）给出明确中文报错。

## 10. 安全

- `.env` 加入 `.gitignore`（当前未忽略，必须先修）；
- 删除 `test_db.py`（硬编码数据库密码），连接逻辑迁入 `data/db.py`；
- 新增 `.env.example` 仅保留键名；
- gitignore 追加 `data_cache/`、`outputs/`、`logs/`。

## 11. 测试策略（pytest）

- 策略单测：合成行情断言信号逐日符合预期；
- 交易规则单测：涨停买不进、跌停卖不掉顺延、停牌、T+1、整手取整、费用计算；
- 指标单测：手算净值序列验证年化/回撤/夏普；
- 防未来函数测试：策略输入窗口截止信号日；
- 入库单测：mock Tushare/Mysql，验证增量水位线与幂等 upsert；
- 集成测试 `@pytest.mark.integration`（默认跳过）：真实连一次 MySQL/Tushare。

## 12. 里程碑

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| M1 骨架 | 项目结构、config、db.py、删 test_db.py、修 .gitignore、.env.example | `quant data status` 能连库并输出状态 |
| M2 数据 | 全部表 DDL + 全量/增量入库 + Parquet 缓存 | 能完整拉取并缓存一段真实数据，中断可续跑 |
| M3 策略+选股 | 注册框架 + ma_volume + select 命令 | `quant select` 输出真实选股名单 |
| M4 回测 | 引擎 + 规则 + 指标 + 报告 + ma_volume 端到端 | 跑通回测并生成 4 个输出文件 |
| M5 收尾 | high_tight_flag + 集成测试 + README | 两个策略均可用，测试通过 |

## 13. 主要依赖

pandas、pyarrow、pymysql、tushare、pydantic、pydantic-settings、PyYAML、typer、matplotlib、pytest。
