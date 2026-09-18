from datetime import date

import pytest

from quant.utils.dates import to_yyyymmdd


def test_to_yyyymmdd_accepts_multiple_formats():
    assert to_yyyymmdd("2024-01-02") == "20240102"
    assert to_yyyymmdd("20240102") == "20240102"
    assert to_yyyymmdd(date(2024, 1, 2)) == "20240102"


def test_to_yyyymmdd_rejects_bad_input():
    with pytest.raises(ValueError):
        to_yyyymmdd("2024/01/02")
