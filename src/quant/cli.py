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
