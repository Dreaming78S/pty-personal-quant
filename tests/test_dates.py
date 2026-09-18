from datetime import date

import pytest

from quant.utils.dates import calendar_days_between, to_yyyymmdd


def test_to_yyyymmdd_accepts_multiple_formats():
    assert to_yyyymmdd("2024-01-02") == "20240102"
    assert to_yyyymmdd("20240102") == "20240102"
    assert to_yyyymmdd(date(2024, 1, 2)) == "20240102"


def test_to_yyyymmdd_rejects_bad_input():
    with pytest.raises(ValueError):
        to_yyyymmdd("2024/01/02")


def test_calendar_days_between_is_inclusive():
    assert calendar_days_between("20240101", "20240103") == [
        "20240101", "20240102", "20240103"]


def test_calendar_days_between_empty_on_reversed_range():
    assert calendar_days_between("20240103", "20240101") == []


def test_calendar_days_between_empty_on_invalid_range():
    assert calendar_days_between("2024-01-01", "20240103") == []
    assert calendar_days_between("20240101", "20240230") == []
