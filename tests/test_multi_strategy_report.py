from quant.engine.hit_performance import report_prefix


def test_report_prefix_encodes_threshold_and_new_only():
    assert report_prefix(2, False) == "multi_strategy"
    assert report_prefix(3, False) == "multi_strategy_3plus"
    assert report_prefix(2, True) == "multi_strategy_new"
    assert report_prefix(3, True) == "multi_strategy_3plus_new"
