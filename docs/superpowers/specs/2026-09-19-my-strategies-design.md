# 我的策略（六策略）实现 — 设计文档

- 日期：2026-09-19
- 状态：已与用户逐节确认，待用户最终审阅
- 来源需求：`docs/my-strategies.md`
- 项目：`my-a-stock-quant`

## 1. 背景与目标

`docs/my-strategies.md` 定义了 6 个选股策略。现状：

- `ma_volume`、`high_tight_flag` 已实现，但与文档的参数/逻辑不一致；
- `TurtleTrade`、`LimitUpShakeout`、`UptrendLimitDown`、`RpsBreakout` 尚未实现；
- `RpsBreakout` 需要横截面（全市场）排名，现有 `Strategy` 接口只支持逐股计算；
- 文档要求"只选择沪深主板、非ST、非新上市"，现有 `select` 命令无选股范围入口。

目标：以文档为准完成 6 个策略（改写 2 个、新增 4 个），落地主板默认选股范围，并让引擎具备横截面排名能力。

## 2. 已确认的关键决策

| 决策点 | 选择 |
|---|---|
| 存量策略对齐 | 以 `docs/my-strategies.md` 为准直接改写 `ma_volume`、`high_tight_flag`；不保留旧逻辑/旧默认，历史回测结果会变化 |
| RPS 排名范围 | 全市场一起排名（当日有行情、非停牌、足窗口），再对结果应用主板/非ST/非新过滤 |
| 主板过滤落地 | 全局默认：回测 `default.yaml` 设 `allowed_boards: ["main"]`；`select` 加 `--boards` 选项默认 `main` |
| 流通市值口径 | 取 `daily_basic.circ_mv`（万元）；删除文档中的自算公式 |
| RPS 接入方式 | `Strategy.prepare(market)` 可选钩子，默认原样返回；`compute_signals` 逐股循环前调用一次 |
| 价格口径 | 指标/形态/动量用后复权价；涨停/跌停判定用不复权原始价；成交额用原始值 |
| 涨跌停判定 | 文档阈值法（默认 9.5%，可配置）；`stk_limit` 精确判定不在本次范围 |
| 命名 | 类名 `MaVolume / TurtleTrade / HighTightFlag / LimitUpShakeout / UptrendLimitDown / RpsBreakout`；注册名 `ma_volume / turtle_trade / high_tight_flag / limit_up_shakeout / uptrend_limit_down / rps_breakout` |

## 3. 范围

**做：** 引擎 `prepare` 钩子与 `boards` 过滤、6 个策略实现、策略 YAML 与回测默认配置、文档更新、单元测试与全量回归、真实数据选股与回测验收。

**不做：** 财务/基本面因子、行业动量、`stk_limit` 精确涨跌停判定、参数寻优、Web UI。

## 4. 引擎与接口改动

### 4.1 `Strategy.prepare(market) -> DataFrame`

- 基类新增，默认 `return market`（现有策略行为零变化）。
- 用途：横截面/全市场预计算。实现者如需追加列必须返回 `market.copy()`，不得修改入参；同一行只允许使用该行 `trade_date` 及更早的数据。
- `RpsBreakout` 覆盖此方法注入 `rps` 列。

### 4.2 `selection.compute_signals`

逐股循环前调用一次 `market = strategy.prepare(market)`。信号帧结构与排序逻辑不变，`backtest` 经 `compute_signals` 自动获得相同能力。

### 4.3 选股范围（boards）

- `quant select` 新增 `--boards`：默认 `main`；支持逗号组合（如 `main,gem`）；`all` 表示不限制。解析结果传入 `UniverseFilters(allowed_boards=...)`。
- `configs/backtest/default.yaml` 增加 `allowed_boards: ["main"]`；`backtest.filters_from_config` 把 YAML 的 list 转为 tuple。
- "非ST / 非新上市（60 个交易日）/ 非停牌"沿用现有默认过滤。

### 4.4 排序分（score）

| 策略 | score 来源 |
|---|---|
| TurtleTrade | `circ_mv` 降序（万元） |
| RpsBreakout | `rps` 降序 |
| 其余 4 个 | 引擎默认 `amount` 降序 |

## 5. 价格口径约定

| 用途 | 价格列 |
|---|---|
| 均线、动量、极值比、新高突破、RPS 涨幅、支撑位 | 后复权 `open/high/low/close` |
| 昨日涨停 / 今日跌停判定 | 不复权 `raw_close` |
| 成交额、成交量、换手率、市值 | 原始值（与复权无关） |

理由：涨跌停规则基于原始价计算，除权除息日后复权比率会失真；形态与支撑判断用后复权序列可避免除息跳空造成的假破位。

## 6. 策略规格

通用约定：输入 `bars` 按日期升序；只用每行当日及之前的数据；窗口不足或 NaN 处自然为 `False`；参数均可在 `configs/strategies/<name>.yaml` 覆盖。

### 6.1 MaVolume（`ma_volume`）

| 参数 | 默认 |
|---|---|
| `ma_short` | 5 |
| `ma_long` | 20 |
| `vol_ma` | 20 |
| `vol_ratio` | 1.5 |

- 金叉：昨日 `ma_short < ma_long` 且今日 `ma_short > ma_long`（严格小于）。
- 放量：今日 `vol > vol_ratio × 20 日均量`（均量含当日）。
- 信号 = 金叉 且 放量。删除旧实现的 `close > ma_long` 附加条件。
- 价格：后复权；`warmup_days = max(ma_long, vol_ma) + 1 = 21`。

### 6.2 TurtleTrade（`turtle_trade`）

| 参数 | 默认 |
|---|---|
| `high_days` | 20 |
| `min_amount` | 100,000,000（元） |

- 突破：今日 `close > 前 high_days 日 high 最大值`（窗口不含当日）。
- 流动性：`amount × 1000 > min_amount`（`amount` 存的是千元）。
- 防诱多：`close > open` 且 `close > 昨日 close`。
- score：`circ_mv` 降序。
- 价格：后复权（`amount` 原始）；`warmup_days = 21`。

### 6.3 HighTightFlag（`high_tight_flag`）

| 参数 | 默认 |
|---|---|
| `momentum_days` | 40 |
| `momentum_ratio` | 1.6 |
| `tight_days` | 10 |
| `tight_ratio` | 1.15 |
| `support_ratio` | 0.8 |
| `vol_base_days` | 20 |
| `vol_shrink` | 0.6 |

- 强动量：近 `momentum_days` 日 `high 最大值 / low 最小值 > momentum_ratio`。
- 收敛：近 `tight_days` 日 `high 最大值 / low 最小值 < tight_ratio`。
- 高位抗跌：近 `tight_days` 日 `low 最小值 ≥ support_ratio × 近 momentum_days 日 high 最大值`。
- 缩量：今日 `vol < vol_shrink × 前 vol_base_days 日均量`（均量不含当日）。
- 价格：后复权；`warmup_days = 40`。

### 6.4 LimitUpShakeout（`limit_up_shakeout`）

| 参数 | 默认 |
|---|---|
| `limit_pct` | 0.095 |
| `vol_ratio` | 2.0 |

信号日 `t`（需 3 根 K 线）：

- 昨日涨停：`raw_close[t-1] ≥ raw_close[t-2] × (1 + limit_pct)`。
- 今日收阴：`close[t] < open[t]`（后复权，同日等价）。
- 今日放量：`vol[t] > vol_ratio × vol[t-1]`。
- 支撑不破：`low[t] ≥ close[t-1]`（后复权）。
- `warmup_days = 3`。

### 6.5 UptrendLimitDown（`uptrend_limit_down`）

| 参数 | 默认 |
|---|---|
| `ma_short` | 20 |
| `ma_long` | 60 |
| `limit_pct` | 0.095 |
| `vol_ma` | 20 |
| `vol_ratio` | 2.0 |

- 上升趋势：昨日 `ma_short > ma_long`（后复权收盘均线）。
- 跌停：今日 `raw_close ≤ 昨日 raw_close × (1 - limit_pct)`。
- 放量：今日 `vol > vol_ratio × vol_ma 日均量`（均量含当日）。
- `warmup_days = 61`（昨日需满 60 根）。

### 6.6 RpsBreakout（`rps_breakout`）

| 参数 | 默认 |
|---|---|
| `window` | 120 |
| `rps_threshold` | 90 |
| `high_ratio` | 0.90 |
| `high_min_bars` | 60 |

- `prepare`：对全市场按股票计算 `ret = close / close[window 日前] - 1`（后复权）；按 `trade_date` 横截面排名 `rps = rank(pct=True) × 100`；停牌的股票当日不参与排名；无 `ret`（窗口不足）不参与排名。
- 信号：`rps ≥ rps_threshold` 且 `close ≥ window 日滚动最高 × high_ratio`（高点窗口至少 `high_min_bars` 根）。
- score：`rps` 降序。
- 价格：后复权；`warmup_days = 121`。

## 7. 边界与冲突处理

- 全 NaN / 窗口不足：信号为 `False`，不报错。
- `prepare` 返回值只影响本次信号计算，不写回缓存。
- `select` 单日模式：`rps` 以该日为横截面，与文档"最新数据日排名"一致。
- 回测模式：`prepare` 对整个面板执行一次，逐日横截面天然因果，无未来函数。
- 阈值法涨停：9.5% 会把涨幅 9.5%~10% 未涨停的阳线也判为"涨停"，按文档接受；`stk_limit` 精确判定留作后续增强。
- `--boards all`：等价于不限制板块，其余过滤仍生效。

## 8. 配置与文档变更

- `configs/strategies/`：更新 `ma_volume.yaml`、`high_tight_flag.yaml`；新增 `turtle_trade.yaml`、`limit_up_shakeout.yaml`、`uptrend_limit_down.yaml`、`rps_breakout.yaml`（默认值与第 6 节一致）。
- `configs/backtest/default.yaml`：增加 `allowed_boards: ["main"]`。
- `docs/my-strategies.md`：删除 TurtleTrade 的流通市值自算公式，改为"取 daily_basic.circ_mv（万元）"；其余保持原文。
- `README.md`：策略清单更新为 6 个；注明默认只选主板。
- 所有文档与代码中不出现"（改）/（新）"等标注。

## 9. 测试计划

- 每策略单元测试（合成 K 线）：命中、不命中、临界值（严格 `<` vs `≤`）、NaN/窗口不足、warmup 值。
- 防未来函数：将输入序列截断到信号日重算，各信号位置结果必须与全序列一致；`prepare` 策略按日期截断面板后比较。
- RPS 专项：3 只以上股票横截面排名正确、停牌不参与、窗口不足不计。
- 引擎：`prepare` 默认不改行为；`compute_signals` 使用增强后的列；`filters_from_config` list→tuple。
- CLI：`select --boards` 默认只出主板、`--boards all` 不限制。
- 存在测试更新：`tests/test_strategy_ma_volume.py`（新默认与条件）、`tests/test_strategy_high_tight_flag.py`（按新语义重写）。
- 全量 `uv run pytest -q` 通过，无新增依赖。

## 10. 真实验收

1. 对 6 个策略分别执行 `uv run quant select -s <name> -n 20`（最新交易日；若无信号换最近 2~3 个交易日复核并如实报告）。
2. 对 6 个策略分别执行 `uv run quant backtest -s <name> --start 2021-01-01 --end 2026-09-18`，汇总成交笔数与核心指标。
3. 抽查选股结果：代码均为沪深主板（60/00 开头）、非 ST、非次新。

## 11. 风险与取舍

- 改写存量策略会改变既有回测结果（用户已确认以文档为准）。
- 默认全局限制主板会改变既有 `select`/`backtest` 默认行为（用户已确认）。
- `prepare` 会复制一份面板数据（约百万行，内存可接受）。
- 阈值法涨跌停存在 9.5%~10% 的判定模糊，按文档接受。
