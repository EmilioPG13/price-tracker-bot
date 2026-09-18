"""Tests for the two-run comparison.

The comparison is what distinguishes a real page from a challenge page wearing a
`200`, so its readings decide which stores make the MVP. They get tests.
"""

from store_spike import render_comparison


def run(label: str, **stores) -> dict:
    """Build a run document. Each store is (bytes, has_jsonld, price)."""
    return {
        "label": label,
        "egress": f"1.2.3.4 ({label})",
        "results": [
            {
                "name": name,
                "bytes_received": b,
                "has_jsonld_product": ld,
                "parsed_price": price,
            }
            for name, (b, ld, price) in stores.items()
        ],
    }


def reading_for(store: str, table: str) -> str:
    row = next(line for line in table.splitlines() if line.startswith(f"| {store} |"))
    return row.rsplit("|", 2)[-2].strip()


def test_collapsed_body_is_called_a_challenge_page():
    # Walmart's real signature: 279 KB residentially, 13 KB from a datacenter IP,
    # both HTTP 200.
    a = run("home", walmart=(278_961, True, "1252.53"))
    b = run("ci", walmart=(13_599, False, None))
    assert "challenge" in reading_for("walmart", render_comparison(a, b))


def test_same_size_but_structured_data_gone():
    # Amazon's signature: the page is served, the JSON-LD is not.
    a = run("home", amazon=(1_015_482, True, "1295"))
    b = run("ci", amazon=(1_036_734, False, None))
    assert "withheld" in reading_for("amazon", render_comparison(a, b))


def test_identical_and_readable_is_the_green_case():
    a = run("home", cyberpuerta=(247_996, True, "879"))
    b = run("ci", cyberpuerta=(247_996, True, "879"))
    assert reading_for("cyberpuerta", render_comparison(a, b)) == (
        "identical treatment, price readable"
    )


def test_identical_but_no_price_is_not_a_block():
    # Liverpool: consistent across networks, price simply is not in JSON-LD.
    # This must not be reported as blocking, or a usable store gets dropped.
    a = run("home", liverpool=(1_083_470, False, None))
    b = run("ci", liverpool=(1_083_469, False, None))
    got = reading_for("liverpool", render_comparison(a, b))
    assert "price not in server HTML" in got
    assert "challenge" not in got


def test_store_missing_from_the_other_run_is_skipped():
    a = run("home", only_here=(1000, True, "10"))
    b = run("ci", different=(1000, True, "10"))
    assert "only_here" not in render_comparison(a, b)


def test_zero_byte_baseline_does_not_divide_by_zero():
    a = run("home", broken=(0, False, None))
    b = run("ci", broken=(0, False, None))
    assert "0.00x" in render_comparison(a, b)
