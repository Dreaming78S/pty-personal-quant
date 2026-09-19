import importlib.util
from pathlib import Path

SCRIPT_PATH = (Path(__file__).resolve().parents[1] / "scripts"
               / "multi_strategy_performance_report.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("multi_strategy_report_script",
                                                  SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_prefix_encodes_threshold_and_new_only():
    script = _load_script()

    assert script._report_prefix(2, False) == "multi_strategy"
    assert script._report_prefix(3, False) == "multi_strategy_3plus"
    assert script._report_prefix(2, True) == "multi_strategy_new"
    assert script._report_prefix(3, True) == "multi_strategy_3plus_new"
