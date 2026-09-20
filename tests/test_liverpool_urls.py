"""Tests for turning a pasted Liverpool link into something worth fetching.

Same job as `test_cyberpuerta_urls.py` and a different shape of URL: Liverpool puts the
product id in the path, which Cyberpuerta does not. That makes the routing rule easy and
makes one decision worth being explicit about — see the `skuId` test.
"""

import pytest

from price_tracker.scrapers import LiverpoolParser, UnsupportedUrlError

PRODUCT = "https://www.liverpool.com.mx/tienda/pdp/licuadora-oster-2110245-2-velocidades/1141535451"


@pytest.fixture
def parser():
    return LiverpoolParser()


def test_a_clean_product_url_is_left_alone(parser):
    assert parser.normalise_url(PRODUCT) == PRODUCT


@pytest.mark.parametrize(
    "suffix",
    [
        "?utm_source=telegram&utm_medium=share",
        "?cm_mmc=email",
        "#opiniones",
        "?fbclid=abc123#top",
        "/",
    ],
)
def test_tracking_parameters_and_trailing_slashes_are_dropped(parser, suffix):
    assert parser.normalise_url(PRODUCT + suffix) == PRODUCT


def test_http_and_bare_host_are_normalised(parser):
    assert parser.normalise_url(PRODUCT.replace("https://www.", "http://")) == PRODUCT


def test_a_share_links_sku_is_dropped_with_the_rest_of_the_query(parser):
    # `?skuId=` names a variant — a size or a colour — of the same listing. Deliberate:
    # `external_id` is the product id every variant shares, so keeping the parameter
    # would not buy a second row, it would only make the stored canonical_url flap.
    assert parser.normalise_url(PRODUCT + "?skuId=1141535451") == PRODUCT


@pytest.mark.parametrize(
    "product_id",
    ["1141535451", "99991606363", "999673847131"],
)
def test_product_ids_are_not_assumed_to_be_one_length(parser, product_id):
    # All three lengths appear in the captured pages. A `\d{10}` would have rejected two
    # of them, and the user would have been told Liverpool was not supported.
    url = f"https://www.liverpool.com.mx/tienda/pdp/algo/{product_id}"
    assert parser.normalise_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.liverpool.com.mx/tienda/categoria/licuadoras",  # category
        "https://www.liverpool.com.mx/",  # front page
        "https://www.liverpool.com.mx/tienda/pdp/",  # no product
        "https://www.liverpool.com.mx/tienda/pdp/solo-slug",  # slug, no id
        "https://www.liverpool.com.mx/tienda/pdp/algo/no-es-un-id",  # id is not digits
        "https://www.liverpool.com.mx/tienda/pdp/algo/123/extra",  # something after the id
        "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB.html",  # another store
        "https://www.liverpool.com.mx.evil.example/tienda/pdp/a/1",  # lookalike host
        "ftp://www.liverpool.com.mx/tienda/pdp/a/1",
        "hola que tal",
        "",
    ],
)
def test_rejects_what_is_not_a_liverpool_product_page(parser, url):
    with pytest.raises(UnsupportedUrlError):
        parser.normalise_url(url)


def test_supports_mirrors_normalise(parser):
    assert parser.supports(PRODUCT + "?utm_source=x")
    assert not parser.supports("https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB.html")
