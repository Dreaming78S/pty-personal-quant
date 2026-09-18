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
- 因果约定（重要）：`generate_signals` 收到的是该股票整段已加载历史（含信号日之后的行情），策略必须只使用每行 `trade_date` 及之前的数据，禁止负向 `shift`、全序列归一化、反向窗口等引用未来行情的写法

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
