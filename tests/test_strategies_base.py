import pandas as pd
import pytest
from pydantic import BaseModel

from quant.strategies.base import (EmptyParams, get_strategy, list_strategies,
                                   load_strategy_config, register_strategy,
                                   Strategy)


class DummyParams(BaseModel):
    threshold: float = 1.0


@register_strategy("dummy")
class DummyStrategy(Strategy):
    Params = DummyParams

    def generate_signals(self, bars):
        return bars["close"] > self.p.threshold


def test_registry_contains_dummy():
    assert "dummy" in list_strategies()


def test_get_strategy_builds_instance_with_params():
    s = get_strategy("dummy", threshold=2.0)
    assert isinstance(s, DummyStrategy)
    assert s.p.threshold == 2.0


def test_unknown_strategy_raises():
    with pytest.raises(KeyError, match="未注册"):
        get_strategy("nope")


def test_invalid_params_raise():
    with pytest.raises(Exception):
        get_strategy("dummy", threshold="not-a-number")


def test_load_strategy_config(tmp_path):
    path = tmp_path / "dummy.yaml"
    path.write_text("strategy: dummy\nparams:\n  threshold: 3.5\n", encoding="utf-8")
    name, params = load_strategy_config(path)
    assert name == "dummy"
    assert params == {"threshold": 3.5}


def test_empty_params_default():
    assert EmptyParams().model_dump() == {}


def test_list_command_shows_registered_strategies():
    from typer.testing import CliRunner

    from quant.cli import app

    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0
    assert "dummy" in result.output


def test_prepare_default_returns_market_unchanged():
    market = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"],
                           "close": [10.0]})
    assert DummyStrategy().prepare(market) is market
