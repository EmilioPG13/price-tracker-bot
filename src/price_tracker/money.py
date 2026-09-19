"""Money handling for the whole project: integer cents, never float.

Money is integer cents everywhere, and this module is the only door in; the reasoning
is in `docs/scraper-design.md`. The danger is not arithmetic drift in this file, it is
that a price read as `float` travels all the way into SQLite and comes back as
1898.9999999999998 with no error raised anywhere along the way.

Everything here goes through `Decimal`, which is the same discipline as `decimal.js`
in a JS codebase: parse the store's text exactly as written, then convert once.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

CENTS = Decimal(100)


def to_cents(raw: str | int | float | Decimal) -> int:
    """Convert a price as the store wrote it into integer cents.

    Accepts what JSON-LD actually ships: `"1899.00"`, `1899`, `"1,899.00"`, `499.5`.
    A `float` is accepted because `json.loads` produces one for an unquoted decimal,
    but it is routed through `str()` so the original digits are used rather than the
    binary approximation.

    Raises:
        ValueError: the text is not a price we can read exactly. The caller turns this
            into a `LayoutChangedError`, since it means the store's format moved.
    """
    if isinstance(raw, bool):  # bool is an int subclass; a price is never True
        raise ValueError(f"not a price: {raw!r}")

    if isinstance(raw, str):
        text = raw.strip().replace(",", "").replace("\xa0", "").replace(" ", "")
        text = text.removeprefix("$").removeprefix("MXN").strip()
    else:
        # str(Decimal | int | float) keeps the decimal digits; float(...) would not.
        text = str(raw)

    if not text:
        raise ValueError("empty price")

    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a price: {raw!r}") from exc

    if not amount.is_finite():
        raise ValueError(f"not a finite price: {raw!r}")
    if amount < 0:
        raise ValueError(f"negative price: {raw!r}")

    cents = amount * CENTS
    if cents != cents.to_integral_value():
        # Fractions of a cent mean we misread the format (a thousands separator taken
        # for a decimal point, say). Better to fail loudly than to round silently.
        raise ValueError(f"price has sub-cent precision: {raw!r}")

    return int(cents)


def format_cents(cents: int, currency: str = "MXN") -> str:
    """Render integer cents the way a person reads a price. The way back out.

    Integer arithmetic only, and for the same reason the way in goes through `Decimal`:
    `cents / 100` is float division, so the one place in the codebase that exists to
    keep floats away from money would be reintroducing one on the last line.

    `divmod` also keeps the two halves exact for a negative amount, which a price should
    never be — but this formats whatever it is handed, and a `-$0.01` is a far better
    thing to see in a message than a silently rounded zero.
    """
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"cents must be int cents, got {type(cents).__name__}")

    sign = "-" if cents < 0 else ""
    major, minor = divmod(abs(cents), 100)
    return f"{sign}${major:,}.{minor:02d} {currency}"
