import pandas as pd
import pytest
from pydantic import BaseModel

from quant.engine import loader, selection
from quant.engine.loader import UniverseFilters
from quant.strategies.base import EmptyParams, Strategy


class AboveParams(BaseModel):
    threshold: float = 10.0


class AboveStrategy(Strategy):
    Params = AboveParams

    def generate_signals(self, bars):
        return bars["close"] > self.p.threshold


def make_market():
    rows = []
    for i, code in enumerate(["600000.SH", "600001.SH"]):
        for d in ["20240101", "20240102", "20240103"]:
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": 11.0 + i, "high": 12.0 + i, "low": 10.0 + i,
                "close": 11.0 + i, "raw_close": 11.0 + i,
                "vol": 100.0, "amount": 1000.0 + i, "adj_factor": 1.0,
                "turnover_rate": 1.0, "volume_ratio": 1.0, "pe_ttm": 10.0,
                "pb": 1.0, "total_mv": 100.0, "circ_mv": 80.0,
                "up_limit": 12.0 + i, "down_limit": 10.0 + i,
                "suspended": False, "is_st": False, "is_new": False,
                "board": "main", "raw_open": 11.0 + i,
            })
    return pd.DataFrame(rows)


def test_compute_signals_columns_and_values():
    s = AboveStrategy(threshold=10.5)
    signals = selection.compute_signals(s, make_market())
    assert set(signals.columns) == {"ts_code", "trade_date", "signal", "score"}
    assert signals["signal"].all()
    assert len(signals) == 6


def test_signal_causality_future_data_does_not_change_past(monkeypatch):
    class CrossStrategy(Strategy):
        Params = EmptyParams

        def generate_signals(self, bars):
            ma = bars["close"].rolling(2).mean()
            return bars["close"] > ma

    market = make_market()
    full = selection.compute_signals(CrossStrategy(), market)
    truncated = selection.compute_signals(
        CrossStrategy(), market[market["trade_date"] <= "20240102"])
    common = full[full["trade_date"] <= "20240102"].reset_index(drop=True)
    pd.testing.assert_series_equal(common["signal"], truncated["signal"])


def test_run_selection_ranks_and_filters(monkeypatch):
    market = make_market()
    monkeypatch.setattr(loader, "load_stock_names",
                        lambda: pd.DataFrame({"ts_code": ["600000.SH", "600001.SH"],
                                              "name": ["甲", "乙"]}))
    s = AboveStrategy(threshold=10.5)
    out = selection.run_selection(s, "20240103", top_n=1, market=market, rank_by="amount")
    assert len(out) == 1
    assert out.iloc[0]["ts_code"] == "600001.SH"
    assert out.iloc[0]["name"] == "乙"
    assert out.iloc[0]["rank"] == 1


def test_run_selection_respects_universe(monkeypatch):
    market = make_market()
    market.loc[market["ts_code"] == "600001.SH", "is_st"] = True
    monkeypatch.setattr(loader, "load_stock_names",
                        lambda: pd.DataFrame({"ts_code": ["600000.SH", "600001.SH"],
                                              "name": ["甲", "乙"]}))
    s = AboveStrategy(threshold=10.5)
    out = selection.run_selection(s, "20240103", top_n=5, market=market,
                                  filters=UniverseFilters(exclude_st=True))
    assert list(out["ts_code"]) == ["600000.SH"]


def test_run_selection_raises_on_empty_market(monkeypatch):
    s = AboveStrategy(threshold=10.5)
    monkeypatch.setattr(loader, "load_market_data",
                        lambda **kwargs: pd.DataFrame())
    with pytest.raises(ValueError, match="没有可用行情"):
        selection.run_selection(s, "20240103", market=None)
