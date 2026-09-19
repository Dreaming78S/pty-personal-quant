# 我的策略（六策略）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/my-strategies.md` 实现 6 个选股策略（改写 2 个、新增 4 个），为引擎增加横截面预计算钩子与主板默认过滤。

**Architecture:** 在 `Strategy` 基类新增默认空实现的 `prepare(market)` 钩子，`compute_signals` 逐股计算前调用一次，使 `RpsBreakout` 能在全市场面板上做逐日横截面排名；选股范围（沪深主板）作为全局默认：回测 `default.yaml` 与 `select --boards` 均默认 `main`。

**Tech Stack:** Python 3.12、uv、pandas、pydantic、typer、pytest。

**Spec:** `docs/superpowers/specs/2026-09-19-my-strategies-design.md`（执行者需同时阅读该 spec 与 `docs/my-strategies.md`）

## Global Constraints

- 注册名固定：`ma_volume / turtle_trade / high_tight_flag / limit_up_shakeout / uptrend_limit_down / rps_breakout`；类名固定：`MaVolume / TurtleTrade / HighTightFlag / LimitUpShakeout / UptrendLimitDown / RpsBreakout`。
- 价格口径：均线/动量/极值/新高/支撑用后复权列（`open/high/low/close`）；涨停/跌停判定用不复权 `raw_close`；成交额 `amount` 单位为千元（判断"元"需 ×1000）。
- 因果约定：策略只允许使用每行 `trade_date` 及更早的数据；禁止负向 `shift`、全序列归一化、反向窗口。
- 窗口不足/NaN 处信号为 `False`，不得抛错。
- 不加新依赖；全部用户可见文本用中文；代码中不出现"（改）/（新）"等标注。
- TDD：每个任务先写失败测试并运行确认 RED，再实现转 GREEN，再提交。
- 提交信息前缀 `feat:/fix:/test:/docs:/chore:`；只 `git add` 显式文件路径，禁止 `git add .`。
- 运行测试统一 `uv run pytest -q`；单测时用 `uv run pytest tests/test_xxx.py -v`。
- 所有任务在分支 `feat/my-strategies` 上进行（Task 1 第 1 步创建）。

---

### Task 1: 引擎 prepare 钩子

**Files:**
- Modify: `src/quant/strategies/base.py`（`generate_signals` 之后、`rank` 之前新增方法）
- Modify: `src/quant/engine/selection.py:10-14`（`compute_signals` 首行调用钩子）
- Test: `tests/test_strategies_base.py`
- Test: `tests/test_selection.py`

**Interfaces:**
- Consumes: 现有 `Strategy` 基类与 `selection.compute_signals(strategy, market, rank_by)`。
- Produces: `Strategy.prepare(self, market: pd.DataFrame) -> pd.DataFrame`（默认原样返回）；Task 8 的 `RpsBreakout` 覆盖此方法。

- [ ] **Step 1: 创建分支**

```powershell
git checkout -b feat/my-strategies
git branch --show-current
```

Expected: 输出 `feat/my-strategies`。

- [ ] **Step 2: 写失败测试（基类默认行为）**

在 `tests/test_strategies_base.py` 顶部补 `import pandas as pd`，并在文件末尾追加：

```python
def test_prepare_default_returns_market_unchanged():
    market = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"],
                           "close": [10.0]})
    assert DummyStrategy().prepare(market) is market
```

在 `tests/test_selection.py` 的 `AboveStrategy` 定义之后追加：

```python
class EnrichStrategy(Strategy):
    Params = AboveParams

    def prepare(self, market):
        market = market.copy()
        market["double_close"] = market["close"] * 2
        return market

    def generate_signals(self, bars):
        return bars["double_close"] > self.p.threshold


def test_compute_signals_uses_prepared_columns():
    s = EnrichStrategy(threshold=23.0)
    signals = selection.compute_signals(s, make_market())
    by_code = signals.groupby("ts_code")["signal"].any().to_dict()
    assert by_code == {"600000.SH": False, "600001.SH": True}
```

（`make_market()` 里 `600000.SH` 收盘 11.0、`600001.SH` 收盘 12.0；×2 后 22 与 24，只有后者 > 23。）

- [ ] **Step 3: 运行确认 RED**

Run: `uv run pytest tests/test_strategies_base.py::test_prepare_default_returns_market_unchanged tests/test_selection.py::test_compute_signals_uses_prepared_columns -v`

Expected: 两个用例 FAIL（`AttributeError: 'DummyStrategy' object has no attribute 'prepare'` / `KeyError: 'double_close'`）。

- [ ] **Step 4: 实现**

在 `src/quant/strategies/base.py` 的 `generate_signals` 方法（abstractmethod）之后、`rank` 之前插入：

```python
    def prepare(self, market: pd.DataFrame) -> pd.DataFrame:
        """横截面预计算钩子：可返回追加了因子列的行情面板；默认原样返回。

        实现者必须返回副本（不得修改入参）；同一行只允许使用该行
        trade_date 及更早的数据，禁止引用未来行。
        """
        return market
```

把 `src/quant/engine/selection.py` 的 `compute_signals` 改为：

```python
def compute_signals(strategy: Strategy, market: pd.DataFrame,
                    rank_by: str = "amount") -> pd.DataFrame:
    market = strategy.prepare(market)
    frames = []
```

（其余函数体不变。）

- [ ] **Step 5: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategies_base.py tests/test_selection.py -v`

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add src/quant/strategies/base.py src/quant/engine/selection.py tests/test_strategies_base.py tests/test_selection.py
git commit -m "feat: add cross-sectional prepare hook to strategy engine"
```

---

### Task 2: 主板默认过滤（select --boards 与回测配置）

**Files:**
- Modify: `src/quant/cli.py:92-123`（`select` 命令新增 `--boards`）
- Modify: `src/quant/engine/backtest.py:62-68`（`filters_from_config` list→tuple）
- Modify: `configs/backtest/default.yaml`（新增一行 `allowed_boards`）
- Test: `tests/test_cli_select.py`
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `loader.UniverseFilters`（字段 `allowed_boards: tuple[str, ...] | None`）、`BacktestConfig.allowed_boards`。
- Produces: `select --boards`（默认 `main`；`all` 表示不限；支持逗号组合）；`filters_from_config` 输出 `allowed_boards` 为 tuple 或 None。

- [ ] **Step 1: 写失败测试**

在 `tests/test_cli_select.py` 末尾追加：

```python
def _patch_select(monkeypatch, captured):
    monkeypatch.setattr("quant.data.cache.ensure_all", lambda tables=None: None)
    monkeypatch.setattr("quant.engine.loader.resolve_trade_date",
                        lambda date=None: "20240103")
    monkeypatch.setattr("quant.engine.loader.load_market_data",
                        lambda **kwargs: pd.DataFrame({"dummy": [1]}))

    def fake_run_selection(strategy, trade_date, top_n=20, market=None,
                           filters=None, rank_by="amount"):
        captured["filters"] = filters
        return pd.DataFrame({"rank": [1], "ts_code": ["600000.SH"],
                             "name": ["浦发银行"]})

    monkeypatch.setattr("quant.engine.selection.run_selection", fake_run_selection)
    monkeypatch.setattr("quant.engine.report.save_selection",
                        lambda *a, **k: "outputs/select/x.csv")


def test_select_defaults_to_main_board(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume"])
    assert result.exit_code == 0
    assert captured["filters"].allowed_boards == ("main",)
    assert captured["filters"].exclude_st is True
    assert captured["filters"].min_list_days == 60


def test_select_boards_all_disables_board_filter(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume", "--boards", "all"])
    assert result.exit_code == 0
    assert captured["filters"].allowed_boards is None


def test_select_boards_accepts_comma_list(monkeypatch):
    captured = {}
    _patch_select(monkeypatch, captured)
    result = runner.invoke(app, ["select", "-s", "ma_volume",
                                 "--boards", "main,gem"])
    assert result.exit_code == 0
    assert captured["filters"].allowed_boards == ("main", "gem")
```

在 `tests/test_backtest.py` 末尾追加：

```python
def test_filters_from_config_converts_boards_to_tuple():
    config = BacktestConfig(start="20240101", end="20240131",
                            allowed_boards=["main"])
    filters = backtest.filters_from_config(config)
    assert filters.allowed_boards == ("main",)


def test_filters_from_config_keeps_none_boards():
    config = BacktestConfig(start="20240101", end="20240131")
    filters = backtest.filters_from_config(config)
    assert filters.allowed_boards is None
```

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_cli_select.py tests/test_backtest.py -v -k "boards or filters_from_config"`

Expected: CLI 三个用例 FAIL（`TypeError: ... unexpected keyword`/选项不存在）；tuple 用例 FAIL（list != tuple）。

- [ ] **Step 3: 实现**

`src/quant/engine/backtest.py` 的 `filters_from_config` 改为：

```python
def filters_from_config(config: BacktestConfig) -> UniverseFilters:
    boards = config.allowed_boards
    if boards is not None:
        boards = tuple(boards)
    return UniverseFilters(
        exclude_st=config.exclude_st,
        min_list_days=config.min_list_days,
        exclude_suspended=config.exclude_suspended,
        allowed_boards=boards,
    )
```

`src/quant/cli.py` 在 `select` 函数签名中 `rank_by` 选项后新增：

```python
    boards: str = typer.Option("main", "--boards",
                               help="板块过滤，逗号分隔 main/gem/star/bse；all 表示不限"),
```

在 `select` 函数体内、`strat = get_strategy(...)` 之前新增模块级辅助函数（放在 `select` 上方、`list_strategies_cmd` 下方）：

```python
def _parse_boards(value: str) -> tuple[str, ...] | None:
    value = value.strip().lower()
    if value in ("", "all"):
        return None
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    return parts or None
```

并把 `select` 中的调用改为：

```python
    result = selection.run_selection(
        strat, target, top_n=top, market=market,
        filters=loader.UniverseFilters(allowed_boards=_parse_boards(boards)),
        rank_by=rank_by)
```

`configs/backtest/default.yaml` 在 `exclude_suspended: true` 之后新增：

```yaml
allowed_boards: ["main"]
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_cli_select.py tests/test_backtest.py tests/test_cli_backtest.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/cli.py src/quant/engine/backtest.py configs/backtest/default.yaml tests/test_cli_select.py tests/test_backtest.py
git commit -m "feat: default selection universe to main board"
```

---

### Task 3: MaVolume 改写（以文档为准）

**Files:**
- Modify: `src/quant/strategies/ma_volume.py`（全部替换）
- Modify: `configs/strategies/ma_volume.yaml`
- Test: `tests/test_strategy_ma_volume.py`（全部替换）

**Interfaces:**
- Consumes: Task 1 的 `Strategy.prepare`（不覆盖）、现有注册器。
- Produces: 注册名 `ma_volume`；参数 `ma_short=5, ma_long=20, vol_ma=20, vol_ratio=1.5`；`warmup_days=21`。

- [ ] **Step 1: 写失败测试（替换整个测试文件）**

`tests/test_strategy_ma_volume.py` 全文：

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
    closes = [10, 10, 11, 9, 10, 11, 11]
    vols = [100, 100, 100, 100, 100, 500, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert list(signals) == [False, False, False, False, False, True, False]


def test_ma_volume_requires_volume_spike():
    closes = [10, 10, 11, 9, 10, 11, 11]
    vols = [100, 100, 100, 100, 100, 150, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_ma_volume_strict_cross_needs_yesterday_below():
    # 昨日 ma2 == ma3（相等不算"昨日小于"），今日上穿不命中
    closes = [10, 10, 10, 9, 11, 11, 11]
    vols = [100, 100, 100, 100, 100, 500, 100]
    s = get_strategy("ma_volume", ma_short=2, ma_long=3, vol_ma=3, vol_ratio=2.0)

    signals = s.generate_signals(make_bars(closes, vols))

    assert not signals.any()


def test_ma_volume_defaults_and_warmup():
    s = get_strategy("ma_volume")
    assert s.p.ma_short == 5
    assert s.p.ma_long == 20
    assert s.p.vol_ma == 20
    assert s.p.vol_ratio == 1.5
    assert s.warmup_days == 21


def test_ma_volume_no_signal_when_short_warmup_rows():
    closes = [10, 11]
    s = get_strategy("ma_volume")
    signals = s.generate_signals(make_bars(closes, [100, 100]))
    assert not signals.any()
```

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_strategy_ma_volume.py -v`

Expected: `test_ma_volume_defaults_and_warmup` FAIL（默认 2.0 != 1.5）；`test_ma_volume_strict_cross_needs_yesterday_below` FAIL（旧实现 `<=` 会命中）。

- [ ] **Step 3: 实现**

`src/quant/strategies/ma_volume.py` 全文：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class MaVolumeParams(BaseModel):
    ma_short: int = 5
    ma_long: int = 20
    vol_ma: int = 20
    vol_ratio: float = 1.5


@register_strategy("ma_volume")
class MaVolume(Strategy):
    """短均线上穿长均线，且当日明显放量。"""

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
        cross_up = (ma_short > ma_long) & (ma_short.shift(1) < ma_long.shift(1))
        signal = cross_up & (vol > self.p.vol_ratio * vol_ma)
        return signal.fillna(False)
```

`configs/strategies/ma_volume.yaml` 全文：

```yaml
strategy: ma_volume
params:
  ma_short: 5
  ma_long: 20
  vol_ma: 20
  vol_ratio: 1.5
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategy_ma_volume.py tests/test_selection.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/strategies/ma_volume.py configs/strategies/ma_volume.yaml tests/test_strategy_ma_volume.py
git commit -m "feat: align ma_volume strategy with my-strategies spec"
```

---

### Task 4: TurtleTrade 新增

**Files:**
- Create: `src/quant/strategies/turtle_trade.py`
- Create: `configs/strategies/turtle_trade.yaml`
- Test: `tests/test_strategy_turtle_trade.py`

**Interfaces:**
- Consumes: `Strategy.rank` 既有约定（返回 Series 作为 score）、行情列 `circ_mv`（万元）。
- Produces: 注册名 `turtle_trade`；参数 `high_days=20, min_amount=100000000.0`；`warmup_days=21`。

- [ ] **Step 1: 写失败测试**

`tests/test_strategy_turtle_trade.py` 全文：

```python
import pandas as pd

from quant.strategies import get_strategy

PARAMS = dict(high_days=3, min_amount=10_000_000.0)


def make_bars(highs, opens, closes, amounts, circ_mv=None):
    n = len(closes)
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(n)],
        "open": opens,
        "high": highs,
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
        "vol": [100.0] * n,
        "amount": amounts,
        "circ_mv": circ_mv if circ_mv is not None else [100.0] * n,
    })


def test_turtle_trade_true_on_breakout():
    highs = [10, 10, 10, 11]
    opens = [9.5, 9.5, 9.5, 10.5]
    closes = [10, 10, 10, 11]
    amounts = [5000, 5000, 5000, 20000]  # 千元，20000 千元 = 2000 万元
    s = get_strategy("turtle_trade", **PARAMS)

    signals = s.generate_signals(make_bars(highs, opens, closes, amounts))

    assert list(signals) == [False, False, False, True]


def test_turtle_trade_false_without_liquidity():
    amounts = [5000, 5000, 5000, 10000]  # 10000 千元 = 1000 万元，未"过亿"
    s = get_strategy("turtle_trade", **PARAMS)
    signals = s.generate_signals(
        make_bars([10, 10, 10, 11], [9.5, 9.5, 9.5, 10.5], [10, 10, 10, 11], amounts))
    assert not signals.any()


def test_turtle_trade_false_on_bearish_candle():
    opens = [9.5, 9.5, 9.5, 12.0]
    s = get_strategy("turtle_trade", **PARAMS)
    signals = s.generate_signals(
        make_bars([10, 10, 10, 12], opens, [10, 10, 10, 11],
                  [5000, 5000, 5000, 20000]))
    assert not signals.any()


def test_turtle_trade_false_without_true_rise():
    closes = [10, 10, 10, 10]
    s = get_strategy("turtle_trade", **PARAMS)
    signals = s.generate_signals(
        make_bars([10, 10, 10, 10.5], [9.5, 9.5, 9.5, 9.5], closes,
                  [5000, 5000, 5000, 20000]))
    assert not signals.any()


def test_turtle_trade_rank_is_circ_mv():
    bars = make_bars([10], [9.5], [10], [5000], circ_mv=[123.4])
    s = get_strategy("turtle_trade")
    assert list(s.rank(bars)) == [123.4]


def test_turtle_trade_defaults_and_warmup():
    s = get_strategy("turtle_trade")
    assert s.p.high_days == 20
    assert s.p.min_amount == 100_000_000.0
    assert s.warmup_days == 21
```

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_strategy_turtle_trade.py -v`

Expected: FAIL（`KeyError: '未注册的策略：turtle_trade'`）。

- [ ] **Step 3: 实现**

`src/quant/strategies/turtle_trade.py` 全文：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class TurtleTradeParams(BaseModel):
    high_days: int = 20
    min_amount: float = 100_000_000.0


@register_strategy("turtle_trade")
class TurtleTrade(Strategy):
    """20 日新高 + 成交额过亿 + 阳线真涨，按流通市值从大到小排序。"""

    Params = TurtleTradeParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.high_days + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close = bars["close"]
        prev_high = bars["high"].shift(1).rolling(p.high_days).max()
        amount_yuan = bars["amount"] * 1000.0
        signal = (
            (close > prev_high)
            & (amount_yuan > p.min_amount)
            & (close > bars["open"])
            & (close > close.shift(1))
        )
        return signal.fillna(False)

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        return pd.to_numeric(bars["circ_mv"], errors="coerce")
```

`configs/strategies/turtle_trade.yaml` 全文：

```yaml
strategy: turtle_trade
params:
  high_days: 20
  min_amount: 100000000
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategy_turtle_trade.py tests/test_strategies_base.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/strategies/turtle_trade.py configs/strategies/turtle_trade.yaml tests/test_strategy_turtle_trade.py
git commit -m "feat: add turtle_trade strategy"
```

---

### Task 5: HighTightFlag 改写（以文档为准）

**Files:**
- Modify: `src/quant/strategies/high_tight_flag.py`（全部替换）
- Modify: `configs/strategies/high_tight_flag.yaml`
- Test: `tests/test_strategy_high_tight_flag.py`（全部替换）

**Interfaces:**
- Consumes: 现有注册器；后复权 `high/low/close/vol` 列。
- Produces: 注册名 `high_tight_flag`；参数 `momentum_days=40, momentum_ratio=1.6, tight_days=10, tight_ratio=1.15, support_ratio=0.8, vol_base_days=20, vol_shrink=0.6`；`warmup_days=40`。

- [ ] **Step 1: 写失败测试（替换整个测试文件）**

`tests/test_strategy_high_tight_flag.py` 全文：

```python
import pandas as pd

from quant.strategies import get_strategy

PARAMS = dict(momentum_days=5, momentum_ratio=1.6, tight_days=3,
              tight_ratio=1.15, support_ratio=0.8, vol_base_days=3,
              vol_shrink=0.6)

LOWS = [10.0, 10.0, 10.0, 16.2, 16.1, 16.2, 16.1]
HIGHS = [10.3, 10.3, 20.0, 16.6, 16.5, 16.5, 16.5]
CLOSES = [10.1, 10.2, 19.5, 16.4, 16.3, 16.4, 16.3]
VOLS = [100, 100, 300, 100, 100, 100, 30]


def make_bars(lows, highs, closes, vols):
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(len(closes))],
        "open": closes, "high": highs, "low": lows, "close": closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_high_tight_flag_true_on_last_bar():
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(LOWS, HIGHS, CLOSES, VOLS))
    assert list(signals) == [False] * 6 + [True]


def test_high_tight_flag_false_without_volume_dry_up():
    vols = [100, 100, 300, 100, 100, 100, 100]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(LOWS, HIGHS, CLOSES, vols))
    assert not signals.any()


def test_high_tight_flag_false_when_support_breaks():
    lows = LOWS[:-1] + [15.0]
    s = get_strategy("high_tight_flag", **PARAMS)
    signals = s.generate_signals(make_bars(lows, HIGHS, CLOSES, VOLS))
    assert not signals.any()


def test_high_tight_flag_defaults_and_warmup():
    s = get_strategy("high_tight_flag")
    assert s.p.momentum_days == 40
    assert s.p.momentum_ratio == 1.6
    assert s.p.tight_days == 10
    assert s.p.tight_ratio == 1.15
    assert s.p.support_ratio == 0.8
    assert s.p.vol_base_days == 20
    assert s.p.vol_shrink == 0.6
    assert s.warmup_days == 40
```

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_strategy_high_tight_flag.py -v`

Expected: FAIL（旧参数名不匹配：`lookback`/`min_gain` 等未知参数抛出 pydantic 校验错误）。

- [ ] **Step 3: 实现**

`src/quant/strategies/high_tight_flag.py` 全文：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class HighTightFlagParams(BaseModel):
    momentum_days: int = 40
    momentum_ratio: float = 1.6
    tight_days: int = 10
    tight_ratio: float = 1.15
    support_ratio: float = 0.8
    vol_base_days: int = 20
    vol_shrink: float = 0.6


@register_strategy("high_tight_flag")
class HighTightFlag(Strategy):
    """强动量后高位窄幅缩量整理（高而窄的旗形）。"""

    Params = HighTightFlagParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = max(self.p.momentum_days, self.p.vol_base_days + 1)

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        high, low, vol = bars["high"], bars["low"], bars["vol"]

        momentum = (high.rolling(p.momentum_days).max()
                    / low.rolling(p.momentum_days).min())
        tight = (high.rolling(p.tight_days).max()
                 / low.rolling(p.tight_days).min())
        support = (low.rolling(p.tight_days).min()
                   >= p.support_ratio * high.rolling(p.momentum_days).max())
        quiet = vol < p.vol_shrink * vol.shift(1).rolling(p.vol_base_days).mean()

        signal = ((momentum > p.momentum_ratio) & (tight < p.tight_ratio)
                  & support & quiet)
        return signal.fillna(False)
```

`configs/strategies/high_tight_flag.yaml` 全文：

```yaml
strategy: high_tight_flag
params:
  momentum_days: 40
  momentum_ratio: 1.6
  tight_days: 10
  tight_ratio: 1.15
  support_ratio: 0.8
  vol_base_days: 20
  vol_shrink: 0.6
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategy_high_tight_flag.py tests/test_strategies_base.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/strategies/high_tight_flag.py configs/strategies/high_tight_flag.yaml tests/test_strategy_high_tight_flag.py
git commit -m "feat: align high_tight_flag strategy with my-strategies spec"
```

---

### Task 6: LimitUpShakeout 新增

**Files:**
- Create: `src/quant/strategies/limit_up_shakeout.py`
- Create: `configs/strategies/limit_up_shakeout.yaml`
- Test: `tests/test_strategy_limit_up_shakeout.py`

**Interfaces:**
- Consumes: 行情列 `raw_close`（不复权）、`open/high/low/close/vol`（后复权）。
- Produces: 注册名 `limit_up_shakeout`；参数 `limit_pct=0.095, vol_ratio=2.0`；`warmup_days=3`。

- [ ] **Step 1: 写失败测试**

`tests/test_strategy_limit_up_shakeout.py` 全文：

```python
import pandas as pd

from quant.strategies import get_strategy


def make_bars(raw_close, open_, close, low, vols):
    n = len(close)
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(n)],
        "open": open_,
        "high": [max(o, c) + 0.1 for o, c in zip(open_, close)],
        "low": low,
        "close": close,
        "raw_close": raw_close,
        "vol": vols,
        "amount": [1e5] * n,
    })


def test_limit_up_shakeout_true_on_pullback():
    raw_close = [10.0, 11.0, 10.4]
    close = [10.0, 11.0, 10.5]
    open_ = [10.0, 10.8, 11.0]
    low = [9.9, 10.9, 11.0]
    vols = [100, 100, 300]
    s = get_strategy("limit_up_shakeout")

    signals = s.generate_signals(make_bars(raw_close, open_, close, low, vols))

    assert list(signals) == [False, False, True]


def test_limit_up_shakeout_false_without_volume_spike():
    raw_close = [10.0, 11.0, 10.4]
    close = [10.0, 11.0, 10.5]
    open_ = [10.0, 10.8, 11.0]
    low = [9.9, 10.9, 11.0]
    vols = [100, 100, 150]
    s = get_strategy("limit_up_shakeout")
    assert not s.generate_signals(
        make_bars(raw_close, open_, close, low, vols)).any()


def test_limit_up_shakeout_false_when_support_breaks():
    raw_close = [10.0, 11.0, 10.4]
    close = [10.0, 11.0, 10.5]
    open_ = [10.0, 10.8, 11.0]
    low = [9.9, 10.9, 10.9]
    vols = [100, 100, 300]
    s = get_strategy("limit_up_shakeout")
    assert not s.generate_signals(
        make_bars(raw_close, open_, close, low, vols)).any()


def test_limit_up_shakeout_false_without_limit_up():
    raw_close = [10.0, 10.5, 10.0]
    close = [10.0, 10.5, 10.0]
    open_ = [9.8, 10.3, 10.4]
    low = [9.7, 10.2, 9.9]
    vols = [100, 100, 300]
    s = get_strategy("limit_up_shakeout")
    assert not s.generate_signals(
        make_bars(raw_close, open_, close, low, vols)).any()


def test_limit_up_shakeout_defaults_and_warmup():
    s = get_strategy("limit_up_shakeout")
    assert s.p.limit_pct == 0.095
    assert s.p.vol_ratio == 2.0
    assert s.warmup_days == 3
```

（第 1 个用例中 `low[2]=11.0 >= close[1]=11.0` 为含相等边界；`close[2]=10.5 < open[2]=11.0` 收阴。）

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_strategy_limit_up_shakeout.py -v`

Expected: FAIL（未注册策略）。

- [ ] **Step 3: 实现**

`src/quant/strategies/limit_up_shakeout.py` 全文：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class LimitUpShakeoutParams(BaseModel):
    limit_pct: float = 0.095
    vol_ratio: float = 2.0


@register_strategy("limit_up_shakeout")
class LimitUpShakeout(Strategy):
    """昨日涨停、今日放量收阴不破昨收（洗盘回踩）。"""

    Params = LimitUpShakeoutParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = 3

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        raw_close = bars["raw_close"]
        close, open_ = bars["close"], bars["open"]
        vol = bars["vol"]

        limit_up = raw_close.shift(1) >= raw_close.shift(2) * (1.0 + p.limit_pct)
        bearish = close < open_
        heavy = vol > p.vol_ratio * vol.shift(1)
        support = bars["low"] >= close.shift(1)

        return (limit_up & bearish & heavy & support).fillna(False)
```

`configs/strategies/limit_up_shakeout.yaml` 全文：

```yaml
strategy: limit_up_shakeout
params:
  limit_pct: 0.095
  vol_ratio: 2.0
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategy_limit_up_shakeout.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/strategies/limit_up_shakeout.py configs/strategies/limit_up_shakeout.yaml tests/test_strategy_limit_up_shakeout.py
git commit -m "feat: add limit_up_shakeout strategy"
```

---

### Task 7: UptrendLimitDown 新增

**Files:**
- Create: `src/quant/strategies/uptrend_limit_down.py`
- Create: `configs/strategies/uptrend_limit_down.yaml`
- Test: `tests/test_strategy_uptrend_limit_down.py`

**Interfaces:**
- Consumes: 后复权 `close/vol`、不复权 `raw_close`。
- Produces: 注册名 `uptrend_limit_down`；参数 `ma_short=20, ma_long=60, limit_pct=0.095, vol_ma=20, vol_ratio=2.0`；`warmup_days=61`。

- [ ] **Step 1: 写失败测试**

`tests/test_strategy_uptrend_limit_down.py` 全文：

```python
import pandas as pd

from quant.strategies import get_strategy

PARAMS = dict(ma_short=3, ma_long=5, limit_pct=0.095, vol_ma=3, vol_ratio=2.0)


def make_bars(closes, raw_closes, vols):
    n = len(closes)
    return pd.DataFrame({
        "trade_date": [f"202401{i + 1:02d}" for i in range(n)],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "raw_close": raw_closes,
        "vol": vols,
        "amount": [c * v for c, v in zip(closes, vols)],
    })


def test_uptrend_limit_down_true_on_oversold_dump():
    closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    raw_closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    vols = [100, 100, 100, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)

    signals = s.generate_signals(make_bars(closes, raw_closes, vols))

    assert list(signals) == [False] * 5 + [True]


def test_uptrend_limit_down_false_without_volume_spike():
    closes = [10.0, 10.2, 10.4, 10.6, 11.0, 9.9]
    vols = [100, 100, 100, 100, 100, 200]
    s = get_strategy("uptrend_limit_down", **PARAMS)
    assert not s.generate_signals(
        make_bars(closes, closes, vols)).any()


def test_uptrend_limit_down_false_without_uptrend():
    closes = [11.0, 10.8, 10.6, 10.4, 10.2, 9.0]
    vols = [100, 100, 100, 100, 100, 500]
    s = get_strategy("uptrend_limit_down", **PARAMS)
    assert not s.generate_signals(
        make_bars(closes, closes, vols)).any()


def test_uptrend_limit_down_defaults_and_warmup():
    s = get_strategy("uptrend_limit_down")
    assert s.p.ma_short == 20
    assert s.p.ma_long == 60
    assert s.p.limit_pct == 0.095
    assert s.p.vol_ma == 20
    assert s.p.vol_ratio == 2.0
    assert s.warmup_days == 61
```

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_strategy_uptrend_limit_down.py -v`

Expected: FAIL（未注册策略）。

- [ ] **Step 3: 实现**

`src/quant/strategies/uptrend_limit_down.py` 全文：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class UptrendLimitDownParams(BaseModel):
    ma_short: int = 20
    ma_long: int = 60
    limit_pct: float = 0.095
    vol_ma: int = 20
    vol_ratio: float = 2.0


@register_strategy("uptrend_limit_down")
class UptrendLimitDown(Strategy):
    """上升趋势中放量跌停的错杀机会。"""

    Params = UptrendLimitDownParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.ma_long + 1

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        close, raw_close, vol = bars["close"], bars["raw_close"], bars["vol"]

        ma_short = close.rolling(p.ma_short).mean()
        ma_long = close.rolling(p.ma_long).mean()
        uptrend = ma_short.shift(1) > ma_long.shift(1)
        limit_down = raw_close <= raw_close.shift(1) * (1.0 - p.limit_pct)
        heavy = vol > p.vol_ratio * vol.rolling(p.vol_ma).mean()

        return (uptrend & limit_down & heavy).fillna(False)
```

`configs/strategies/uptrend_limit_down.yaml` 全文：

```yaml
strategy: uptrend_limit_down
params:
  ma_short: 20
  ma_long: 60
  limit_pct: 0.095
  vol_ma: 20
  vol_ratio: 2.0
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategy_uptrend_limit_down.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/strategies/uptrend_limit_down.py configs/strategies/uptrend_limit_down.yaml tests/test_strategy_uptrend_limit_down.py
git commit -m "feat: add uptrend_limit_down strategy"
```

---

### Task 8: RpsBreakout 新增（prepare 横截面）

**Files:**
- Create: `src/quant/strategies/rps_breakout.py`
- Create: `configs/strategies/rps_breakout.yaml`
- Test: `tests/test_strategy_rps_breakout.py`

**Interfaces:**
- Consumes: Task 1 的 `Strategy.prepare`；`selection.compute_signals` 会先调用 `prepare`；行情列 `suspended`。
- Produces: 注册名 `rps_breakout`；参数 `window=120, rps_threshold=90.0, high_ratio=0.90, high_min_bars=60`；`warmup_days=121`；`prepare` 注入 `rps` 列；`rank` 返回 `rps`。

- [ ] **Step 1: 写失败测试**

`tests/test_strategy_rps_breakout.py` 全文：

```python
import pandas as pd

from quant.engine import selection
from quant.strategies import get_strategy

PARAMS = dict(window=3, rps_threshold=90.0, high_ratio=0.9, high_min_bars=2)

DATES = ["20240101", "20240102", "20240103", "20240104"]


def make_market(series, suspended=None):
    rows = []
    for code, closes in series.items():
        for i, (d, c) in enumerate(zip(DATES[-len(closes):], closes)):
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": c, "high": c, "low": c, "close": c,
                "raw_close": c, "vol": 100.0, "amount": 1000.0,
                "adj_factor": 1.0, "suspended": bool(suspended and code in suspended),
                "is_st": False, "is_new": False, "board": "main",
            })
    return pd.DataFrame(rows)


def test_prepare_adds_rps_and_does_not_mutate_input():
    market = make_market({"600000.SH": [1, 1, 1, 2]})
    s = get_strategy("rps_breakout", **PARAMS)

    enriched = s.prepare(market)

    assert "rps" not in market.columns
    assert enriched["rps"].iloc[-1] == 100.0


def test_rps_breakout_selects_top_percentile():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1, 1, 1, 1.5],
        "600002.SH": [1, 1, 1, 1.1],
        "600003.SH": [1, 1, 1, 0.9],
    })
    s = get_strategy("rps_breakout", **PARAMS)

    signals = selection.compute_signals(s, market)
    last = signals[signals["trade_date"] == "20240104"]
    hit = last[last["signal"]]["ts_code"].tolist()

    assert hit == ["600000.SH"]


def test_rps_breakout_excludes_suspended_from_ranking():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1, 1, 1, 1.5],
        "600009.SH": [1, 1, 1, 5.0],
    }, suspended={"600009.SH"})
    s = get_strategy("rps_breakout", **PARAMS)

    signals = selection.compute_signals(s, market)
    last = signals[signals["trade_date"] == "20240104"].set_index("ts_code")

    assert last.loc["600000.SH", "score"] == 100.0
    assert not last.loc["600009.SH", "signal"]


def test_rps_breakout_skips_insufficient_window():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1.0, 5.0],
    })
    s = get_strategy("rps_breakout", **PARAMS)

    enriched = s.prepare(market)
    short = enriched[enriched["ts_code"] == "600001.SH"]

    assert short["rps"].isna().all()
    signals = selection.compute_signals(s, market)
    assert not signals[signals["ts_code"] == "600001.SH"]["signal"].any()


def test_rps_breakout_no_lookahead_on_truncated_market():
    market = make_market({
        "600000.SH": [1, 1, 1, 2.0],
        "600001.SH": [1, 1, 1, 1.5],
        "600002.SH": [1, 1, 1, 1.1],
    })
    s = get_strategy("rps_breakout", **PARAMS)

    full = selection.compute_signals(s, market)
    truncated = selection.compute_signals(
        s, market[market["trade_date"] <= "20240103"])
    common = full[full["trade_date"] <= "20240103"].reset_index(drop=True)
    pd.testing.assert_series_equal(common["signal"], truncated["signal"])


def test_rps_breakout_defaults_and_warmup():
    s = get_strategy("rps_breakout")
    assert s.p.window == 120
    assert s.p.rps_threshold == 90.0
    assert s.p.high_ratio == 0.90
    assert s.p.high_min_bars == 60
    assert s.warmup_days == 121
```

- [ ] **Step 2: 运行确认 RED**

Run: `uv run pytest tests/test_strategy_rps_breakout.py -v`

Expected: FAIL（未注册策略）。

- [ ] **Step 3: 实现**

`src/quant/strategies/rps_breakout.py` 全文：

```python
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from quant.strategies.base import Strategy, register_strategy


class RpsBreakoutParams(BaseModel):
    window: int = 120
    rps_threshold: float = 90.0
    high_ratio: float = 0.90
    high_min_bars: int = 60


@register_strategy("rps_breakout")
class RpsBreakout(Strategy):
    """全市场 120 日相对强度前 10% 且接近 120 日高点的突破。"""

    Params = RpsBreakoutParams

    def __init__(self, **params):
        super().__init__(**params)
        self.warmup_days = self.p.window + 1

    def prepare(self, market: pd.DataFrame) -> pd.DataFrame:
        market = market.copy()
        prev_close = market.groupby("ts_code")["close"].shift(self.p.window)
        ret = market["close"] / prev_close - 1.0
        eligible = ret.notna() & ~market["suspended"].astype(bool)
        rps = ret.where(eligible).groupby(market["trade_date"]).rank(pct=True) * 100.0
        market["rps"] = rps
        return market

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        p = self.p
        high_max = bars["high"].rolling(p.window, min_periods=p.high_min_bars).max()
        signal = (bars["rps"] >= p.rps_threshold) & (bars["close"] >= high_max * p.high_ratio)
        return signal.fillna(False)

    def rank(self, bars: pd.DataFrame) -> pd.Series | None:
        return pd.to_numeric(bars["rps"], errors="coerce")
```

`configs/strategies/rps_breakout.yaml` 全文：

```yaml
strategy: rps_breakout
params:
  window: 120
  rps_threshold: 90
  high_ratio: 0.9
  high_min_bars: 60
```

- [ ] **Step 4: 运行确认 GREEN**

Run: `uv run pytest tests/test_strategy_rps_breakout.py tests/test_selection.py -v`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```powershell
git add src/quant/strategies/rps_breakout.py configs/strategies/rps_breakout.yaml tests/test_strategy_rps_breakout.py
git commit -m "feat: add rps_breakout strategy with cross-sectional ranking"
```

---

### Task 9: 文档更新

**Files:**
- Modify: `docs/my-strategies.md:47-52`（删除流通市值公式）
- Modify: `README.md`（策略清单与默认选股范围）

**Interfaces:**
- Consumes: 前 8 个任务的最终策略名与默认参数。
- Produces: 文档与代码一致。

- [ ] **Step 1: 修改 `docs/my-strategies.md`**

把第 47-52 行的排序说明：

```
**排序**：按流通市值从大到小。流通市值按以下口径计算（使用不复权数据）：

```
流通股本 = volume / (换手率% / 100)
流通市值 = 流通股本 × 不复权收盘价
```
```

替换为：

```
**排序**：按流通市值从大到小（取 `daily_basic.circ_mv`，单位万元）。
```

- [ ] **Step 2: 修改 `README.md` 的"策略参数"节**

在 `## 策略参数` 标题后、"`configs/strategies/<策略名>.yaml`" 之前插入：

```markdown
内置策略（默认只选沪深主板，非ST、非次新、非停牌）：

| 策略 | 一句话逻辑 | 排序 |
|---|---|---|
| `ma_volume` | 5 日均线上穿 20 日均线 + 放量 | 成交额 |
| `turtle_trade` | 20 日新高 + 成交额过亿 + 阳线真涨 | 流通市值 |
| `high_tight_flag` | 强动量后高位窄幅缩量整理 | 成交额 |
| `limit_up_shakeout` | 昨日涨停、今日放量收阴不破昨收 | 成交额 |
| `uptrend_limit_down` | 上升趋势中放量跌停（错杀） | 成交额 |
| `rps_breakout` | 120 日 RPS≥90 且接近 120 日高点 | RPS |
```

并把"常用命令"代码块中的选股示例改为：

```bash
uv run quant select -s ma_volume -n 20           # 选股（默认最新交易日、默认只选主板）
uv run quant select -s ma_volume --boards all    # 不限板块（main,gem,star,bse 可逗号组合）
```

- [ ] **Step 3: 校验**

Run: `Select-String -Path "docs\my-strategies.md" -Pattern "换手率"` 与 `Select-String -Path "README.md" -Pattern "rps_breakout"`

Expected: 第一个无输出（公式已删）；第二个命中新表格行。

- [ ] **Step 4: 提交**

```powershell
git add docs/my-strategies.md README.md
git commit -m "docs: update strategy list and circ_mv ranking note"
```

---

### Task 10: 全量回归与真实验收

**Files:**
- 无代码改动（如发现问题则按对应任务流程修复并提交）。

**Interfaces:**
- Consumes: 全部前序任务；真实 MySQL/Parquet 缓存已就绪（水位线 2026-09-18）。
- Produces: 验收结论（选股 CSV 与回测指标汇总）。

- [ ] **Step 1: 全量单元测试**

Run: `uv run pytest -q`

Expected: 全部 PASS（含更新后的 5 个策略测试文件与新增 4 个），无失败。

- [ ] **Step 2: 6 个策略真实选股**

依次执行（每个约 10~60 秒，`select` 默认最新交易日、默认主板）：

```powershell
uv run quant select -s ma_volume -n 20
uv run quant select -s turtle_trade -n 20
uv run quant select -s high_tight_flag -n 20
uv run quant select -s limit_up_shakeout -n 20
uv run quant select -s uptrend_limit_down -n 20
uv run quant select -s rps_breakout -n 20
```

记录每个策略输出的行数；若某策略当天空结果，用 `--date 2026-09-17`（或更早交易日）复核一次并如实记录。并核对选出的 `ts_code` 均以 `60`/`00` 开头。

- [ ] **Step 3: 6 个策略真实回测**

依次执行（每个约 1~3 分钟）：

```powershell
uv run quant backtest -s ma_volume --start 2021-01-01 --end 2026-09-18
uv run quant backtest -s turtle_trade --start 2021-01-01 --end 2026-09-18
uv run quant backtest -s high_tight_flag --start 2021-01-01 --end 2026-09-18
uv run quant backtest -s limit_up_shakeout --start 2021-01-01 --end 2026-09-18
uv run quant backtest -s uptrend_limit_down --start 2021-01-01 --end 2026-09-18
uv run quant backtest -s rps_breakout --start 2021-01-01 --end 2026-09-18
```

记录每个策略的成交笔数、总收益、最大回撤（`outputs/backtest/<策略>_<时间戳>/metrics.json`）。

- [ ] **Step 4: 汇报**

向用户汇总：每个策略选股行数（或空结果说明）、回测成交笔数与核心指标、异常与偏差。不提交代码；若验收发现问题，回到对应任务修复后重跑。

---

## Self-Review 结果

- **Spec 覆盖**：spec §4（prepare/boards/排序分）→ Task 1-2；§5 价格口径 → 各策略任务内部实现；§6 六个策略 → Task 3-8；§8 配置与文档 → 各策略任务 YAML + Task 9；§9 测试计划 → 各任务测试 + Task 10 Step 1（防未来函数用例在 Task 8 的 truncation 测试）；§10 验收 → Task 10。无遗漏。
- **占位符扫描**：无 TODO/TBD；所有测试与实现均为完整代码。
- **类型一致性**：`prepare` 签名、`filters_from_config` 返回 tuple、策略参数名在测试/YAML/实现中一致；`RpsBreakout.prepare` 注入列名 `rps` 与 `generate_signals`/`rank` 使用一致。
