"""quant.engine.exit_tiers 动态止盈分段的单元测试（离线）。

具体边界断言使用固定样例 SAMPLE_TIERS，不依赖 DEFAULT_EXIT_TIERS 的取值，
调整默认分档不需要改测试；对默认分档只做结构一致性校验。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from quant.engine.exit_tiers import (
    DEFAULT_EXIT_TIERS,
    apply_exit_tier_codes,
    apply_exit_thresholds,
    format_tiers_summary,
    normalize_exit_tiers,
    parse_exit_tiers,
    tier_index,
    tier_labels,
    tier_rate,
    tiers_from_scalar,
    tiers_to_jsonable,
    validate_exit_tiers,
)

# 固定样例分档：具体边界断言用它，避免随 DEFAULT_EXIT_TIERS 调整而失效
SAMPLE_TIERS = (
    (None, -0.05, -0.02),
    (-0.05, 0.00, 0.01),
    (0.00, 0.05, 0.02),
    (0.05, 0.07, 0.03),
    (0.07, None, 0.04),
)


class TestDefaultTiers:
    def test_default_covers_whole_line_without_gap(self):
        assert DEFAULT_EXIT_TIERS[0][0] is None
        assert DEFAULT_EXIT_TIERS[-1][1] is None
        for prev, nxt in zip(DEFAULT_EXIT_TIERS, DEFAULT_EXIT_TIERS[1:]):
            assert prev[1] == nxt[0]

    def test_default_boundary_semantics(self):
        # 从默认结构推导：恰好等于上界落入本段（右闭），恰好等于下界落入上一段（左开）
        for i, (lo, hi, rate) in enumerate(DEFAULT_EXIT_TIERS):
            if hi is not None:
                assert tier_rate(hi, DEFAULT_EXIT_TIERS) == rate
                if i + 1 < len(DEFAULT_EXIT_TIERS):
                    assert (
                        tier_rate(hi + 1e-9, DEFAULT_EXIT_TIERS)
                        == DEFAULT_EXIT_TIERS[i + 1][2]
                    )
            if lo is not None:
                assert tier_rate(lo, DEFAULT_EXIT_TIERS) == DEFAULT_EXIT_TIERS[i - 1][2]
                assert tier_rate(lo + 1e-9, DEFAULT_EXIT_TIERS) == rate

    def test_flat_default_equivalent_to_fixed(self):
        # 若所有段止盈率相同（固定阈值型默认），任意涨幅都映射到该值
        rates = {t[2] for t in DEFAULT_EXIT_TIERS}
        if len(rates) == 1:
            (rate,) = rates
            for value in (-0.5, 0.0, 0.03, 0.5):
                assert tier_rate(value, DEFAULT_EXIT_TIERS) == rate

    def test_default_json_roundtrip(self):
        text = json.dumps(tiers_to_jsonable(DEFAULT_EXIT_TIERS))
        assert parse_exit_tiers(text) == DEFAULT_EXIT_TIERS


class TestLabelsAndSummary:
    def test_labels(self):
        assert tier_labels(SAMPLE_TIERS) == (
            "(-∞, -5%]",
            "(-5%, 0%]",
            "(0%, 5%]",
            "(5%, 7%]",
            "(7%, +∞)",
        )

    def test_summary_line(self):
        summary = format_tiers_summary(SAMPLE_TIERS)
        assert "(-∞,-5%]→-2%" in summary
        assert "(7%,+∞)→+4%" in summary


class TestTierLookup:
    @pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
    def test_invalid_values_return_none(self, value):
        assert tier_index(value, SAMPLE_TIERS) is None
        assert tier_rate(value, SAMPLE_TIERS) is None

    def test_sample_boundary_mapping(self):
        # 区间左开右闭：恰好等于上界落入本段，恰好等于下界落入上一段
        cases = {
            -0.10: -0.02,
            -0.05: -0.02,   # 恰好 -5%
            -0.0499: 0.01,
            0.0: 0.01,      # 恰好 0%
            0.0001: 0.02,
            0.05: 0.02,     # 恰好 5%
            0.0501: 0.03,
            0.07: 0.03,     # 恰好 7%
            0.0701: 0.04,
            1.5: 0.04,
        }
        for value, expected in cases.items():
            assert tier_rate(value, SAMPLE_TIERS) == expected

    def test_single_fixed_tier_covers_everything(self):
        tiers = tiers_from_scalar(0.021)
        assert tiers == ((None, None, 0.021),)
        for value in (-10.0, -0.05, 0.0, 0.05, 3.0):
            assert tier_rate(value, tiers) == 0.021

    def test_custom_tiers(self):
        tiers = normalize_exit_tiers([[None, 0.0, -0.01], [0.0, None, 0.05]])
        assert tier_rate(-0.5, tiers) == -0.01
        assert tier_rate(0.0, tiers) == -0.01   # 0 属于左段（右闭）
        assert tier_rate(0.0001, tiers) == 0.05
        assert tier_labels(tiers) == ("(-∞, 0%]", "(0%, +∞)")


class TestVectorized:
    def test_apply_thresholds_matches_scalar(self):
        day1_ret = pd.Series([-0.2, -0.05, -0.01, 0.0, 0.03, 0.05, 0.06, 0.2])
        threshold = apply_exit_thresholds(day1_ret, SAMPLE_TIERS)
        expected = [tier_rate(v, SAMPLE_TIERS) for v in day1_ret]
        assert threshold.tolist() == expected

    def test_apply_thresholds_with_missing_values(self):
        day1_ret = pd.Series([0.01, np.nan, None, float("inf"), -0.06])
        threshold = apply_exit_thresholds(day1_ret, SAMPLE_TIERS)
        assert threshold.iloc[0] == 0.02
        assert threshold.iloc[4] == -0.02
        assert threshold.iloc[1:4].isna().all()

    def test_apply_tier_codes(self):
        day1_ret = pd.Series([-0.06, -0.05, 0.0, 0.07, 0.1, np.nan])
        codes = apply_exit_tier_codes(day1_ret, SAMPLE_TIERS)
        assert codes.tolist() == [0, 0, 1, 3, 4, -1]

    def test_apply_on_empty_series(self):
        empty = pd.Series([], dtype="float64")
        assert apply_exit_thresholds(empty, SAMPLE_TIERS).empty
        assert apply_exit_tier_codes(empty, SAMPLE_TIERS).empty


class TestValidation:
    @pytest.mark.parametrize("bad,reason", [
        ([], "非空列表"),
        ([[0.0, 0.05, 0.02]], "首段下界必须为 None"),
        ([[None, 0.05, 0.02], [0.06, None, 0.03]], "无缝无叠"),
        ([[None, 0.05, 0.02], [0.04, None, 0.03]], "无缝无叠"),
        ([[None, 0.05, 0.02], [0.05, 0.0, 0.03], [0.0, None, 0.04]], "必须小于上界"),
        ([[None, 0.05, 0.02], [0.05, None, -1.0]], "止盈率必须 > -1"),
        ([[None, None, 0.02], [0.05, None, 0.03]], "仅末段上界可为 None"),
        ([[None, 0.05, 0.02], [None, None, 0.03]], "仅首段下界可为 None"),
        ("not-a-list", "非空列表"),
        ([[None, 0.05]], "三个值"),
        ([["a", 0.05, 0.02]], "下界必须是数字或 null"),
        ([[None, float("inf"), 0.02]], "无穷请用 null"),
        ([[None, 0.05, "x"]], "止盈率必须是数字"),
    ])
    def test_invalid_specs_raise(self, bad, reason):
        with pytest.raises(ValueError, match=reason):
            normalize_exit_tiers(bad)

    def test_valid_seamless_chain_passes(self):
        tiers = normalize_exit_tiers([
            [None, -0.05, -0.02],
            [-0.05, 0.0, 0.01],
            [0.0, None, 0.04],
        ])
        validate_exit_tiers(tiers)
        assert len(tiers) == 3

    def test_scalar_degenerate(self):
        with pytest.raises(ValueError, match="有限"):
            tiers_from_scalar(float("nan"))
        with pytest.raises(ValueError, match="-1"):
            tiers_from_scalar(-1.5)


class TestParsing:
    def test_parse_json_roundtrip(self):
        tiers = parse_exit_tiers(
            '[[null, -0.05, -0.02], [-0.05, 0.0, 0.01], [0.0, null, 0.04]]'
        )
        assert tiers == ((None, -0.05, -0.02), (-0.05, 0.0, 0.01), (0.0, None, 0.04))

    def test_parse_invalid_json_raises(self):
        with pytest.raises(ValueError, match="JSON"):
            parse_exit_tiers("[[null, -0.05, -0.02]")

    def test_parse_invalid_tiers_raises(self):
        with pytest.raises(ValueError, match="无缝无叠"):
            parse_exit_tiers("[[null, -0.05, -0.02], [0.0, null, 0.04]]")
