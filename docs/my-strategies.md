# 我的策略

本文档记录我的选股策略、全部参数的含义与调参影响。

## 通用约定

- **选股范围**：沪深主板（`60`/`00` 开头）、非 ST、上市 ≥60 个交易日、非停牌；`select`/`hits`/`backtest` 默认同口径（`--boards main`）。
- **价格口径**：形态、均线、动量等一律使用**后复权价**（`open/high/low/close`，等效分红再投资）；**涨停/跌停判定使用不复权真实价** `raw_close`（行情界面价），因为涨跌停是按真实价格触发的。
- **单位**：`amount` 为千元（Tushare 原始单位，策略内乘 1000 换算为元）；`circ_mv` 为万元；`vol` 为手。
- **参数配置**：代码内为默认值，实际生效值取 `configs/strategies/<策略名>.yaml`；`params` 含义见各策略的参数表。
- **修改参数后必须重算历史命中表**：`uv run quant hits update -s <策略名> --from-date 2024-01-01`（详细流程见 README「策略变更标准操作流程」）。`quant select` 是实时计算，改完 yaml 立即生效、无需重算。
- **暖机（warmup_days）**：引擎为每只股票在信号日之前额外加载的交易日数；信号日 T 实际可用的 K 线数为 `暖机 + 1`（含 T 当日）。行数不足时窗口值为 NaN、该日不产生信号。
- **排序分与 rank**：每日命中股票按 `score` 降序编号为 `rank`。`score` 由策略的排序口径决定（多数为成交额），仅影响输出顺序与限量（`-n`），不影响是否命中。

## 速查表

| # | 策略 | 一句话逻辑 | 暖机 | 排序（score） | 关键参数（代码默认值） |
|---|---|---|---|---|---|
| 1 | `ma_volume` | 短均线上穿长均线 + 放量 | 21 | 成交额 | `ma_short=5`, `ma_long=20`, `vol_ma=20`, `vol_ratio=1.5` |
| 2 | `turtle_trade` | 20 日新高 + 成交额过亿 + 阳线真涨 | 21 | 流通市值 | `high_days=20`, `min_amount=1e8` |
| 3 | `high_tight_flag` | 强动量后高位窄幅缩量整理 | 40 | 成交额 | `momentum_days=40`, `momentum_ratio=1.6`, `tight_days=10`, `tight_ratio=1.15`, `support_ratio=0.8`, `vol_base_days=20`, `vol_shrink=0.6` |
| 4 | `limit_up_shakeout` | 昨日涨停、今日放量收阴不破昨收 | 3 | 成交额 | `limit_pct=0.095`, `vol_ratio=2.0` |
| 5 | `uptrend_limit_down` | 上升趋势中放量跌停（错杀） | 61 | 成交额 | `ma_short=20`, `ma_long=60`, `limit_pct=0.095`, `vol_ma=20`, `vol_ratio=2.0` |
| 6 | `rps_breakout` | 120 日 RPS≥90 且接近 120 日高点 | 121 | RPS 值 | `window=120`, `rps_threshold=90`, `high_ratio=0.9`, `high_min_bars=60` |
| 7 | `rise_shrink_pullback` | 近 M 日涨超 X% 后缩量阴线回调 | 11 | 成交额 | `rise_days=10`, `rise_pct=25.0`, `pullback_days=3`, `min_bearish=2`, `vol_shrink=0.80`, `bearish_mode=close_below_open` |

> 注：`ma_volume` 当前 yaml 已将 `vol_ma` 调为 `10`、`vol_ratio` 调为 `1.8`（覆盖代码默认值），其余策略 yaml 与默认值一致。

---

## 1. MaVolume — 均线金叉放量

**定位**：经典趋势启动信号，捕捉短均线上穿长均线、且当日明显放量的股票。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `ma_short` | int | 5 | 短均线窗口（收盘价后复权） | 越小越灵敏、信号越多 |
| `ma_long` | int | 20 | 长均线窗口（收盘价后复权） | 决定趋势级别 |
| `vol_ma` | int | 20 | 均量窗口（成交量） | **窗口含当日**；当前 yaml 已调为 10 |
| `vol_ratio` | float | 1.5 | 放量倍数 | 今日量须大于 `vol_ratio × 均量`；当前 yaml 已调为 1.8 |

### 选股条件（T 日为信号日）

1. **金叉**：`ma_short(T) > ma_long(T)` 且 `ma_short(T-1) < ma_long(T-1)`。数学相等（含浮点 1ulp 级噪声，容差 `1e-9×|ma_long|`）**不算**金叉，避免同一信号随加载面板长短闪烁。
2. **放量**：`vol(T) > vol_ratio × mean(vol[T-vol_ma+1 .. T])`（均量窗口**包含当日**）。

### 说明

- 暖机 = `max(ma_long, vol_ma) + 1`（默认 21）：金叉需前一日均线有效。
- 排序：成交额。

### 调参影响

- 调大 `vol_ratio`：要求更极端的放量，信号更少更严（当前 1.8 比默认 1.5 更严）。
- 调小 `vol_ma`：均量更敏感、更容易被单日异动放大（配合 `vol_ratio` 一起调）。
- `ma_short/ma_long` 组合变化会改变金叉频率，暖机随之变化。

---

## 2. TurtleTrade — 海龟突破

**定位**：经典海龟突破的 A 股改良版，突破新高同时要求流动性充足，并用阳线过滤高开低走诱多。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `high_days` | int | 20 | 新高回看窗口 | 与前 `high_days` 个交易日（**不含当日**）的最高价比较 |
| `min_amount` | float | 100000000（1 亿） | 流动性门槛（元） | 比较对象是 `amount × 1000`（Tushare 千元换算为元），条件为**严格大于** |

### 选股条件

1. **突破新高**：`close(T) > max(high[T-high_days .. T-1])`（窗口不含当日，严格大于）。
2. **流动性**：`amount(T) × 1000 > min_amount`。
3. **阳线真涨**：`close(T) > open(T)` 且 `close(T) > close(T-1)`（实体阳线且相较昨收上涨）。

### 说明

- 暖机 = `high_days + 1` = 21。
- 排序：流通市值（`circ_mv`，万元，从大到小）；市值缺失的股票 `score` 为空、排在最后。

### 调参影响

- `min_amount` 单位是元，想放松到 5000 万填 `50000000`。
- `high_days` 越大突破越难、信号越少（60 即经典 60 日突破）。

---

## 3. HighTightFlag — 高而窄的旗形整理

**定位**：欧奈尔"高而窄的旗形"变体，寻找前期强动量拉升、随后高位极度收敛缩量的整理形态。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `momentum_days` | int | 40 | 强动量回看窗口 | `max(high)/min(low)`，**窗口含当日** |
| `momentum_ratio` | float | 1.6 | 强动量阈值 | 区间最高/最低 > 该值（1.6 ≈ 区间涨幅 >60%），严格大于 |
| `tight_days` | int | 10 | 收敛回看窗口 | 近 `tight_days` 日振幅 |
| `tight_ratio` | float | 1.15 | 收敛阈值 | 近端 `max(high)/min(low) < 1.15`（振幅 <15%），严格小于 |
| `support_ratio` | float | 0.8 | 高位支撑比例 | 近端最低 ≥ `support_ratio × 远期最高`，整理不破坏趋势 |
| `vol_base_days` | int | 20 | 均量基准窗口 | 缩量比较的均量窗口，**不含当日**（`vol.shift(1).rolling(...)`） |
| `vol_shrink` | float | 0.6 | 缩量倍数 | 今日量 < `vol_shrink × 前 vol_base_days 日均量`，严格小于 |

### 选股条件

1. `max(high[T-39..T]) / min(low[T-39..T]) > momentum_ratio`
2. `max(high[T-9..T]) / min(low[T-9..T]) < tight_ratio`
3. `min(low[T-9..T]) >= support_ratio × max(high[T-39..T])`
4. `vol(T) < vol_shrink × mean(vol[T-20..T-1])`

### 说明

- 暖机 = `max(momentum_days, vol_base_days + 1)` = 40。
- 排序：成交额。

### 调参影响

- `momentum_ratio` 调大 → 要求前期拉升更凶；`tight_ratio` 调小 → 要求整理更窄；`vol_shrink` 调小 → 要求缩量更极致。
- 三者同时收紧会大幅减少信号（该策略本身命中数很少，属精选型）。

---

## 4. LimitUpShakeout — 涨停洗盘回踩

**定位**：昨日涨停后今日放量收阴但支撑不破，捕捉主力洗盘后的回踩确认形态。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `limit_pct` | float | 0.095 | 涨停判定幅度 | 昨日涨幅 ≥ `limit_pct` 视为涨停；主板 10% 制度留 0.5% 容差 |
| `vol_ratio` | float | 2.0 | 放量倍数 | 今日量 > `vol_ratio × 昨日量`（与**前一交易日单日量**比较，非均量） |

### 选股条件（T 为信号日，取最近 3 根 K 线）

1. **昨日涨停**：`raw_close(T-1) >= raw_close(T-2) × (1 + limit_pct)`（**不复权真实价**判定）。
2. **今日收阴**：`close(T) < open(T)`（后复权价）。
3. **今日放量**：`vol(T) > vol_ratio × vol(T-1)`。
4. **支撑不破**：`low(T) >= close(T-1)`（今日最低不低于昨日收盘，后复权价）。

### 说明

- 暖机 = 3。
- 排序：成交额。
- 涨停用真实价、收阴与支撑用后复权价：除权日不会把真实价格跳变误判为涨跌停。

### 调参影响

- `vol_ratio` 调大 → 要求洗盘放量更显著。
- `limit_pct` 一般不用改；若扩展到 20cm 板块（创业板/科创板）需相应调大（默认范围只有主板）。

---

## 5. UptrendLimitDown — 上升趋势跌停反包

**定位**：上升趋势中的"错杀"机会，捕捉均线多头排列下突发放量跌停的股票。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `ma_short` | int | 20 | 短均线窗口（后复权收盘） | 与 `ma_long` 构成多头排列 |
| `ma_long` | int | 60 | 长均线窗口 | 暖机随其变化 |
| `limit_pct` | float | 0.095 | 跌停判定幅度 | 今日跌幅 ≥ `limit_pct` 视为跌停 |
| `vol_ma` | int | 20 | 均量窗口 | **窗口含当日** |
| `vol_ratio` | float | 2.0 | 放量倍数 | 今日量 > `vol_ratio × 均量` |

### 选股条件

1. **上升趋势**：昨日 `ma_short(T-1) > ma_long(T-1)`（严格多头排列，含 `1e-9` 相对容差防浮点闪烁；数学相等不算）。
2. **放量跌停**：`raw_close(T) <= raw_close(T-1) × (1 - limit_pct)`（**不复权真实价**）。
3. **量能确认**：`vol(T) > vol_ratio × mean(vol[T-vol_ma+1 .. T])`（均量含当日）。

### 说明

- 暖机 = `ma_long + 1` = 61。
- 排序：成交额。
- 均线或均量为 NaN（K 线不足）时该日不产生信号。

### 调参影响

- `vol_ratio` 调大 → 只保留恐慌盘更重的错杀；`ma_long` 调大 → 趋势判定更长期。
- 该策略命中数少而波动大，调参后建议先用 `quant hits update -s uptrend_limit_down --from-date 2024-01-01` 重算再评估。

---

## 6. RpsBreakout — RPS 相对强度突破

**定位**：欧奈尔 RPS 相对强度体系的突破策略，选出全市场动量排名前 10% 且接近 120 日高点的股票。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `window` | int | 120 | 涨幅与高点回看窗口（交易日） | 按**交易日历**定位窗口（不是按个股行数），停牌不改变基准日的位置 |
| `rps_threshold` | float | 90 | RPS 阈值 | 当日收益率在全市场的百分位 ≥ 90（即前 10%），严格 `>=` |
| `high_ratio` | float | 0.9 | 接近高点比例 | `close >= 120 日滚动最高价 × high_ratio` |
| `high_min_bars` | int | 60 | 高点窗口最少有效根数 | 不足 60 根时高点为 NaN、不命中 |

### 计算与选股条件

1. **相对强度（横截面，`prepare` 预计算）**：
   - 对每个交易日、每只股票：`ret = close(T) / close(T 的 window 个交易日前) - 1`；
   - 基准日为交易日历上往前 `window` 个交易日（停牌导致该日无成交价时 `ret` 为 NaN，当日不参与排名）；
   - 当日全体非停牌且 `ret` 有效的股票按 `ret` 排名，`RPS = 百分位 × 100`；
   - 命中要求 `RPS >= rps_threshold`。
2. **接近突破**：`close(T) >= 滚动 window 日最高价(T) × high_ratio`，滚动窗口不足 `high_min_bars` 根时不计。

### 说明

- 暖机 = `window + 1` = 121。
- 排序：RPS 值（越高越靠前）。
- `prepare` 是横截面钩子：在逐股循环前对全市场面板计算一次 `ret/rps/high_max`，保证排名是"全市场"口径。

### 调参影响

- `rps_threshold` 调低（如 80）→ 入选面扩大到前 20%，信号显著增多。
- `high_ratio` 调低 → 允许离高点更远（突破确认更松）。
- `high_min_bars` 调大 → 上市初期股票被更严格地排除。

---

## 7. RiseShrinkPullback — 上涨后缩量回调

**定位**：捕捉强势上涨后的缩量回调（洗盘），要求前期涨幅足够大、近端出现阴线但量能明显萎缩。

### 参数

| 参数 | 类型 | 默认 | 含义 | 说明 |
|---|---|---|---|---|
| `rise_days` (M) | int | 10 | 涨幅回看窗口（交易日） | 必须大于 `pullback_days`，否则参数校验直接报错 |
| `rise_pct` (X) | float | 25.0 | 涨幅阈值（百分数） | `close(T)/close(T-M)-1 > X%`，**严格大于** |
| `pullback_days` (P) | int | 3 | 阴线统计与近端均量窗口（交易日） | |
| `min_bearish` (L) | int | 2 | 近 P 日中至少几根阴线 | `≥`，恰好 L 根也命中 |
| `vol_shrink` | float | 0.80 | 缩量比例 | 近 P 日均量 < `vol_shrink ×` 近 M 日均量，**严格小于** |
| `bearish_mode` | str | `close_below_open` | 阴线判定模式 | `close_below_open` = 收盘<开盘（实体阴线）；`close_below_prev_close` = 收盘<昨收（下跌日） |

### 选股条件（T 日，窗口均含当日，后复权价）

1. **涨幅**：`close(T)/close(T-M) - 1 > rise_pct/100`
2. **阴线数**：近 P 根 K 线中阴线数 ≥ `min_bearish`
3. **缩量**：`mean(vol[T-P+1 .. T]) < vol_shrink × mean(vol[T-M+1 .. T])`（M > P）

### 说明

- 暖机 = `rise_days + 1` = 11。
- 排序：成交额。
- 两种阴线模式的区别：某日收出小阳线（`close > open`）但低于昨收时，`close_below_open` 不算阴线，`close_below_prev_close` 算。
- 命中表只记录 2026-01-01 起的样本（`prev_hit/hit_3d/hit_5d/hit_10d/streak` 等历史列也只反映该区间内的命中）。

### 调参影响

- `rise_pct` 调大 → 要求前期涨幅更陡；`vol_shrink` 调小 → 要求缩量更极致。
- `min_bearish` 调大（如 = P）→ 要求近 P 日全部收阴。
- 改完 yaml 后需重算命中表（`uv run quant hits update -s rise_shrink_pullback --from-date 2026-01-01`）。

---

## 附：参数与命中表

- 命中表 `hit_<策略>` 的 `score` 列即上述排序分，`rank` 为按 `score` 降序的当日序号；`params` 列保存该次运行时的策略参数 JSON 快照，可直接用表内数据复核参数。
- 回测与两日/共振表现报告读取的也是这套 `score/rank` 与信号口径；修改参数或逻辑后，历史表现结论需要重算命中表后重新生成（见 README「策略变更标准操作流程」）。
