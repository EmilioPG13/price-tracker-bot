"""Tests for the spike's JSON-LD extraction.

Stores ship schema.org data in three shapes and all three appear in the wild, so the
extractor has to handle each. This logic graduates into the real parser in phase 1;
these tests come along with it.
"""

from store_spike import extract_jsonld_product


def page(*blocks: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body><h1>irrelevant</h1></body></html>"


def test_bare_product_object():
    html = page("""
        {"@context":"https://schema.org","@type":"Product","name":"Licuadora X",
         "offers":{"@type":"Offer","price":"1899.00","priceCurrency":"MXN"}}
    """)
    found, name, price, currency = extract_jsonld_product(html)
    assert (found, name, price, currency) == (True, "Licuadora X", "1899.00", "MXN")


def test_product_inside_a_list():
    html = page("""
        [{"@type":"BreadcrumbList","itemListElement":[]},
         {"@type":"Product","name":"Taladro Y",
          "offers":{"price":499.5,"priceCurrency":"MXN"}}]
    """)
    found, name, price, _ = extract_jsonld_product(html)
    assert found and name == "Taladro Y"
    # Numeric prices must survive as text; the spike reports what the store said.
    assert price == "499.5"


def test_product_inside_a_graph_wrapper():
    html = page("""
        {"@context":"https://schema.org","@graph":[
           {"@type":"WebPage"},
           {"@type":["Product","Thing"],"name":"Monitor Z",
            "offers":{"lowPrice":"3200","priceCurrency":"MXN"}}]}
    """)
    found, name, price, _ = extract_jsonld_product(html)
    # @type can be a list, and some stores only expose lowPrice.
    assert (found, name, price) == (True, "Monitor Z", "3200")


def test_multiple_blocks_skips_the_non_product_one():
    html = page(
        '{"@type":"Organization","name":"Tienda"}',
        '{"@type":"Product","name":"Real","offers":{"price":"10","priceCurrency":"MXN"}}',
    )
    found, name, _, _ = extract_jsonld_product(html)
    assert found and name == "Real"


def test_malformed_json_does_not_crash_the_run():
    # A store shipping broken JSON-LD must degrade to "no price", not take the spike down.
    html = page("{not json at all,,,}")
    assert extract_jsonld_product(html) == (False, None, None, None)


def test_page_without_any_jsonld():
    html = "<html><body><span class='price'>$1,899</span></body></html>"
    assert extract_jsonld_product(html) == (False, None, None, None)


def test_product_without_offers():
    # Out-of-stock pages routinely drop the offers block entirely.
    html = page('{"@type":"Product","name":"Agotado"}')
    found, name, price, _ = extract_jsonld_product(html)
    assert (found, name, price) == (True, "Agotado", None)
