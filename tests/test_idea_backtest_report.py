import importlib.util
from pathlib import Path

SCRIPT_PATH = (Path(__file__).resolve().parents[1] / "scripts"
               / "idea_backtest.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("idea_backtest_script",
                                                  SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_signature_encodes_non_default_parameters():
    script = _load_script()

    assert script._run_signature(
        hold=1, t1_min_count=2, t1_strategies=("ma_volume", "rps_breakout"),
        t2_min_count=None, t2_strategies=(), cum_min_count=1,
        cum_strategies=(), mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge2-ma_volume+rps_breakout_cumge1"

    assert script._run_signature(
        hold=3, t1_min_count=2, t1_strategies=(),
        t2_min_count=1, t2_strategies=("rps_breakout",), cum_min_count=3,
        cum_strategies=("turtle_trade",), mv_min=50.0, mv_max=5000.0,
    ) == "n3_t1ge2_t2ge1-rps_breakout_cumge3-turtle_trade_mv50-5000"

    assert script._run_signature(
        hold=1, t1_min_count=1, t1_strategies=(),
        t2_min_count=None, t2_strategies=("turtle_trade",), cum_min_count=1,
        cum_strategies=(), mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge1_t2geany-turtle_trade_cumge1"

    assert script._run_signature(
        hold=1, t1_min_count=2, t1_strategies=(),
        t2_min_count=1, t2_max_count=1, t2_strategies=("rps_breakout",),
        cum_min_count=2, cum_max_count=2, cum_strategies=(),
        mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge2_t2ge1-rps_breakout_t2le1_cumge2_cumle2"

    assert script._run_signature(
        hold=1, t1_min_count=1, t1_strategies=(),
        t2_min_count=None, t2_max_count=1, t2_strategies=(),
        cum_min_count=1, cum_max_count=1, cum_strategies=(),
        mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge1_t2le1_cumge1_cumle1"
