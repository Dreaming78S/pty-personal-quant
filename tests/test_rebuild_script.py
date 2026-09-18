import importlib.util
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_data.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("rebuild_data_script",
                                                  SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_script(monkeypatch, script, results):
    state = {"truncated": [], "created": 0, "migrated": 0,
             "rebuild_called": 0, "rate": None, "order": [],
             "rebuild_kwargs": {}}

    def note(event):
        state["order"].append(event)

    def fake_create_all():
        note("create_all")
        state["created"] += 1
        return []

    def fake_migrate():
        note("migrate")
        state["migrated"] += 1
        return []

    def fake_truncate(names=None, all_tables=False):
        note("truncate")
        state["truncated"].append(list(names) if names is not None else None)
        return list(names or [])

    def fake_rebuild(**kwargs):
        note("rebuild")
        state["rebuild_called"] += 1
        state["rebuild_kwargs"] = kwargs
        return results

    def fake_client(calls_per_minute=150):
        note("client")
        state["rate"] = calls_per_minute
        return object()

    monkeypatch.setattr(script, "setup_logging", lambda: None)
    monkeypatch.setattr(script.maintenance, "tables_to_truncate",
                        lambda all_tables=False: ["daily", "ingest_log"])
    monkeypatch.setattr(script.schemas, "create_all", fake_create_all)
    monkeypatch.setattr(script.schemas, "migrate", fake_migrate)
    monkeypatch.setattr(script.maintenance, "truncate_tables", fake_truncate)
    monkeypatch.setattr(script.maintenance, "rebuild", fake_rebuild)
    monkeypatch.setattr(script, "TushareClient", fake_client)
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
    assert state["truncated"] == [["daily", "ingest_log"]]
    assert state["created"] == 1
    assert state["migrated"] == 1
    assert state["rebuild_kwargs"]["resume"] is False
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
    assert state["rebuild_kwargs"]["resume"] is True


def test_main_orders_create_migrate_before_truncate(monkeypatch):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    script.main(["--yes"])

    order = state["order"]
    assert order.index("client") < order.index("create_all")
    assert order.index("create_all") < order.index("migrate")
    assert order.index("migrate") < order.index("truncate")
    assert order.index("truncate") < order.index("rebuild")


def test_main_skip_truncate_without_yes_skips_prompt_and_wipe(monkeypatch):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    def fail_input(prompt=""):
        raise AssertionError("--skip-truncate 不应弹出确认")

    monkeypatch.setattr("builtins.input", fail_input)

    code = script.main(["--skip-truncate"])

    assert code == 0
    assert state["truncated"] == []
    assert state["rebuild_kwargs"]["resume"] is True


def test_main_rejects_invalid_market_start_before_wiping(monkeypatch, capsys):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    code = script.main(["--yes", "--market-start", "2018-1-1"])

    assert code == 2
    assert state["truncated"] == []
    assert state["created"] == 0
    assert state["rebuild_called"] == 0
    assert "参数错误" in capsys.readouterr().out


def test_main_rejects_invalid_holdertrade_start_before_wiping(monkeypatch, capsys):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    code = script.main(["--yes", "--holdertrade-start", "2019/01/01"])

    assert code == 2
    assert state["truncated"] == []
    assert "参数错误" in capsys.readouterr().out


def test_main_rejects_rate_below_one_before_wiping(monkeypatch, capsys):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    code = script.main(["--yes", "--rate", "0"])

    assert code == 2
    assert state["rate"] is None
    assert state["truncated"] == []
    assert "参数错误" in capsys.readouterr().out


def test_main_client_failure_happens_before_wiping(monkeypatch):
    script = _load_script()
    state = _stub_script(monkeypatch, script, {"daily": 1})

    def broken_client(calls_per_minute=150):
        raise RuntimeError("缺少 tushare_token")

    monkeypatch.setattr(script, "TushareClient", broken_client)

    with pytest.raises(RuntimeError, match="tushare_token"):
        script.main(["--yes"])

    assert state["truncated"] == []
    assert state["created"] == 0


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
