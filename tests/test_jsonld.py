"""Tests for JSON-LD extraction.

These graduated from `tests/test_spike_jsonld.py` together with the function they
cover. Stores ship schema.org data in three shapes and all three appear in the wild, so
the extractor has to handle each; the cases below are unchanged in substance, adapted
to the richer return type the application needs.
"""

from price_tracker.scrapers.jsonld import extract_jsonld_product


def page(*blocks: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body><h1>irrelevant</h1></body></html>"


def test_bare_product_object():
    html = page("""
        {"@context":"https://schema.org","@type":"Product","name":"Licuadora X",
         "offers":{"@type":"Offer","price":"1899.00","priceCurrency":"MXN"}}
    """)
    product = extract_jsonld_product(html)
    assert product is not None
    assert (product.name, product.price, product.currency) == ("Licuadora X", "1899.00", "MXN")


def test_product_inside_a_list():
    html = page("""
        [{"@type":"BreadcrumbList","itemListElement":[]},
         {"@type":"Product","name":"Taladro Y",
          "offers":{"price":499.5,"priceCurrency":"MXN"}}]
    """)
    product = extract_jsonld_product(html)
    assert product is not None and product.name == "Taladro Y"
    # Numeric prices must survive as text: converting here would be the first place a
    # float could creep into money.
    assert product.price == "499.5"


def test_product_inside_a_graph_wrapper():
    html = page("""
        {"@context":"https://schema.org","@graph":[
           {"@type":"WebPage"},
           {"@type":["Product","Thing"],"name":"Monitor Z",
            "offers":{"lowPrice":"3200","priceCurrency":"MXN"}}]}
    """)
    product = extract_jsonld_product(html)
    # @type can be a list, and an AggregateOffer only exposes lowPrice.
    assert product is not None
    assert (product.name, product.price) == ("Monitor Z", "3200")


def test_multiple_blocks_skips_the_non_product_one():
    html = page(
        '{"@type":"Organization","name":"Tienda"}',
        '{"@type":"Product","name":"Real","offers":{"price":"10","priceCurrency":"MXN"}}',
    )
    product = extract_jsonld_product(html)
    assert product is not None and product.name == "Real"


def test_malformed_json_does_not_crash_the_run():
    # A store shipping one broken script tag must degrade to "no price found", not take
    # a whole scheduled run down with it.
    assert extract_jsonld_product(page("{not json at all,,,}")) is None


def test_page_without_any_jsonld():
    # Liverpool's shape, and the reason it was chosen as store #2.
    html = "<html><body><span class='price'>$1,899</span></body></html>"
    assert extract_jsonld_product(html) is None


def test_product_without_offers():
    # Out of stock pages routinely drop the offers block entirely.
    html = page('{"@type":"Product","name":"Agotado"}')
    product = extract_jsonld_product(html)
    assert product is not None
    assert (product.name, product.price) == ("Agotado", None)


def test_availability_url_is_reduced_to_its_term():
    html = page("""
        {"@type":"Product","name":"Con URL",
         "offers":{"price":"1","priceCurrency":"MXN",
                   "availability":"https://schema.org/OutOfStock"}}
    """)
    product = extract_jsonld_product(html)
    # Stores write availability both ways and they mean the same thing.
    assert product is not None and product.availability == "OutOfStock"


def test_first_offer_wins_when_several_are_listed():
    html = page("""
        {"@type":"Product","name":"Varios vendedores",
         "offers":[{"price":"100","priceCurrency":"MXN"},
                   {"price":"90","priceCurrency":"MXN"}]}
    """)
    product = extract_jsonld_product(html)
    # Not the cheapest: that would track a different seller than the user looked at.
    assert product is not None and product.price == "100"


def test_sku_and_offer_url_are_carried_through():
    html = page("""
        {"@type":"Product","name":"Con SKU","sku":"ABC-123",
         "offers":{"price":"1","priceCurrency":"MXN","url":"https://tienda.mx/p.html"}}
    """)
    product = extract_jsonld_product(html)
    assert product is not None
    assert (product.sku, product.url) == ("ABC-123", "https://tienda.mx/p.html")
