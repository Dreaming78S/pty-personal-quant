import datetime
import os

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION"),
                       reason="需要 RUN_INTEGRATION=1 才运行集成测试"),
]


def test_mysql_core_tables_reachable():
    from quant.data import db

    count = db.scalar("SELECT COUNT(*) FROM trade_cal")
    assert count and count > 0


def test_tushare_fetch_trade_cal():
    from quant.data.tushare_client import TushareClient

    df = TushareClient().fetch_trade_cal("20240101", "20240110")
    assert not df.empty


def test_daily_ingest_then_select_and_backtest_smoke():
    """gated 冒烟：入库最近 3 个交易日 daily，再程序化跑选股与回测。

    手动命令：$env:RUN_INTEGRATION=1; uv run pytest -m integration
    真实数据经 MySQL -> parquet -> loader，覆盖 Decimal 行情路径。
    """
    from quant.data import cache, db, ingest
    from quant.engine import backtest as bt
    from quant.engine import loader, selection
    from quant.strategies.base import get_strategy

    today = datetime.date.today().strftime("%Y%m%d")
    try:
        rows = db.read_df(
            "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 "
            "AND cal_date<=%s ORDER BY cal_date DESC LIMIT 3", (today,))
    except Exception as exc:  # noqa: BLE001 - 集成环境不可用时跳过
        pytest.skip(f"MySQL 不可用：{exc}")
    if rows.empty:
        pytest.skip("trade_cal 中没有近期交易日，请先运行 quant data update")
    dates = sorted(rows["cal_date"].astype(str).tolist())

    try:
        ingest.update("daily", from_date=dates[0], to_date=dates[-1])
    except Exception as exc:  # noqa: BLE001 - 无 token/权限时跳过
        pytest.skip(f"Tushare 拉取 daily 失败：{exc}")

    try:
        cache.ensure_all()
    except Exception as exc:  # noqa: BLE001 - 其余表数据缺失时跳过
        pytest.skip(f"缓存刷新失败：{exc}")

    target = dates[-1]
    strategy = get_strategy("ma_volume")
    market = loader.load_market_data(start=target, end=target,
                                     warmup_days=strategy.warmup_days)
    if market.empty:
        pytest.skip(f"{target} 无可用行情")

    picked = selection.run_selection(strategy, target, top_n=5, market=market)
    assert list(picked.columns) == ["rank", "ts_code", "name", "trade_date",
                                    "close", "raw_close", "amount", "score"]

    config = bt.BacktestConfig(start=dates[0], end=dates[-1],
                               initial_cash=100_000.0, rebalance="daily",
                               top_n=3, min_list_days=0)
    history = loader.load_market_data(
        config.start, config.end, warmup_days=strategy.warmup_days,
        filters=bt.filters_from_config(config))
    result = bt.run_backtest(strategy, config, market=history)
    assert result.equity["trade_date"].tolist() == dates
    assert (result.equity["cash"] >= 0).all()
