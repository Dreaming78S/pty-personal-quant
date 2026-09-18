# Tushare 数据接口文档整理

> 本文档整理自 Tushare Pro 官网（https://tushare.pro/document/2）数据接口页面，涵盖**基础数据、行情数据、财务数据**三大类共 17 个常用接口。
>
> 统一调用方式：
>
> ```python
> import tushare as ts
> pro = ts.pro_api('your token')   # 或 pro = ts.pro_api() 已配置 token 时
> df = pro.接口名(**参数)
> # 等价写法
> df = pro.query('接口名', **参数)
> ```
>
> 日期格式统一为 `YYYYMMDD`（如 `20181010`）。

---

## 目录

- [一、基础数据](#一基础数据)
  - [1. 股票基础信息（stock_basic）](#1-股票基础信息stock_basic)
  - [2. 交易日历（trade_cal）](#2-交易日历trade_cal)
  - [3. ST 股票列表（stock_st）](#3-st-股票列表stock_st)
  - [4. 股票曾用名（namechange）](#4-股票曾用名namechange)
  - [5. 上市公司基本信息（stock_company）](#5-上市公司基本信息stock_company)
  - [6. 管理层薪酬和持股（stk_rewards）](#6-管理层薪酬和持股stk_rewards)
  - [7. IPO 新股列表（new_share）](#7-ipo-新股列表new_share)
- [二、行情数据](#二行情数据)
  - [8. A 股日线行情（daily）](#8-a-股日线行情daily)
  - [9. A 股复权行情（pro_bar）](#9-a-股复权行情pro_bar)
  - [10. 复权因子（adj_factor）](#10-复权因子adj_factor)
  - [11. 沪深股通十大成交股（hsgt_top10）](#11-沪深股通十大成交股hsgt_top10)
  - [12. 每日指标（daily_basic）](#12-每日指标daily_basic)
- [三、财务数据](#三财务数据)
  - [13. 股东人数（stk_holdernumber）](#13-股东人数stk_holdernumber)
  - [14. 利润表（income）](#14-利润表income)
  - [15. 资产负债表（balancesheet）](#15-资产负债表balancesheet)
  - [16. 现金流量表（cashflow）](#16-现金流量表cashflow)
  - [17. 股东增减持（stk_holdertrade）](#17-股东增减持stk_holdertrade)

---

## 一、基础数据

### 1. 股票基础信息（stock_basic）

- **接口**：`stock_basic`
- **描述**：获取基础信息数据，包括股票代码、名称、上市日期、退市日期等。
- **限量**：每次最多返回 6000 行数据（覆盖全市场 A 股，会随股票总数增长而增加）。
- **权限**：2000 积分起，每分钟请求 50 次。此接口为基础信息，调取一次即可拉完，建议保存到本地存储后使用。
- **官方页面**：https://tushare.pro/document/2?doc_id=25

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | TS 股票代码（格式说明见官网） |
| name | str | N | 名称 |
| market | str | N | 市场类别（主板/创业板/科创板/CDR/北交所） |
| list_status | str | N | 上市状态 L 上市 D 退市 P 暂停上市 G 未交易 UN 未上市，默认 L |
| exchange | str | N | 交易所 SSE 上交所 SZSE 深交所 BSE 北交所 |
| is_hs | str | N | 是否沪深港通标的，N 否 H 沪股通 S 深股通 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 代码 |
| symbol | str | Y | 股票代码 |
| name | str | Y | 股票名称 |
| area | str | Y | 地域 |
| industry | str | Y | 所属行业 |
| fullname | str | N | 股票全称 |
| enname | str | N | 英文全称 |
| cnspell | str | Y | 拼音缩写 |
| market | str | Y | 市场类型（主板/创业板/科创板/CDR） |
| exchange | str | N | 交易所代码 |
| curr_type | str | N | 交易货币 |
| list_status | str | N | 上市状态 L 上市 D 退市 G 过会未交易 P 暂停上市 UN 未上市 |
| list_date | str | Y | 上市日期 |
| delist_date | str | N | 退市日期 |
| is_hs | str | N | 是否沪深港通标的，N 否 H 沪股通 S 深股通 |
| act_name | str | Y | 实控人名称 |
| act_ent_type | str | Y | 实控人企业性质 |

> 说明：旧版上的 PE/PB/股本等字段，请在行情接口"每日指标"中获取。

**接口示例**

```python
pro = ts.pro_api()

# 查询当前所有正常上市交易的股票列表
data = pro.stock_basic(exchange='', list_status='L',
                       fields='ts_code,symbol,name,area,industry,list_date')
```

---

### 2. 交易日历（trade_cal）

- **接口**：`trade_cal`
- **描述**：获取各大交易所交易日历数据，默认提取上交所。三大交易所的交易日历一致，北交所交易日历参考上交所和深交所。
- **积分**：需 2000 积分。
- **官方页面**：https://tushare.pro/document/2?doc_id=26

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| exchange | str | N | 交易所 SSE 上交所, SZSE 深交所, CFFEX 中金所, SHFE 上期所, CZCE 郑商所, DCE 大商所, INE 上能源 |
| start_date | str | N | 开始日期（YYYYMMDD） |
| end_date | str | N | 结束日期 |
| is_open | str | N | 是否交易 '0' 休市 '1' 交易 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| exchange | str | Y | 交易所 SSE 上交所 SZSE 深交所 |
| cal_date | str | Y | 日历日期 |
| is_open | str | Y | 是否交易 0 休市 1 交易 |
| pretrade_date | str | Y | 上一个交易日 |

**接口示例**

```python
pro = ts.pro_api()
df = pro.trade_cal(exchange='', start_date='20180101', end_date='20181231')
# 等价：df = pro.query('trade_cal', start_date='20180101', end_date='20181231')
```

---

### 3. ST 股票列表（stock_st）

- **接口**：`stock_st`
- **描述**：获取 ST 股票列表，可根据交易日期获取历史上每天的 ST 列表。
- **权限**：3000 积分起。
- **提示**：每天上午 9:20 更新；单次请求最大返回 1000 行数据，可循环提取；本接口数据从 20000101 开始，太早历史无法补齐。
- **官方页面**：https://tushare.pro/document/2?doc_id=397

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | 股票代码 |
| trade_date | str | N | 交易日期（YYYYMMDD） |
| start_date | str | N | 开始时间 |
| end_date | str | N | 结束时间 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| name | str | Y | 股票名称 |
| trade_date | str | Y | 交易日期 |
| type | str | Y | 类型 |
| type_name | str | Y | 类型名称 |

**接口示例**

```python
pro = ts.pro_api()
# 获取 20250813 日所有的 ST 股票
df = pro.stock_st(trade_date='20250813')
```

---

### 4. 股票曾用名（namechange）

- **接口**：`namechange`
- **描述**：历史名称变更记录。
- **官方页面**：https://tushare.pro/document/2?doc_id=100

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | TS 代码 |
| start_date | str | N | 公告开始日期 |
| end_date | str | N | 公告结束日期 |

**输出参数**

| 名称 | 类型 | 默认输出 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 代码 |
| name | str | Y | 证券名称 |
| start_date | str | Y | 开始日期 |
| end_date | str | Y | 结束日期 |
| ann_date | str | Y | 公告日期 |
| change_reason | str | Y | 变更原因 |

**接口示例**

```python
pro = ts.pro_api()
df = pro.namechange(ts_code='600848.SH',
                    fields='ts_code,name,start_date,end_date,change_reason')
```

---

### 5. 上市公司基本信息（stock_company）

- **接口**：`stock_company`
- **描述**：获取上市公司基础信息，单次提取 4500 条，可根据交易所分批提取。
- **积分**：至少 120 积分。
- **官方页面**：https://tushare.pro/document/2?doc_id=112

**输入参数**

| 名称 | 类型 | 必须 | 描述 |
|---|---|---|---|
| ts_code | str | N | 股票代码 |
| exchange | str | N | 交易所代码，SSE 上交所 SZSE 深交所 BSE 北交所 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| com_name | str | Y | 公司全称 |
| com_id | str | Y | 统一社会信用代码 |
| exchange | str | Y | 交易所代码 |
| chairman | str | Y | 法人代表 |
| manager | str | Y | 总经理 |
| secretary | str | Y | 董秘 |
| reg_capital | float | Y | 注册资本（万元） |
| setup_date | str | Y | 注册日期 |
| province | str | Y | 所在省份 |
| city | str | Y | 所在城市 |
| introduction | str | N | 公司介绍 |
| website | str | Y | 公司主页 |
| email | str | Y | 电子邮件 |
| office | str | N | 办公室 |
| employees | int | Y | 员工人数 |
| main_business | str | N | 主要业务及产品 |
| business_scope | str | N | 经营范围 |

**接口示例**

```python
pro = ts.pro_api()
df = pro.stock_company(exchange='SZSE',
                       fields='ts_code,chairman,manager,secretary,reg_capital,setup_date,province')
```

---

### 6. 管理层薪酬和持股（stk_rewards）

- **接口**：`stk_rewards`
- **描述**：获取上市公司管理层薪酬和持股。
- **积分**：2000 积分可调取，5000 积分以上频次相对较高。
- **官方页面**：https://tushare.pro/document/2?doc_id=194

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码，支持单个或多个代码输入 |
| end_date | str | N | 报告期 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码 |
| ann_date | str | Y | 公告日期 |
| end_date | str | Y | 截止日期 |
| name | str | Y | 姓名 |
| title | str | Y | 职务 |
| reward | float | Y | 报酬（元） |
| hold_vol | float | Y | 持股数（股） |

**接口示例**

```python
pro = ts.pro_api()

# 获取单个公司高管全部数据
df = pro.stk_rewards(ts_code='000001.SZ')

# 获取多个公司高管全部数据
df = pro.stk_rewards(ts_code='000001.SZ,600000.SH')
```

---

### 7. IPO 新股列表（new_share）

- **接口**：`new_share`
- **描述**：获取新股上市列表数据。
- **限量**：单次最大 2000 条，总量不限制。
- **积分**：至少 120 积分。
- **官方页面**：https://tushare.pro/document/2?doc_id=123

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| start_date | str | N | 上网发行开始日期 |
| end_date | str | N | 上网发行结束日期 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码 |
| sub_code | str | Y | 申购代码 |
| name | str | Y | 名称 |
| ipo_date | str | Y | 上网发行日期 |
| issue_date | str | Y | 上市日期 |
| amount | float | Y | 发行总量（万股） |
| market_amount | float | Y | 上网发行总量（万股） |
| price | float | Y | 发行价格 |
| pe | float | Y | 市盈率 |
| limit_amount | float | Y | 个人申购上限（万股） |
| funds | float | Y | 募集资金（亿元） |
| ballot | float | Y | 中签率 |

**接口示例**

```python
pro = ts.pro_api()
df = pro.new_share(start_date='20180901', end_date='20181018')
```

---

## 二、行情数据

### 8. A 股日线行情（daily）

- **接口**：`daily`
- **数据说明**：交易日每天 15:00～16:00 之间入库。本接口是**未复权**行情，停牌期间不提供数据。
- **调取说明**：基础积分每分钟可调取 500 次，每次 6000 条数据，一次请求相当于提取一只股票 23 年历史。
- **描述**：获取股票行情数据，或通过通用行情接口获取包含前后复权的数据。
- **官方页面**：https://tushare.pro/document/2?doc_id=27

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | 股票代码（支持多个股票同时提取，逗号分隔） |
| trade_date | str | N | 交易日期（YYYYMMDD） |
| start_date | str | N | 开始日期（YYYYMMDD） |
| end_date | str | N | 结束日期（YYYYMMDD） |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| trade_date | str | Y | 交易日期 |
| open | float | Y | 开盘价 |
| high | float | Y | 最高价 |
| low | float | Y | 最低价 |
| close | float | Y | 收盘价 |
| pre_close | float | Y | 昨收价【除权价】 |
| change | float | Y | 涨跌额 |
| pct_chg | float | Y | 涨跌幅（%），基于除权后的昨收计算：(今收-除权昨收)/除权昨收 |
| vol | float | Y | 成交量（手） |
| amount | float | Y | 成交额（千元） |
| ah_vol | float | N | 盘后成交量（手） |
| ah_amount | float | N | 盘后成交额（千元） |

> 注：建议通过循环 trade_date 来提取全市场数据，不要通过循环 ts_code 拉取历史。

**接口示例**

```python
pro = ts.pro_api()

# 提取单个股票跨时间段的历史日线
df = pro.daily(ts_code='000001.SZ', start_date='20180701', end_date='20180718')

# 通过日期取历史某一天的全部股票数据
df = pro.daily(trade_date='20180810')

# 指定提取盘后固定成交量/金额（2026-07-06 开始有数据）
df = pro.daily(trade_date='20260707', fields='ts_code,open,close,ah_vol,ah_amount')
```

---

### 9. A 股复权行情（pro_bar）

- **接口**：`pro_bar`（通过通用行情接口实现）
- **接口说明**：复权行情利用 Tushare Pro 提供的复权因子进行动态计算，因此 **http 方式无法调取**。若需要静态复权行情（支持 http），请访问股票技术因子接口。
- **Python SDK 版本要求**：>= 1.2.26。
- **官方页面**：https://tushare.pro/document/2?doc_id=146

**复权说明**

| 类型 | 算法 | 参数标识 |
|---|---|---|
| 不复权 | 无 | 空或 None |
| 前复权 | 当日收盘价 × 当日复权因子 / 最新复权因子 | qfq |
| 后复权 | 当日收盘价 × 当日复权因子 | hfq |

> 注：目前只支持 A 股的日线复权。Tushare 以用户设定的 end_date 开始往前复权，与行情软件从最近交易日往前复权可能存在差异；Tushare 复权采用"分红再投"模式计算。

**接口参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 证券代码 |
| start_date | str | N | 开始日期（YYYYMMDD） |
| end_date | str | N | 结束日期（YYYYMMDD） |
| asset | str | Y | 资产类别：E 股票 I 沪深指数 FT 期货 FD 基金 O 期权，默认 E |
| adj | str | N | 复权类型（只针对股票）：None 未复权 qfq 前复权 hfq 后复权，默认 None |
| freq | str | Y | 数据频度：1MIN 表示 1 分钟（1/5/15/30/60 分钟），D 日线，默认 D |
| ma | list | N | 均线，支持任意周期的均价和均量，输入任意合理 int 数值 |

**接口示例**

```python
# 日线复权
df = ts.pro_bar(ts_code='000001.SZ', adj='qfq', start_date='20180101', end_date='20181011')  # 前复权
df = ts.pro_bar(ts_code='000001.SZ', adj='hfq', start_date='20180101', end_date='20181011')  # 后复权

# 周线复权
df = ts.pro_bar(ts_code='000001.SZ', freq='W', adj='qfq', start_date='20180101', end_date='20181011')

# 月线复权
df = ts.pro_bar(ts_code='000001.SZ', freq='M', adj='hfq', start_date='20180101', end_date='20181011')
```

---

### 10. 复权因子（adj_factor）

- **接口**：`adj_factor`
- **更新时间**：盘前 9:15～9:20 完成当日复权因子入库。
- **描述**：本接口由 Tushare 自行生产，获取股票复权因子，可提取单只股票全部历史复权因子，也可提取单日全部股票的复权因子。
- **积分要求**：2000 积分起，5000 以上可高频调取。
- **官方页面**：https://tushare.pro/document/2?doc_id=28

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | 股票代码 |
| trade_date | str | N | 交易日期（YYYYMMDD） |
| start_date | str | N | 开始日期 |
| end_date | str | N | 结束日期 |

**输出参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| trade_date | str | Y | 交易日期 |
| adj_factor | float | Y | 复权因子 |

**接口示例**

```python
pro = ts.pro_api()

# 提取 000001 全部复权因子
df = pro.adj_factor(ts_code='000001.SZ', trade_date='')

# 提取 2018 年 7 月 18 日全部股票复权因子
df = pro.adj_factor(ts_code='', trade_date='20180718')
# 等价：df = pro.query('adj_factor', trade_date='20180718')
```

---

### 11. 沪深股通十大成交股（hsgt_top10）

- **接口**：`hsgt_top10`
- **描述**：获取沪股通、深股通每日前十大成交详细数据，每天 18:00～20:00 之间完成当日更新。
- **官方页面**：https://tushare.pro/document/2?doc_id=48

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | 股票代码（与 trade_date 二选一） |
| trade_date | str | N | 交易日期（与 ts_code 二选一） |
| start_date | str | N | 开始日期 |
| end_date | str | N | 结束日期 |
| market_type | str | N | 市场类型（1：沪市 3：深市） |

**输出参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| trade_date | str | Y | 交易日期 |
| ts_code | str | Y | 股票代码 |
| name | str | Y | 股票名称 |
| close | float | Y | 收盘价 |
| change | float | Y | 涨跌额 |
| rank | int | Y | 资金排名 |
| market_type | str | Y | 市场类型（1：沪市 3：深市） |
| amount | float | Y | 成交金额（元） |
| net_amount | float | Y | 净成交金额（元） |
| buy | float | Y | 买入金额（元） |
| sell | float | Y | 卖出金额（元） |

**接口示例**

```python
pro = ts.pro_api()

# 按交易日取沪市十大成交股
pro.hsgt_top10(trade_date='20180725', market_type='1')

# 按股票代码取历史区间数据
pro.query('hsgt_top10', ts_code='600519.SH',
          start_date='20180701', end_date='20180725')
```

---

### 12. 每日指标（daily_basic）

- **接口**：`daily_basic`
- **更新时间**：交易日每日 15:00～17:00 之间。
- **描述**：获取全部股票每日重要的基本面指标，可用于选股分析、报表展示等。单次请求最大返回 6000 条数据，可按日线循环提取全部历史。
- **积分**：至少 2000 积分，5000 积分无总量限制。
- **官方页面**：https://tushare.pro/document/2?doc_id=32

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码（与 trade_date 二选一） |
| trade_date | str | N | 交易日期（与 ts_code 二选一） |
| start_date | str | N | 开始日期（YYYYMMDD） |
| end_date | str | N | 结束日期（YYYYMMDD） |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码 |
| trade_date | str | Y | 交易日期 |
| close | float | Y | 当日收盘价 |
| turnover_rate | float | Y | 换手率（成交量/无限售流通股数） |
| turnover_rate_f | float | Y | 换手率（自由流通股）（成交量/自由流通股数） |
| volume_ratio | float | Y | 量比 VOL/MA |
| pe | float | Y | 市盈率（总市值/净利润，亏损的 PE 为空） |
| pe_ttm | float | Y | 市盈率（总市值/净利润 TTM，亏损的 PE 为空） |
| pb | float | Y | 市净率（总市值/(净资产-其他权益工具)） |
| ps | float | Y | 市销率（总市值/营业收入(最新年报)） |
| ps_ttm | float | Y | 市销率（TTM）（总市值/营业收入 TTM） |
| dv_ratio | float | Y | 股息率（%），除息日发生在去年期间的派现 |
| dv_ttm | float | Y | 股息率（TTM）（%），除息日在近 12 个月且分红报告期在 12 个月以内的派现 |
| total_share | float | Y | 总股本（万股） |
| float_share | float | Y | 流通股本（万股） |
| free_share | float | Y | 自由流通股本（万） |
| total_mv | float | Y | 总市值（万元） |
| circ_mv | float | Y | 流通市值（万元） |
| limit_status | int | N | 收盘涨跌状态：0 平盘，1 上涨(不含涨停)，2 涨停(不含一字涨停)，3 一字涨停，4 下跌(不含跌停)，5 跌停(不含一字跌停)，6 一字跌停 |

**接口示例**

```python
pro = ts.pro_api()
df = pro.daily_basic(ts_code='', trade_date='20180726',
                     fields='ts_code,trade_date,turnover_rate,volume_ratio,pe,pb')
```

---

## 三、财务数据

### 13. 股东人数（stk_holdernumber）

- **接口**：`stk_holdernumber`
- **描述**：获取上市公司股东户数数据，数据不定期公布。
- **限量**：单次最大 3000，总量不限制。
- **积分**：2000 积分可调取，基础积分每分钟调取 200 次，5000 积分以上频次相对较高。
- **官方页面**：https://tushare.pro/document/2?doc_id=166

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | TS 股票代码 |
| ann_date | str | N | 公告日期 |
| enddate | str | N | 截止日期 |
| start_date | str | N | 公告开始日期 |
| end_date | str | N | 公告结束日期 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码 |
| ann_date | str | Y | 公告日期 |
| end_date | str | Y | 截止日期 |
| holder_num | int | Y | 股东户数 |

**接口示例**

```python
pro = ts.pro_api()
df = pro.stk_holdernumber(ts_code='300199.SZ',
                          start_date='20160101', end_date='20181231')
```

---

### 14. 利润表（income）

- **接口**：`income`
- **描述**：获取上市公司财务利润表数据。
- **积分**：至少 2000 积分。
- **提示**：当前接口只能按单只股票获取其历史数据；如需获取某一季度全部上市公司数据，请使用 `income_vip` 接口（参数一致），需 5000 积分。
- **官方页面**：https://tushare.pro/document/2?doc_id=33

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| ann_date | str | N | 公告日期（YYYYMMDD） |
| f_ann_date | str | N | 实际公告日期 |
| start_date | str | N | 公告日开始日期 |
| end_date | str | N | 公告日结束日期 |
| period | str | N | 报告期（每季度最后一天日期，如 20171231 年报，20170630 半年报，20170930 三季报） |
| report_type | str | N | 报告类型，见下方"报表类型说明" |
| comp_type | str | N | 公司类型（1 一般工商业 2 银行 3 保险 4 证券 7 多元金融） |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 代码 |
| ann_date | str | Y | 公告日期 |
| f_ann_date | str | Y | 实际公告日期 |
| end_date | str | Y | 报告期 |
| report_type | str | Y | 报告类型（见底部表） |
| comp_type | str | Y | 公司类型（1 一般工商业 2 银行 3 保险 4 证券 7 多元金融） |
| end_type | str | Y | 报告期类型 |
| basic_eps | float | Y | 基本每股收益 |
| diluted_eps | float | Y | 稀释每股收益 |
| total_revenue | float | Y | 营业总收入 |
| revenue | float | Y | 营业收入 |
| int_income | float | Y | 利息收入 |
| prem_earned | float | Y | 已赚保费 |
| comm_income | float | Y | 手续费及佣金收入 |
| n_commis_income | float | Y | 手续费及佣金净收入 |
| n_oth_income | float | Y | 其他经营净收益 |
| n_oth_b_income | float | Y | 加:其他业务净收益 |
| prem_income | float | Y | 保险业务收入 |
| out_prem | float | Y | 减:分出保费 |
| une_prem_reser | float | Y | 提取未到期责任准备金 |
| reins_income | float | Y | 其中:分保费收入 |
| n_sec_tb_income | float | Y | 代理买卖证券业务净收入 |
| n_sec_uw_income | float | Y | 证券承销业务净收入 |
| n_asset_mg_income | float | Y | 受托客户资产管理业务净收入 |
| oth_b_income | float | Y | 其他业务收入 |
| fv_value_chg_gain | float | Y | 加:公允价值变动净收益 |
| invest_income | float | Y | 加:投资净收益 |
| ass_invest_income | float | Y | 其中:对联营企业和合营企业的投资收益 |
| forex_gain | float | Y | 加:汇兑净收益 |
| total_cogs | float | Y | 营业总成本 |
| oper_cost | float | Y | 减:营业成本 |
| int_exp | float | Y | 减:利息支出 |
| comm_exp | float | Y | 减:手续费及佣金支出 |
| biz_tax_surchg | float | Y | 减:营业税金及附加 |
| sell_exp | float | Y | 减:销售费用 |
| admin_exp | float | Y | 减:管理费用 |
| fin_exp | float | Y | 减:财务费用 |
| assets_impair_loss | float | Y | 减:资产减值损失 |
| prem_refund | float | Y | 退保金 |
| compens_payout | float | Y | 赔付总支出 |
| reser_insur_liab | float | Y | 提取保险责任准备金 |
| div_payt | float | Y | 保户红利支出 |
| reins_exp | float | Y | 分保费用 |
| oper_exp | float | Y | 营业支出 |
| compens_payout_refu | float | Y | 减:摊回赔付支出 |
| insur_reser_refu | float | Y | 减:摊回保险责任准备金 |
| reins_cost_refund | float | Y | 减:摊回分保费用 |
| other_bus_cost | float | Y | 其他业务成本 |
| operate_profit | float | Y | 营业利润 |
| non_oper_income | float | Y | 加:营业外收入 |
| non_oper_exp | float | Y | 减:营业外支出 |
| nca_disploss | float | Y | 其中:减:非流动资产处置净损失 |
| total_profit | float | Y | 利润总额 |
| income_tax | float | Y | 所得税费用 |
| n_income | float | Y | 净利润(含少数股东损益) |
| n_income_attr_p | float | Y | 净利润(不含少数股东损益) |
| minority_gain | float | Y | 少数股东损益 |
| oth_compr_income | float | Y | 其他综合收益 |
| t_compr_income | float | Y | 综合收益总额 |
| compr_inc_attr_p | float | Y | 归属于母公司(或股东)的综合收益总额 |
| compr_inc_attr_m_s | float | Y | 归属于少数股东的综合收益总额 |
| ebit | float | Y | 息税前利润 |
| ebitda | float | Y | 息税折旧摊销前利润 |
| insurance_exp | float | Y | 保险业务支出 |
| undist_profit | float | Y | 年初未分配利润 |
| distable_profit | float | Y | 可分配利润 |
| rd_exp | float | Y | 研发费用 |
| fin_exp_int_exp | float | Y | 财务费用:利息费用 |
| fin_exp_int_inc | float | Y | 财务费用:利息收入 |
| transfer_surplus_rese | float | Y | 盈余公积转入 |
| transfer_housing_imprest | float | Y | 住房周转金转入 |
| transfer_oth | float | Y | 其他转入 |
| adj_lossgain | float | Y | 调整以前年度损益 |
| withdra_legal_surplus | float | Y | 提取法定盈余公积 |
| withdra_legal_pubfund | float | Y | 提取法定公益金 |
| withdra_biz_devfund | float | Y | 提取企业发展基金 |
| withdra_rese_fund | float | Y | 提取储备基金 |
| withdra_oth_ersu | float | Y | 提取任意盈余公积金 |
| workers_welfare | float | Y | 职工奖金福利 |
| distr_profit_shrhder | float | Y | 可供股东分配的利润 |
| prfshare_payable_dvd | float | Y | 应付优先股股利 |
| comshare_payable_dvd | float | Y | 应付普通股股利 |
| capit_comstock_div | float | Y | 转作股本的普通股股利 |
| net_after_nr_lp_correct | float | N | 扣除非经常性损益后的净利润（更正前） |
| credit_impa_loss | float | N | 信用减值损失 |
| net_expo_hedging_benefits | float | N | 净敞口套期收益 |
| oth_impair_loss_assets | float | N | 其他资产减值损失 |
| total_opcost | float | N | 营业总成本（二） |
| amodcost_fin_assets | float | N | 以摊余成本计量的金融资产终止确认收益 |
| oth_income | float | N | 其他收益 |
| asset_disp_income | float | N | 资产处置收益 |
| continued_net_profit | float | N | 持续经营净利润 |
| end_net_profit | float | N | 终止经营净利润 |
| update_flag | str | Y | 更新标识 |

**报表类型说明（report_type）**

| 代码 | 类型 | 说明 |
|---|---|---|
| 1 | 合并报表 | 上市公司最新报表（默认） |
| 2 | 单季合并 | 单一季度的合并报表 |
| 3 | 调整单季合并表 | 调整后的单季合并报表（如果有） |
| 4 | 调整合并报表 | 本年度公布上年同期的财务报表数据，报告期为上年度 |
| 5 | 调整前合并报表 | 数据发生变更，将原数据进行保留，即调整前的原数据 |
| 6 | 母公司报表 | 该公司母公司的财务报表数据 |
| 7 | 母公司单季表 | 母公司的单季度表 |
| 8 | 母公司调整单季表 | 母公司调整后的单季表 |
| 9 | 母公司调整表 | 该公司母公司的本年度公布上年同期的财务报表数据 |
| 10 | 母公司调整前报表 | 母公司调整之前的原始财务报表数据 |
| 11 | 母公司调整前合并报表 | 母公司调整之前合并报表原数据 |
| 12 | 母公司调整前报表 | 母公司报表发生变更前保留的原数据 |

**接口示例**

```python
pro = ts.pro_api()

# 按单只股票取历史利润表
df = pro.income(ts_code='600000.SH', start_date='20180101', end_date='20180730',
                fields='ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,basic_eps,diluted_eps')

# 获取某一季度全部股票数据（需 5000 积分）
df = pro.income_vip(period='20181231',
                    fields='ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,basic_eps,diluted_eps')
```

---

### 15. 资产负债表（balancesheet）

- **接口**：`balancesheet`
- **描述**：获取上市公司资产负债表。
- **积分**：至少 2000 积分。
- **提示**：当前接口只能按单只股票获取其历史数据；如需获取某一季度全部上市公司数据，请使用 `balancesheet_vip` 接口（参数一致），需 5000 积分。
- **官方页面**：https://tushare.pro/document/2?doc_id=36

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| ann_date | str | N | 公告日期（YYYYMMDD） |
| start_date | str | N | 公告日开始日期 |
| end_date | str | N | 公告日结束日期 |
| period | str | N | 报告期（每季度最后一天日期，如 20171231 年报，20170630 半年报，20170930 三季报） |
| report_type | str | N | 报告类型，见下方"报表类型说明" |
| comp_type | str | N | 公司类型：1 一般工商业 2 银行 3 保险 4 证券 7 多元金融 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码 |
| ann_date | str | Y | 公告日期 |
| f_ann_date | str | Y | 实际公告日期 |
| end_date | str | Y | 报告期 |
| report_type | str | Y | 报表类型 |
| comp_type | str | Y | 公司类型（1 一般工商业 2 银行 3 保险 4 证券 7 多元金融） |
| end_type | str | Y | 报告期类型 |
| total_share | float | Y | 期末总股本 |
| cap_rese | float | Y | 资本公积金 |
| undistr_porfit | float | Y | 未分配利润 |
| surplus_rese | float | Y | 盈余公积金 |
| special_rese | float | Y | 专项储备 |
| money_cap | float | Y | 货币资金 |
| trad_asset | float | Y | 交易性金融资产 |
| notes_receiv | float | Y | 应收票据 |
| accounts_receiv | float | Y | 应收账款 |
| oth_receiv | float | Y | 其他应收款 |
| prepayment | float | Y | 预付款项 |
| div_receiv | float | Y | 应收股利 |
| int_receiv | float | Y | 应收利息 |
| inventories | float | Y | 存货 |
| amor_exp | float | Y | 待摊费用 |
| nca_within_1y | float | Y | 一年内到期的非流动资产 |
| sett_rsrv | float | Y | 结算备付金 |
| loanto_oth_bank_fi | float | Y | 拆出资金 |
| premium_receiv | float | Y | 应收保费 |
| reinsur_receiv | float | Y | 应收分保账款 |
| reinsur_res_receiv | float | Y | 应收分保合同准备金 |
| pur_resale_fa | float | Y | 买入返售金融资产 |
| oth_cur_assets | float | Y | 其他流动资产 |
| total_cur_assets | float | Y | 流动资产合计 |
| fa_avail_for_sale | float | Y | 可供出售金融资产 |
| htm_invest | float | Y | 持有至到期投资 |
| lt_eqt_invest | float | Y | 长期股权投资 |
| invest_real_estate | float | Y | 投资性房地产 |
| time_deposits | float | Y | 定期存款 |
| oth_assets | float | Y | 其他资产 |
| lt_rec | float | Y | 长期应收款 |
| fix_assets | float | Y | 固定资产 |
| cip | float | Y | 在建工程 |
| const_materials | float | Y | 工程物资 |
| fixed_assets_disp | float | Y | 固定资产清理 |
| produc_bio_assets | float | Y | 生产性生物资产 |
| oil_and_gas_assets | float | Y | 油气资产 |
| intan_assets | float | Y | 无形资产 |
| r_and_d | float | Y | 研发支出 |
| goodwill | float | Y | 商誉 |
| lt_amor_exp | float | Y | 长期待摊费用 |
| defer_tax_assets | float | Y | 递延所得税资产 |
| decr_in_disbur | float | Y | 发放贷款及垫款 |
| oth_nca | float | Y | 其他非流动资产 |
| total_nca | float | Y | 非流动资产合计 |
| cash_reser_cb | float | Y | 现金及存放中央银行款项 |
| depos_in_oth_bfi | float | Y | 存放同业和其它金融机构款项 |
| prec_metals | float | Y | 贵金属 |
| deriv_assets | float | Y | 衍生金融资产 |
| rr_reins_une_prem | float | Y | 应收分保未到期责任准备金 |
| rr_reins_outstd_cla | float | Y | 应收分保未决赔款准备金 |
| rr_reins_lins_liab | float | Y | 应收分保寿险责任准备金 |
| rr_reins_lthins_liab | float | Y | 应收分保长期健康险责任准备金 |
| refund_depos | float | Y | 存出保证金 |
| ph_pledge_loans | float | Y | 保户质押贷款 |
| refund_cap_depos | float | Y | 存出资本保证金 |
| indep_acct_assets | float | Y | 独立账户资产 |
| client_depos | float | Y | 其中：客户资金存款 |
| client_prov | float | Y | 其中：客户备付金 |
| transac_seat_fee | float | Y | 其中:交易席位费 |
| invest_as_receiv | float | Y | 应收款项类投资 |
| total_assets | float | Y | 资产总计 |
| lt_borr | float | Y | 长期借款 |
| st_borr | float | Y | 短期借款 |
| cb_borr | float | Y | 向中央银行借款 |
| depos_ib_deposits | float | Y | 吸收存款及同业存放 |
| loan_oth_bank | float | Y | 拆入资金 |
| trading_fl | float | Y | 交易性金融负债 |
| notes_payable | float | Y | 应付票据 |
| acct_payable | float | Y | 应付账款 |
| adv_receipts | float | Y | 预收款项 |
| sold_for_repur_fa | float | Y | 卖出回购金融资产款 |
| comm_payable | float | Y | 应付手续费及佣金 |
| payroll_payable | float | Y | 应付职工薪酬 |
| taxes_payable | float | Y | 应交税费 |
| int_payable | float | Y | 应付利息 |
| div_payable | float | Y | 应付股利 |
| oth_payable | float | Y | 其他应付款 |
| acc_exp | float | Y | 预提费用 |
| deferred_inc | float | Y | 递延收益 |
| st_bonds_payable | float | Y | 应付短期债券 |
| payable_to_reinsurer | float | Y | 应付分保账款 |
| rsrv_insur_cont | float | Y | 保险合同准备金 |
| acting_trading_sec | float | Y | 代理买卖证券款 |
| acting_uw_sec | float | Y | 代理承销证券款 |
| non_cur_liab_due_1y | float | Y | 一年内到期的非流动负债 |
| oth_cur_liab | float | Y | 其他流动负债 |
| total_cur_liab | float | Y | 流动负债合计 |
| bond_payable | float | Y | 应付债券 |
| lt_payable | float | Y | 长期应付款 |
| specific_payables | float | Y | 专项应付款 |
| estimated_liab | float | Y | 预计负债 |
| defer_tax_liab | float | Y | 递延所得税负债 |
| defer_inc_non_cur_liab | float | Y | 递延收益-非流动负债 |
| oth_ncl | float | Y | 其他非流动负债 |
| total_ncl | float | Y | 非流动负债合计 |
| depos_oth_bfi | float | Y | 同业和其它金融机构存放款项 |
| deriv_liab | float | Y | 衍生金融负债 |
| depos | float | Y | 吸收存款 |
| agency_bus_liab | float | Y | 代理业务负债 |
| oth_liab | float | Y | 其他负债 |
| prem_receiv_adva | float | Y | 预收保费 |
| depos_received | float | Y | 存入保证金 |
| ph_invest | float | Y | 保户储金及投资款 |
| reser_une_prem | float | Y | 未到期责任准备金 |
| reser_outstd_claims | float | Y | 未决赔款准备金 |
| reser_lins_liab | float | Y | 寿险责任准备金 |
| reser_lthins_liab | float | Y | 长期健康险责任准备金 |
| indept_acc_liab | float | Y | 独立账户负债 |
| pledge_borr | float | Y | 其中:质押借款 |
| indem_payable | float | Y | 应付赔付款 |
| policy_div_payable | float | Y | 应付保单红利 |
| total_liab | float | Y | 负债合计 |
| treasury_share | float | Y | 减:库存股 |
| ordin_risk_reser | float | Y | 一般风险准备 |
| forex_differ | float | Y | 外币报表折算差额 |
| invest_loss_unconf | float | Y | 未确认的投资损失 |
| minority_int | float | Y | 少数股东权益 |
| total_hldr_eqy_exc_min_int | float | Y | 股东权益合计(不含少数股东权益) |
| total_hldr_eqy_inc_min_int | float | Y | 股东权益合计(含少数股东权益) |
| total_liab_hldr_eqy | float | Y | 负债及股东权益总计 |
| lt_payroll_payable | float | Y | 长期应付职工薪酬 |
| oth_comp_income | float | Y | 其他综合收益 |
| oth_eqt_tools | float | Y | 其他权益工具 |
| oth_eqt_tools_p_shr | float | Y | 其他权益工具(优先股) |
| lending_funds | float | Y | 融出资金 |
| acc_receivable | float | Y | 应收款项 |
| st_fin_payable | float | Y | 应付短期融资款 |
| payables | float | Y | 应付款项 |
| hfs_assets | float | Y | 持有待售的资产 |
| hfs_sales | float | Y | 持有待售的负债 |
| cost_fin_assets | float | Y | 以摊余成本计量的金融资产 |
| fair_value_fin_assets | float | Y | 以公允价值计量且其变动计入其他综合收益的金融资产 |
| cip_total | float | Y | 在建工程(合计)(元) |
| oth_pay_total | float | Y | 其他应付款(合计)(元) |
| long_pay_total | float | Y | 长期应付款(合计)(元) |
| debt_invest | float | Y | 债权投资(元) |
| oth_debt_invest | float | Y | 其他债权投资(元) |
| oth_eq_invest | float | N | 其他权益工具投资(元) |
| oth_illiq_fin_assets | float | N | 其他非流动金融资产(元) |
| oth_eq_ppbond | float | N | 其他权益工具:永续债(元) |
| receiv_financing | float | N | 应收款项融资 |
| use_right_assets | float | N | 使用权资产 |
| lease_liab | float | N | 租赁负债 |
| contract_assets | float | Y | 合同资产 |
| contract_liab | float | Y | 合同负债 |
| accounts_receiv_bill | float | Y | 应收票据及应收账款 |
| accounts_pay | float | Y | 应付票据及应付账款 |
| oth_rcv_total | float | Y | 其他应收款(合计)（元） |
| fix_assets_total | float | Y | 固定资产(合计)(元) |
| update_flag | str | Y | 更新标识 |

**报表类型说明（report_type）**

| 代码 | 类型 | 说明 |
|---|---|---|
| 1 | 合并报表 | 上市公司最新报表（默认） |
| 2 | 单季合并 | 单一季度的合并报表 |
| 3 | 调整单季合并表 | 调整后的单季合并报表（如果有） |
| 4 | 调整合并报表 | 本年度公布上年同期的财务报表数据，报告期为上年度 |
| 5 | 调整前合并报表 | 数据发生变更，将原数据进行保留，即调整前的原数据 |
| 6 | 母公司报表 | 该公司母公司的财务报表数据 |
| 7 | 母公司单季表 | 母公司的单季度表 |
| 8 | 母公司调整单季表 | 母公司调整后的单季表 |
| 9 | 母公司调整表 | 该公司母公司的本年度公布上年同期的财务报表数据 |
| 10 | 母公司调整前报表 | 母公司调整之前的原始财务报表数据 |
| 11 | 母公司调整前合并报表 | 母公司调整之前合并报表原数据 |
| 12 | 母公司调整前报表 | 母公司报表发生变更前保留的原数据 |

**接口示例**

```python
pro = ts.pro_api()

# 按单只股票取历史资产负债表
df = pro.balancesheet(ts_code='600000.SH', start_date='20180101', end_date='20180730',
                      fields='ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,cap_rese')

# 获取某一季度全部股票数据（需 5000 积分）
df2 = pro.balancesheet_vip(period='20181231',
                           fields='ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,cap_rese')
```

---

### 16. 现金流量表（cashflow）

- **接口**：`cashflow`
- **描述**：获取上市公司现金流量表。
- **积分**：至少 2000 积分。
- **提示**：当前接口只能按单只股票获取其历史数据；如需获取某一季度全部上市公司数据，请使用 `cashflow_vip` 接口（参数一致），需 5000 积分。
- **官方页面**：https://tushare.pro/document/2?doc_id=44

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | Y | 股票代码 |
| ann_date | str | N | 公告日期（YYYYMMDD） |
| f_ann_date | str | N | 实际公告日期 |
| start_date | str | N | 公告日开始日期 |
| end_date | str | N | 公告日结束日期 |
| period | str | N | 报告期（每季度最后一天日期，如 20171231 年报，20170630 半年报，20170930 三季报） |
| report_type | str | N | 报告类型，见下方"报表类型说明" |
| comp_type | str | N | 公司类型：1 一般工商业 2 银行 3 保险 4 证券 7 多元金融 |
| is_calc | int | N | 是否计算报表 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 股票代码 |
| ann_date | str | Y | 公告日期 |
| f_ann_date | str | Y | 实际公告日期 |
| end_date | str | Y | 报告期 |
| comp_type | str | Y | 公司类型（1 一般工商业 2 银行 3 保险 4 证券 7 多元金融） |
| report_type | str | Y | 报表类型 |
| end_type | str | Y | 报告期类型 |
| net_profit | float | Y | 净利润 |
| finan_exp | float | Y | 财务费用 |
| c_fr_sale_sg | float | Y | 销售商品、提供劳务收到的现金 |
| recp_tax_rends | float | Y | 收到的税费返还 |
| n_depos_incr_fi | float | Y | 客户存款和同业存放款项净增加额 |
| n_incr_loans_cb | float | Y | 向中央银行借款净增加额 |
| n_inc_borr_oth_fi | float | Y | 向其他金融机构拆入资金净增加额 |
| prem_fr_orig_contr | float | Y | 收到原保险合同保费取得的现金 |
| n_incr_insured_dep | float | Y | 保户储金净增加额 |
| n_reinsur_prem | float | Y | 收到再保业务现金净额 |
| n_incr_disp_tfa | float | Y | 处置交易性金融资产净增加额 |
| ifc_cash_incr | float | Y | 收取利息和手续费净增加额 |
| n_incr_disp_faas | float | Y | 处置可供出售金融资产净增加额 |
| n_incr_loans_oth_bank | float | Y | 拆入资金净增加额 |
| n_cap_incr_repur | float | Y | 回购业务资金净增加额 |
| c_fr_oth_operate_a | float | Y | 收到其他与经营活动有关的现金 |
| c_inf_fr_operate_a | float | Y | 经营活动现金流入小计 |
| c_paid_goods_s | float | Y | 购买商品、接受劳务支付的现金 |
| c_paid_to_for_empl | float | Y | 支付给职工以及为职工支付的现金 |
| c_paid_for_taxes | float | Y | 支付的各项税费 |
| n_incr_clt_loan_adv | float | Y | 客户贷款及垫款净增加额 |
| n_incr_dep_cbob | float | Y | 存放央行和同业款项净增加额 |
| c_pay_claims_orig_inco | float | Y | 支付原保险合同赔付款项的现金 |
| pay_handling_chrg | float | Y | 支付手续费的现金 |
| pay_comm_insur_plcy | float | Y | 支付保单红利的现金 |
| oth_cash_pay_oper_act | float | Y | 支付其他与经营活动有关的现金 |
| st_cash_out_act | float | Y | 经营活动现金流出小计 |
| n_cashflow_act | float | Y | 经营活动产生的现金流量净额 |
| oth_recp_ral_inv_act | float | Y | 收到其他与投资活动有关的现金 |
| c_disp_withdrwl_invest | float | Y | 收回投资收到的现金 |
| c_recp_return_invest | float | Y | 取得投资收益收到的现金 |
| n_recp_disp_fiolta | float | Y | 处置固定资产、无形资产和其他长期资产收回的现金净额 |
| n_recp_disp_sobu | float | Y | 处置子公司及其他营业单位收到的现金净额 |
| stot_inflows_inv_act | float | Y | 投资活动现金流入小计 |
| c_pay_acq_const_fiolta | float | Y | 购建固定资产、无形资产和其他长期资产支付的现金 |
| c_paid_invest | float | Y | 投资支付的现金 |
| n_disp_subs_oth_biz | float | Y | 取得子公司及其他营业单位支付的现金净额 |
| oth_pay_ral_inv_act | float | Y | 支付其他与投资活动有关的现金 |
| n_incr_pledge_loan | float | Y | 质押贷款净增加额 |
| stot_out_inv_act | float | Y | 投资活动现金流出小计 |
| n_cashflow_inv_act | float | Y | 投资活动产生的现金流量净额 |
| c_recp_borrow | float | Y | 取得借款收到的现金 |
| proc_issue_bonds | float | Y | 发行债券收到的现金 |
| oth_cash_recp_ral_fnc_act | float | Y | 收到其他与筹资活动有关的现金 |
| stot_cash_in_fnc_act | float | Y | 筹资活动现金流入小计 |
| free_cashflow | float | Y | 企业自由现金流量 |
| c_prepay_amt_borr | float | Y | 偿还债务支付的现金 |
| c_pay_dist_dpcp_int_exp | float | Y | 分配股利、利润或偿付利息支付的现金 |
| incl_dvd_profit_paid_sc_ms | float | Y | 其中:子公司支付给少数股东的股利、利润 |
| oth_cashpay_ral_fnc_act | float | Y | 支付其他与筹资活动有关的现金 |
| stot_cashout_fnc_act | float | Y | 筹资活动现金流出小计 |
| n_cash_flows_fnc_act | float | Y | 筹资活动产生的现金流量净额 |
| eff_fx_flu_cash | float | Y | 汇率变动对现金的影响 |
| n_incr_cash_cash_equ | float | Y | 现金及现金等价物净增加额 |
| c_cash_equ_beg_period | float | Y | 期初现金及现金等价物余额 |
| c_cash_equ_end_period | float | Y | 期末现金及现金等价物余额 |
| c_recp_cap_contrib | float | Y | 吸收投资收到的现金 |
| incl_cash_rec_saims | float | Y | 其中:子公司吸收少数股东投资收到的现金 |
| uncon_invest_loss | float | Y | 未确认投资损失 |
| prov_depr_assets | float | Y | 加:资产减值准备 |
| depr_fa_coga_dpba | float | Y | 固定资产折旧、油气资产折耗、生产性生物资产折旧 |
| amort_intang_assets | float | Y | 无形资产摊销 |
| lt_amort_deferred_exp | float | Y | 长期待摊费用摊销 |
| decr_deferred_exp | float | Y | 待摊费用减少 |
| incr_acc_exp | float | Y | 预提费用增加 |
| loss_disp_fiolta | float | Y | 处置固定、无形资产和其他长期资产的损失 |
| loss_scr_fa | float | Y | 固定资产报废损失 |
| loss_fv_chg | float | Y | 公允价值变动损失 |
| invest_loss | float | Y | 投资损失 |
| decr_def_inc_tax_assets | float | Y | 递延所得税资产减少 |
| incr_def_inc_tax_liab | float | Y | 递延所得税负债增加 |
| decr_inventories | float | Y | 存货的减少 |
| decr_oper_payable | float | Y | 经营性应收项目的减少 |
| incr_oper_payable | float | Y | 经营性应付项目的增加 |
| others | float | Y | 其他 |
| im_net_cashflow_oper_act | float | Y | 经营活动产生的现金流量净额(间接法) |
| conv_debt_into_cap | float | Y | 债务转为资本 |
| conv_copbonds_due_within_1y | float | Y | 一年内到期的可转换公司债券 |
| fa_fnc_leases | float | Y | 融资租入固定资产 |
| im_n_incr_cash_equ | float | Y | 现金及现金等价物净增加额(间接法) |
| net_dism_capital_add | float | Y | 拆出资金净增加额 |
| net_cash_rece_sec | float | Y | 代理买卖证券收到的现金净额(元) |
| credit_impa_loss | float | Y | 信用减值损失 |
| use_right_asset_dep | float | Y | 使用权资产折旧 |
| oth_loss_asset | float | Y | 其他资产减值损失 |
| end_bal_cash | float | Y | 现金的期末余额 |
| beg_bal_cash | float | Y | 减:现金的期初余额 |
| end_bal_cash_equ | float | Y | 加:现金等价物的期末余额 |
| beg_bal_cash_equ | float | Y | 减:现金等价物的期初余额 |
| update_flag | str | Y | 更新标志(1 最新) |

**报表类型说明（report_type）**

| 代码 | 类型 | 说明 |
|---|---|---|
| 1 | 合并报表 | 上市公司最新报表（默认） |
| 2 | 单季合并 | 单一季度的合并报表 |
| 3 | 调整单季合并表 | 调整后的单季合并报表（如果有） |
| 4 | 调整合并报表 | 本年度公布上年同期的财务报表数据，报告期为上年度 |
| 5 | 调整前合并报表 | 数据发生变更，将原数据进行保留，即调整前的原数据 |
| 6 | 母公司报表 | 该公司母公司的财务报表数据 |
| 7 | 母公司单季表 | 母公司的单季度表 |
| 8 | 母公司调整单季表 | 母公司调整后的单季表 |
| 9 | 母公司调整表 | 该公司母公司的本年度公布上年同期的财务报表数据 |
| 10 | 母公司调整前报表 | 母公司调整之前的原始财务报表数据 |
| 11 | 母公司调整前合并报表 | 母公司调整之前合并报表原数据 |
| 12 | 母公司调整前报表 | 母公司报表发生变更前保留的原数据 |

**接口示例**

```python
pro = ts.pro_api()

# 按单只股票取历史现金流量表
df = pro.cashflow(ts_code='600000.SH', start_date='20180101', end_date='20180730')

# 获取某一季度全部股票数据（需 5000 积分）
df2 = pro.cashflow_vip(period='20181231', fields='')
```

---

### 17. 股东增减持（stk_holdertrade）

- **接口**：`stk_holdertrade`
- **描述**：获取上市公司增减持数据，了解重要股东近期及历史上的股份增减变化。
- **限量**：单次最大提取 3000 行记录，总量不限制。
- **积分**：至少 2000 积分；基础积分有流量控制，5000 积分以上无明显限制。
- **官方页面**：https://tushare.pro/document/2?doc_id=175

**输入参数**

| 名称 | 类型 | 必选 | 描述 |
|---|---|---|---|
| ts_code | str | N | TS 股票代码 |
| ann_date | str | N | 公告日期 |
| start_date | str | N | 公告开始日期 |
| end_date | str | N | 公告结束日期 |
| trade_type | str | N | 交易类型 IN 增持 DE 减持 |
| holder_type | str | N | 股东类型 C 公司 P 个人 G 高管 |

**输出参数**

| 名称 | 类型 | 默认显示 | 描述 |
|---|---|---|---|
| ts_code | str | Y | TS 代码 |
| ann_date | str | Y | 公告日期 |
| holder_name | str | Y | 股东名称 |
| holder_type | str | Y | 股东类型 G 高管 P 个人 C 公司 |
| in_de | str | Y | 类型 IN 增持 DE 减持 |
| change_vol | float | Y | 变动数量 |
| change_ratio | float | Y | 占流通比例（%） |
| after_share | float | Y | 变动后持股 |
| after_ratio | float | Y | 变动后占流通比例（%） |
| avg_price | float | Y | 平均价格 |
| total_share | float | Y | 持股总数 |
| begin_date | str | N | 增减持开始日期 |
| close_date | str | N | 增减持结束日期 |

**接口示例**

```python
# 获取单日全部增减持数据
df = pro.stk_holdertrade(ann_date='20190426')

# 获取单个股票数据
df = pro.stk_holdertrade(ts_code='002149.SZ')

# 获取当日增持数据
df = pro.stk_holdertrade(ann_date='20190426', trade_type='IN')
```

---

## 附录：接口速查表

| 类别 | 接口名 | 接口函数 | 积分门槛 |
|---|---|---|---|
| 基础数据 | 股票基础信息 | stock_basic | 2000 |
| 基础数据 | 交易日历 | trade_cal | 2000 |
| 基础数据 | ST 股票列表 | stock_st | 3000 |
| 基础数据 | 股票曾用名 | namechange | — |
| 基础数据 | 上市公司基本信息 | stock_company | 120 |
| 基础数据 | 管理层薪酬和持股 | stk_rewards | 2000 |
| 基础数据 | IPO 新股列表 | new_share | 120 |
| 行情数据 | A 股日线行情 | daily | 基础积分 |
| 行情数据 | A 股复权行情 | pro_bar | SDK >= 1.2.26 |
| 行情数据 | 复权因子 | adj_factor | 2000 |
| 行情数据 | 沪深股通十大成交股 | hsgt_top10 | — |
| 行情数据 | 每日指标 | daily_basic | 2000 |
| 财务数据 | 股东人数 | stk_holdernumber | 2000 |
| 财务数据 | 利润表 | income / income_vip | 2000 / 5000 |
| 财务数据 | 资产负债表 | balancesheet / balancesheet_vip | 2000 / 5000 |
| 财务数据 | 现金流量表 | cashflow / cashflow_vip | 2000 / 5000 |
| 财务数据 | 股东增减持 | stk_holdertrade | 2000 |

> 数据来源：Tushare Pro 官方文档 https://tushare.pro/document/2 ，整理日期：2026-09-19。具体积分权限、更新时间与字段含义以官网最新说明为准。
