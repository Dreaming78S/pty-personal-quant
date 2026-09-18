import typer

app = typer.Typer(help="A股量化选股与回测系统", no_args_is_help=True)
data_app = typer.Typer(help="数据管理", no_args_is_help=True)
app.add_typer(data_app, name="data")


@data_app.command("init-db")
def data_init_db() -> None:
    """在 MySQL 中创建全部数据表（幂等）。"""
    from quant.data import schemas

    names = schemas.create_all()
    typer.echo(f"已创建/确认 {len(names)} 张表：{', '.join(names)}")


@data_app.command("update")
def data_update(
    table: str = typer.Option(None, "--table", "-t", help="只更新指定表，缺省全部"),
    from_date: str = typer.Option(None, "--from-date", help="起始日期，如 2024-01-02"),
    to_date: str = typer.Option(None, "--to-date", help="结束日期，如 2024-01-31"),
) -> None:
    """从 Tushare 增量入库（可中断续跑）。"""
    from quant.data import ingest
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    start = to_yyyymmdd(from_date) if from_date else None
    end = to_yyyymmdd(to_date) if to_date else None
    if table:
        results = {table: ingest.update(table, from_date=start, to_date=end)}
    else:
        results = ingest.update_all(from_date=start, to_date=end)
    for name, rows in results.items():
        typer.echo(f"{name}: 已写入 {rows} 行")


@data_app.command("status")
def data_status() -> None:
    """显示各表行数、水位线与本地缓存状态。"""
    import pandas as pd

    from quant.data import cache, db, schemas

    try:
        db.scalar("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 连接失败时给出明确提示
        typer.echo(f"数据库连接失败：{exc}")
        raise typer.Exit(code=1)

    rows = []
    for name in schemas.TABLES:
        try:
            count = db.scalar(f"SELECT COUNT(*) FROM `{name}`")
        except Exception:  # noqa: BLE001 - 表不存在时提示即可
            count = "表不存在"
        if name == "ingest_log":
            watermark = "-"
        else:
            try:
                watermark = db.scalar(
                    "SELECT last_trade_date FROM ingest_log WHERE task_name=%s", (name,)
                ) or "-"
            except Exception:  # noqa: BLE001 - 水位线表缺失时留空
                watermark = "-"
        rows.append({
            "表": name,
            "行数": count,
            "水位线": watermark,
            "缓存": "有" if cache.cache_path(name).exists() else "无",
        })
    typer.echo(pd.DataFrame(rows).to_string(index=False))


@app.command("list")
def list_strategies_cmd() -> None:
    """列出已注册策略及其默认参数。"""
    from quant.strategies.base import list_strategies

    for name, cls in sorted(list_strategies().items()):
        schema = cls.Params.model_json_schema().get("properties", {})
        params = "，".join(
            f"{key}={spec.get('default', '?')}" for key, spec in schema.items()
        ) or "无参数"
        typer.echo(f"{name}: {params}")


@app.command("select")
def select(
    strategy: str = typer.Option(..., "--strategy", "-s", help="策略名"),
    date: str = typer.Option(None, "--date", "-d", help="交易日，缺省最新；非交易日自动回退"),
    top: int = typer.Option(20, "--top", "-n", help="输出数量"),
    rank_by: str = typer.Option("amount", "--rank-by", help="排序字段"),
) -> None:
    """按策略筛选个股并输出 CSV。"""
    from pathlib import Path

    from quant.data import cache
    from quant.engine import loader, selection, report
    from quant.strategies.base import get_strategy, load_strategy_config
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    params: dict = {}
    config_path = Path("configs/strategies") / f"{strategy}.yaml"
    if config_path.exists():
        _, params = load_strategy_config(config_path)
    strat = get_strategy(strategy, **params)

    cache.ensure_all()
    target = loader.resolve_trade_date(to_yyyymmdd(date) if date else None)
    market = loader.load_market_data(
        start=target, end=target, warmup_days=strat.warmup_days)
    result = selection.run_selection(strat, target, top_n=top, market=market,
                                     rank_by=rank_by)
    typer.echo(result.to_string(index=False))
    path = report.save_selection(result, target, strategy)
    typer.echo(f"已保存：{path}")


@app.command("backtest")
def backtest_cmd(
    strategy: str = typer.Option(..., "--strategy", "-s", help="策略名"),
    start: str = typer.Option(..., "--start", help="开始日期"),
    end: str = typer.Option(..., "--end", help="结束日期"),
    config: str = typer.Option("configs/backtest/default.yaml", "--config",
                               help="回测参数 YAML"),
    top: int = typer.Option(None, "--top", "-n", help="持仓数量，覆盖配置"),
) -> None:
    """按策略回测并生成报告。"""
    from pathlib import Path

    import yaml

    from quant.engine import backtest as bt
    from quant.engine import loader, report
    from quant.engine.metrics import compute_metrics
    from quant.engine.rules import FeeConfig
    from quant.strategies.base import get_strategy, load_strategy_config
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    start_date, end_date = to_yyyymmdd(start), to_yyyymmdd(end)

    strategy_params: dict = {}
    strategy_config = Path("configs/strategies") / f"{strategy}.yaml"
    if strategy_config.exists():
        _, strategy_params = load_strategy_config(strategy_config)
    strat = get_strategy(strategy, **strategy_params)

    raw: dict = {}
    config_path = Path(config)
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw.update(start=start_date, end=end_date, warmup_days=strat.warmup_days)
    if top is not None:
        raw["top_n"] = top
    if isinstance(raw.get("fees"), dict):
        raw["fees"] = FeeConfig(**raw["fees"])
    backtest_config = bt.BacktestConfig(**raw)

    market = loader.load_market_data(
        backtest_config.start, backtest_config.end,
        warmup_days=strat.warmup_days,
        filters=bt.filters_from_config(backtest_config), ensure=True)
    try:
        benchmark = loader.load_benchmark(backtest_config.benchmark,
                                          backtest_config.start, backtest_config.end)
    except ValueError as exc:
        typer.echo(f"提示：{exc}，将跳过基准对比")
        benchmark = None

    result = bt.run_backtest(strat, backtest_config, market=market, benchmark=benchmark)
    metric_values = compute_metrics(result.equity, result.trades,
                                    backtest_config.risk_free_rate)
    folder = report.save_backtest(result, metric_values)

    if result.trades.empty:
        typer.echo("回测区间内没有成交")
    else:
        typer.echo(f"成交笔数：{len(result.trades)}")
    typer.echo("指标：")
    for key, value in metric_values.items():
        typer.echo(f"  {key}: {value:.4f}" if isinstance(value, float) else f"  {key}: {value}")
    typer.echo(f"报告目录：{folder}")
