import typer

app = typer.Typer(help="A股量化选股与回测系统", no_args_is_help=True)
data_app = typer.Typer(help="数据管理", no_args_is_help=True)
hits_app = typer.Typer(help="策略历史命中", no_args_is_help=True)
bot_app = typer.Typer(help="飞书问数机器人", no_args_is_help=True)
app.add_typer(data_app, name="data")
app.add_typer(hits_app, name="hits")
app.add_typer(bot_app, name="bot")


@data_app.command("init-db")
def data_init_db() -> None:
    """在 MySQL 中创建全部数据表（含 hit_<策略>）并补齐已有表的缺失列（幂等）。"""
    from quant.data import schemas

    names = schemas.create_all() + schemas.create_hit_tables()
    typer.echo(f"已创建/确认 {len(names)} 张表：{', '.join(names)}")
    migrated = schemas.migrate() + schemas.migrate_hit_columns()
    if migrated:
        typer.echo(f"已补齐 {len(migrated)} 个字段：{', '.join(migrated)}")


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
    for name in [*schemas.TABLES, *schemas.HIT_TABLES]:
        is_hit = name in schemas.HIT_TABLES
        task = name
        try:
            count = db.scalar(f"SELECT COUNT(*) FROM `{name}`")
        except Exception:  # noqa: BLE001 - 表不存在时提示即可
            count = "表不存在"
        if name == "ingest_log":
            watermark = "-"
        else:
            try:
                watermark = db.scalar(
                    "SELECT last_trade_date FROM ingest_log WHERE task_name=%s", (task,)
                ) or "-"
            except Exception:  # noqa: BLE001 - 水位线表缺失时留空
                watermark = "-"
        rows.append({
            "表": name,
            "行数": count,
            "水位线": watermark,
            "缓存": "-" if is_hit else ("有" if cache.cache_path(name).exists() else "无"),
        })
    typer.echo(pd.DataFrame(rows).to_string(index=False))


@hits_app.command("update")
def hits_update(
    strategy: str = typer.Option("all", "--strategy", "-s",
                                 help="all、策略名或逗号组合"),
    from_date: str = typer.Option(None, "--from-date",
                                  help="起始日期（指定时先清空区间再重算），缺省从水位线或 2024-01-01 续跑"),
    to_date: str = typer.Option(None, "--to-date", help="结束日期，缺省最新交易日"),
) -> None:
    """按策略回填/增量写入历史命中表 hit_<策略>。"""
    from quant.engine import hits
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    results = hits.fill_hits(
        strategy,
        from_date=to_yyyymmdd(from_date) if from_date else None,
        to_date=to_yyyymmdd(to_date) if to_date else None,
    )
    for name, rows in results.items():
        typer.echo(f"hit_{name}: 已写入 {rows} 行")


@app.command("notify")
def notify_cmd(
    strategy: str = typer.Option(None, "--strategy", "-s",
                                 help="缺省=今日最终推荐+多策略共振；all=全部策略卡；策略名/逗号组合"),
    date: str = typer.Option(None, "--date", "-d",
                             help="交易日，缺省最新；非交易日自动回退"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印消息不发送"),
    co: bool = typer.Option(True, "--co/--no-co",
                            help="是否附带多策略共振卡片（仅 --strategy 有效）"),
) -> None:
    """推送飞书卡片。缺省只发两张卡（今日最终推荐 + 多策略共振），不再逐策略刷屏。"""
    from quant.engine import notify
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    target = to_yyyymmdd(date) if date else None
    if dry_run:
        messages = (notify.build_default_messages(target)
                    if strategy in (None, "")
                    else notify.build_messages(strategy, target, include_co=co))
        for name, message in messages.items():
            typer.echo(f"----- {name} -----")
            typer.echo(notify.render_text(message))
        return
    results = notify.notify_hits(strategy, target, include_co=co)
    failed = False
    for name, status in results.items():
        typer.echo(f"{name}: {status}")
        failed = failed or status.startswith("失败")
    if failed:
        raise typer.Exit(code=1)


@app.command("recommend")
def recommend_cmd(
    date: str = typer.Option(None, "--date", "-d",
                             help="交易日，缺省最新；非交易日自动回退"),
) -> None:
    """显示当日最终推荐（环境闸门 + 分层），只读不推送。"""
    import pandas as pd

    from quant.engine import recommend as rec
    from quant.utils.dates import to_yyyymmdd
    from quant.utils.logging import setup_logging

    setup_logging()
    target = to_yyyymmdd(date) if date else None
    try:
        result = rec.build_recommendations(target)
    except ValueError as exc:
        typer.echo(f"无法生成推荐：{exc}")
        raise typer.Exit(code=1) from exc

    index_name = rec.INDEX_NAMES_ZH.get(rec.load_config().gate_index,
                                        rec.load_config().gate_index)
    if result.gate_open:
        typer.echo(
            f"环境开：{index_name} 收盘 {result.index_close:.2f} ≥ "
            f"{rec.load_config().gate_ma_days} 日均线 {result.index_ma:.2f}"
        )
    else:
        typer.echo(
            f"环境关：{index_name} 收盘 {result.index_close:.2f} < "
            f"{rec.load_config().gate_ma_days} 日均线 {result.index_ma:.2f}"
        )

    rows = []
    for pick in (*result.primary, *result.secondary):
        detail = "、".join(f"{label}({rank})" for label, rank in pick.hits)
        rows.append({
            "档位": pick.tier,
            "名称": pick.name,
            "代码": pick.ts_code,
            "行业": pick.industry,
            "收盘": pick.raw_close,
            "成交额千": pick.amount,
            "共振数": pick.co_count,
            "理由": detail,
        })
    if rows:
        typer.echo(pd.DataFrame(rows).to_string(index=False))
        typer.echo("纪律：T+1 开盘买入，T+2 收盘卖出（最多持有到 T+3）")
    else:
        typer.echo("今日无符合推荐条件的股票。")


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


VALID_BOARDS = ("main", "gem", "star", "bse")


def _parse_boards(value: str) -> tuple[str, ...] | None:
    value = value.strip().lower()
    if value in ("", "all"):
        return None
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    if not parts:
        return None
    if "all" in parts:
        raise typer.BadParameter(
            f"all 不能与其他板块名混用（收到：{value}）；"
            "如需不限板块请单独使用 --boards all")
    unknown = [part for part in parts if part not in VALID_BOARDS]
    if unknown:
        raise typer.BadParameter(
            f"未知板块：{'、'.join(unknown)}；"
            f"可选值为 {'、'.join(VALID_BOARDS)}，或用 all 表示不限")
    return parts


@app.command("select")
def select(
    strategy: str = typer.Option(..., "--strategy", "-s", help="策略名"),
    date: str = typer.Option(None, "--date", "-d", help="交易日，缺省最新；非交易日自动回退"),
    top: int = typer.Option(0, "--top", "-n", help="输出数量，0 表示全部命中"),
    rank_by: str = typer.Option("amount", "--rank-by", help="排序字段"),
    boards: str = typer.Option("main", "--boards",
                               help="板块过滤，逗号分隔 main/gem/star/bse；all 表示不限"),
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
    result = selection.run_selection(
        strat, target, top_n=top if top > 0 else None, market=market,
        filters=loader.UniverseFilters(allowed_boards=_parse_boards(boards)),
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
    top: int = typer.Option(None, "--top", "-n",
                            help="持仓数量，0 表示全部信号股（覆盖配置）"),
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
        raw["top_n"] = top if top > 0 else None
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
    except (ValueError, FileNotFoundError) as exc:
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


@bot_app.command("serve")
def bot_serve() -> None:
    """启动飞书长连接问数机器人（前台常驻，Ctrl+C 退出）。"""
    from quant.bot import feishu
    from quant.utils.logging import setup_logging

    setup_logging()
    try:
        feishu.run_bot()
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc


@bot_app.command("ask")
def bot_ask(question: str = typer.Argument(..., help="自然语言问题")) -> None:
    """本地跑一遍问答链路（不连飞书），打印回答与 SQL。"""
    from quant.bot import qa
    from quant.bot.llm import DeepSeekClient
    from quant.bot.runner import run_readonly
    from quant.config import get_settings
    from quant.utils.logging import setup_logging

    setup_logging()
    settings = get_settings()
    if not settings.deepseek_api_key:
        typer.echo("未配置 DEEPSEEK_API_KEY（.env）")
        raise typer.Exit(code=1)
    llm = DeepSeekClient(settings.deepseek_api_key, settings.deepseek_base_url,
                         settings.deepseek_model)
    result = qa.answer(question, llm, run_readonly)
    typer.echo(result.text)
    if result.sql:
        typer.echo(f"\nSQL：{result.sql}")
    typer.echo(f"返回 {result.row_count} 行（{'成功' if result.ok else '失败'}）")
    if not result.ok:
        raise typer.Exit(code=1)
