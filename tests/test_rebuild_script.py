import importlib.util
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_data.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rebuild_data_script",
                                                  SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_script(monkeypatch, script, results):
    state = {"truncated": [], "created": 0, "rebuild_called": 0, "rate": None}
    monkeypatch.setattr(script, "setup_logging", lambda: None)
    monkeypatch.setattr(script.maintenance, "tables_to_truncate",
                        lambda all_tables=False: ["daily", "ingest_log"])
    monkeypatch.setattr(script.schemas, "create_all",
                        lambda: state.update(created=state["created"] + 1) or [])
    monkeypatch.setattr(script.schemas, "migrate", lambda: [])
    monkeypatch.setattr(script.maintenance, "truncate_tables",
                        lambda all_tables=False: state["truncated"].append(
                            all_tables) or [])
    monkeypatch.setattr(script.maintenance, "rebuild",
                        lambda **kwargs: state.update(
                            rebuild_called=state["rebuild_called"] + 1) or results)
    monkeypatch.setattr(script, "TushareClient",
                        lambda calls_per_minute=150: state.update(
                            rate=calls_per_minute) or object())
    monkeypatch.setattr(script.db, "scalar", lambda sql, params=None: 0)
    return state


def test_parse_args_defaults():
    script = _load_script()

    args = script.parse_args([])

    assert args.market_start == "2018-01-01"
    assert args.holdertrade_start is None
    assert args.all_tables is False
    assert args.skip_truncate is False
    assert args.yes is False
    assert args.rate == 150


def test_parse_args_flags():
    script = _load_script()

    args = script.parse_args(["--market-start", "2020-01-01",
                              "--holdertrade-start", "2019-01-01",
                              "--all-tables", "--skip-truncate", "--yes",
                              "--rate", "90"])

    assert args.market_start == "2020-01-01"
    assert args.holdertrade_start == "2019-01-01"
    assert args.all_tables is True
    assert args.skip_truncate is True
    assert args.yes is True
    assert args.rate == 90


def test_confirm_requires_exact_yes(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    assert script.confirm(["daily", "ingest_log"], all_tables=False) is False
    out = capsys.readouterr().out
    assert "daily" in out
    assert "ingest_log" in out


def test_confirm_accepts_yes_and_warns_for_all_tables(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")

    assert script.confirm(["daily", "legacy_notes"], all_tables=True) is True
    assert "--all-tables" in capsys.readouterr().out


def test_main_yes_runs_rebuild_and_returns_zero(monkeypatch, capsys):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 10, "ingest_log": 1})

    code = script.main(["--yes"])

    assert code == 0
    assert state["truncated"] == [False]
    assert state["created"] == 1
    out = capsys.readouterr().out
    assert "总耗时" in out
    assert "daily" in out


def test_main_returns_one_when_a_table_failed(monkeypatch, capsys):
    script = _load_script()
    _stub_script(monkeypatch, script, {"daily": "失败: 接口超时"})

    assert script.main(["--yes"]) == 1
    assert "失败: 接口超时" in capsys.readouterr().out


def test_main_skip_truncate_does_not_wipe(monkeypatch):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    script.main(["--yes", "--skip-truncate"])

    assert state["truncated"] == []
    assert state["rebuild_called"] == 1


def test_main_declined_confirmation_cancels(monkeypatch, capsys):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")

    code = script.main([])

    assert code == 0
    assert state["truncated"] == []
    assert state["rebuild_called"] == 0
    assert "已取消" in capsys.readouterr().out


def test_main_passes_rate_to_client(monkeypatch):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    script.main(["--yes", "--rate", "90"])

    assert state["rate"] == 90
