from decimal import Decimal

import pandas as pd
import pytest

from quant.engine import recommend

_CFG = recommend.RecommendConfig()


def _dates(start, end):
    index = pd.date_range(start, end)
    return [d.strftime("%Y%m%d") for d in index]


def _open_benchmark():
    dates = _dates("2026-08-20", "2026-09-18")
    values = [10.0] * (len(dates) - 1) + [11.0]
    return pd.Series(values, index=pd.Index(dates, name="trade_date"))


def _closed_benchmark():
    dates = _dates("2026-08-20", "2026-09-18")
    values = [10.0] * (len(dates) - 1) + [9.9]
    return pd.Series(values, index=pd.Index(dates, name="trade_date"))


def _hit_frame(rows):
    return pd.DataFrame(rows, columns=["ts_code", "rank", "name", "industry",
                                       "raw_close", "amount"])


def _scenario_frames():
    limit_up = _hit_frame([
        ("000001.SZ", 2, "股A", "行业A", Decimal("10.5"), Decimal("100000")),
    ])
    rps = _hit_frame([
        ("000002.SZ", 1, "股B", "行业B", 21.34, 300000.0),
        ("000003.SZ", 4, "股C", "行业C", 37.86, 200000.0),
        ("000004.SZ", 9, "股D", "行业D", 8.88, 150000.0),
    ])
    turtle = _hit_frame([
        ("000002.SZ", 3, "股B", "行业B", 21.34, 300000.0),
        ("000003.SZ", 9, "股C", "行业C", 37.86, 200000.0),
        ("000004.SZ", 12, "股D", "行业D", 8.88, 150000.0),
    ])
    ma_volume = _hit_frame([
        ("000002.SZ", 5, "股B", "行业B", 21.34, 300000.0),
    ])
    return {"limit_up_shakeout": limit_up, "rps_breakout": rps,
            "turtle_trade": turtle, "ma_volume": ma_volume}


def _patch_common(monkeypatch, frames, benchmark=None, watermark="20260918"):
    monkeypatch.setattr(recommend.loader, "resolve_trade_date",
                        lambda date=None: "20260918")
    monkeypatch.setattr(recommend.loader, "load_benchmark",
                        lambda code, start, end: (
                            benchmark if benchmark is not None
                            else _open_benchmark()))
    monkeypatch.setattr(recommend.ingest, "get_watermark",
                        lambda table: watermark)

    def fake_read(sql, params=None):
        for name, frame in frames.items():
            if f"`hit_{name}`" in sql:
                return frame
        return pd.DataFrame(columns=["ts_code", "rank", "name", "industry",
                                     "raw_close", "amount"])

    monkeypatch.setattr(recommend.db, "read_df", fake_read)


def test_strategy_names_zh_cover_all_hit_strategies():
    from quant.data import schemas

    for name in schemas.HIT_STRATEGIES:
        assert name in recommend.STRATEGY_NAMES_ZH
    assert recommend.strategy_label("ma_volume") == "均线放量"
    assert recommend.strategy_label("rise_shrink_pullback") == "上涨缩量回调"
    assert recommend.strategy_label("no_such") == "no_such"


def test_load_config_defaults_when_file_missing(tmp_path):
    config = recommend.load_config(tmp_path / "nope.yaml")

    assert config == recommend.RecommendConfig()


def test_load_config_reads_yaml_overrides(tmp_path):
    path = tmp_path / "recommend.yaml"
    path.write_text(
        "gate:\n"
        "  index: \"000905.SH\"\n"
        "  ma_days: 30\n"
        "tiers:\n"
        "  primary_strategies: [\"limit_up_shakeout\", \"ma_volume\"]\n"
        "  primary_min_co: 4\n"
        "  secondary_min_co: 2\n"
        "  secondary_max_rank: 8\n"
        "max_picks: 6\n",
        encoding="utf-8")

    config = recommend.load_config(path)

    assert config.gate_index == "000905.SH"
    assert config.gate_ma_days == 30
    assert config.primary_strategies == ("limit_up_shakeout", "ma_volume")
    assert config.primary_min_co == 4
    assert config.secondary_min_co == 2
    assert config.secondary_max_rank == 8
    assert config.max_picks == 6


def test_market_gate_open_when_index_above_ma(monkeypatch):
    monkeypatch.setattr(recommend.loader, "load_benchmark",
                        lambda code, start, end: _open_benchmark())

    gate_open, close, ma = recommend.market_gate("20260918", config=_CFG)

    assert gate_open is True
    assert close == 11.0
    assert ma == pytest.approx(10.05)


def test_market_gate_closed_when_index_below_ma(monkeypatch):
    monkeypatch.setattr(recommend.loader, "load_benchmark",
                        lambda code, start, end: _closed_benchmark())

    gate_open, close, ma = recommend.market_gate("20260918", config=_CFG)

    assert gate_open is False
    assert close == 9.9


def test_market_gate_raises_when_index_stale(monkeypatch):
    stale = _open_benchmark().iloc[:-1]
    monkeypatch.setattr(recommend.loader, "load_benchmark",
                        lambda code, start, end: stale)

    with pytest.raises(ValueError, match="index_daily"):
        recommend.market_gate("20260918", config=_CFG)


def test_market_gate_raises_when_history_too_short(monkeypatch):
    dates = _dates("2026-09-09", "2026-09-18")
    short = pd.Series([10.0] * len(dates),
                      index=pd.Index(dates, name="trade_date"))
    monkeypatch.setattr(recommend.loader, "load_benchmark",
                        lambda code, start, end: short)

    with pytest.raises(ValueError, match="不足"):
        recommend.market_gate("20260918", config=_CFG)


def test_build_recommendations_tiers_reasons_and_order(monkeypatch):
    _patch_common(monkeypatch, _scenario_frames())

    result = recommend.build_recommendations("20260918", config=_CFG)

    assert result.date == "20260918"
    assert result.gate_open is True
    assert result.index_close == 11.0
    assert result.index_ma == pytest.approx(10.05)
    assert [pick.ts_code for pick in result.primary] == ["000002.SZ", "000001.SZ"]
    assert [pick.ts_code for pick in result.secondary] == ["000003.SZ"]

    top, shakeout = result.primary
    assert top.tier == "重点"
    assert top.co_count == 3
    assert top.hits == (("RPS突破", 1), ("海龟交易", 3), ("均线放量", 5))
    assert top.name == "股B"
    assert top.raw_close == 21.34
    assert top.amount == 300000.0

    assert shakeout.tier == "重点"
    assert shakeout.co_count == 1
    assert shakeout.hits == (("涨停洗盘", 2),)
    assert shakeout.raw_close == 10.5  # Decimal 转 float

    secondary = result.secondary[0]
    assert secondary.tier == "备选"
    assert secondary.co_count == 2
    assert secondary.hits == (("RPS突破", 4), ("海龟交易", 9))


def test_build_recommendations_gate_closed_returns_no_picks(monkeypatch):
    _patch_common(monkeypatch, _scenario_frames(), benchmark=_closed_benchmark())

    result = recommend.build_recommendations("20260918", config=_CFG)

    assert result.gate_open is False
    assert result.primary == ()
    assert result.secondary == ()


def test_build_recommendations_open_day_without_hits(monkeypatch):
    _patch_common(monkeypatch, {})

    result = recommend.build_recommendations("20260918", config=_CFG)

    assert result.gate_open is True
    assert result.primary == ()
    assert result.secondary == ()


def test_build_recommendations_caps_total_picks(monkeypatch):
    _patch_common(monkeypatch, _scenario_frames())

    result = recommend.build_recommendations(
        "20260918", config=recommend.RecommendConfig(max_picks=2))

    assert [pick.ts_code for pick in result.primary] == ["000002.SZ", "000001.SZ"]
    assert result.secondary == ()


def test_build_recommendations_rejects_stale_hit_watermark(monkeypatch):
    _patch_common(monkeypatch, _scenario_frames(), watermark="20260917")

    with pytest.raises(ValueError, match="未更新到"):
        recommend.build_recommendations("20260918", config=_CFG)


def test_build_recommendations_high_co_without_rank_still_primary(monkeypatch):
    # ≥3 策略共振即使排名靠后也进重点档
    frames = {
        "rps_breakout": _hit_frame([("000004.SZ", 9, "股D", "行业D", 8.88, 1.0)]),
        "turtle_trade": _hit_frame([("000004.SZ", 12, "股D", "行业D", 8.88, 1.0)]),
        "ma_volume": _hit_frame([("000004.SZ", 20, "股D", "行业D", 8.88, 1.0)]),
    }
    _patch_common(monkeypatch, frames)

    result = recommend.build_recommendations("20260918", config=_CFG)

    assert [pick.ts_code for pick in result.primary] == ["000004.SZ"]
    assert result.secondary == ()
