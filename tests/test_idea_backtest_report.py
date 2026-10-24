from quant.engine.idea_backtest import run_signature


def test_run_signature_encodes_non_default_parameters():
    assert run_signature(
        hold=1, t1_min_count=2, t1_strategies=("ma_volume", "rps_breakout"),
        t2_min_count=None, t2_strategies=(), cum_min_count=1,
        cum_strategies=(), mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge2-ma_volume+rps_breakout_cumge1"

    assert run_signature(
        hold=3, t1_min_count=2, t1_strategies=(),
        t2_min_count=1, t2_strategies=("rps_breakout",), cum_min_count=3,
        cum_strategies=("turtle_trade",), mv_min=50.0, mv_max=5000.0,
    ) == "n3_t1ge2_t2ge1-rps_breakout_cumge3-turtle_trade_mv50-5000"

    assert run_signature(
        hold=1, t1_min_count=1, t1_strategies=(),
        t2_min_count=None, t2_strategies=("turtle_trade",), cum_min_count=1,
        cum_strategies=(), mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge1_t2geany-turtle_trade_cumge1"

    assert run_signature(
        hold=1, t1_min_count=2, t1_strategies=(),
        t2_min_count=1, t2_max_count=1, t2_strategies=("rps_breakout",),
        cum_min_count=2, cum_max_count=2, cum_strategies=(),
        mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge2_t2ge1-rps_breakout_t2le1_cumge2_cumle2"

    assert run_signature(
        hold=1, t1_min_count=1, t1_strategies=(),
        t2_min_count=None, t2_max_count=1, t2_strategies=(),
        cum_min_count=1, cum_max_count=1, cum_strategies=(),
        mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge1_t2le1_cumge1_cumle1"

    assert run_signature(
        hold=1, t1_min_count=2, t1_strategies=("rps_breakout",),
        t2_min_count=1, t2_max_count=1, t2_strategies=(),
        cum_min_count=None, cum_max_count=None, cum_strategies=(),
        mv_min=0.0, mv_max=50000.0,
    ) == "n1_t1ge2-rps_breakout_t2ge1_t2le1_cumgeany"
