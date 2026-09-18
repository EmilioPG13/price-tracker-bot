"""Tests for the cents conversion.

The rule these defend: money is integer cents, never float. The cases
that matter are the ones where a wrong answer is still a plausible number, because
those are the ones no one notices.
"""

import pytest

from price_tracker.money import to_cents


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1899.00", 189900),
        ("879", 87900),
        (889, 88900),  # Cyberpuerta ships unquoted integers in its JSON-LD
        (499.5, 49950),  # json.loads hands us a float for an unquoted decimal
        ("499.5", 49950),
        ("1,899.00", 189900),  # thousands separator
        ("$1,252.53", 125253),
        ("0", 0),
        ("0.01", 1),
    ],
)
def test_reads_prices_as_stores_write_them(raw, expected):
    assert to_cents(raw) == expected


def test_float_goes_through_its_decimal_digits():
    # 1899.00 is not exactly representable in binary. Converting via str() keeps the
    # digits the store wrote; float arithmetic would land on 189899 often enough to be
    # a bug and rarely enough to be missed.
    assert to_cents(1899.00) == 189900
    assert to_cents(0.29) == 29


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "consultar precio",
        "1.899,00",  # European format: reading this as 1.89900 would be a silent 100x
        "-10",
        True,
        float("nan"),
        float("inf"),
    ],
)
def test_refuses_what_it_cannot_read_exactly(raw):
    with pytest.raises(ValueError):
        to_cents(raw)


def test_sub_cent_precision_is_an_error_not_a_rounding():
    # A price with three decimals means the format moved and we are misreading it.
    with pytest.raises(ValueError, match="sub-cent"):
        to_cents("10.001")
