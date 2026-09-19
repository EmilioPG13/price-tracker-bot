"""Tests for the cents conversion.

The rule these defend: money is integer cents, never float. The cases
that matter are the ones where a wrong answer is still a plausible number, because
those are the ones no one notices.
"""

import pytest

from price_tracker.money import format_cents, to_cents


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


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (88900, "$889.00 MXN"),
        (189900, "$1,899.00 MXN"),
        (0, "$0.00 MXN"),
        (1, "$0.01 MXN"),
        (99, "$0.99 MXN"),
        (100, "$1.00 MXN"),
        (123456789, "$1,234,567.89 MXN"),
    ],
)
def test_formats_cents_for_a_person_to_read(cents, expected):
    assert format_cents(cents) == expected


def test_the_currency_is_the_product_s_own():
    assert format_cents(88900, "USD") == "$889.00 USD"


def test_formatting_survives_a_round_trip():
    # The two halves of this module have to agree, or a price shown to a user is not
    # the price stored for them. Formatting is the only path back out.
    for raw in ("1899.00", "0.01", "889", "1,252.53"):
        assert to_cents(format_cents(to_cents(raw)).removesuffix(" MXN")) == to_cents(raw)


def test_a_negative_amount_keeps_both_halves():
    # Never a price, but a difference between two of them might be, and -$0.01 read as
    # -$0.99 would be the kind of plausible wrong number this module exists to prevent.
    assert format_cents(-1) == "-$0.01 MXN"


@pytest.mark.parametrize("cents", [True, 88.5, "88900", None])
def test_refuses_anything_that_is_not_int_cents(cents):
    with pytest.raises(TypeError):
        format_cents(cents)
