"""Tests for turning a pasted link into something worth fetching.

The link arrives from a human in a Telegram message, so it arrives however they copied
it: with tracking parameters, from a mobile share sheet, or as the wrong page entirely.
"""

import pytest

from price_tracker.scrapers import CyberpuertaParser, UnsupportedUrlError

PRODUCT = (
    "https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/"
    "SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"
)


@pytest.fixture
def parser():
    return CyberpuertaParser()


def test_a_clean_product_url_is_left_alone(parser):
    assert parser.normalise_url(PRODUCT) == PRODUCT


@pytest.mark.parametrize(
    "suffix",
    [
        "?utm_source=telegram&utm_medium=share",
        "?campaign=black-friday",
        "#opiniones",
        "?fbclid=abc123#top",
    ],
)
def test_tracking_parameters_are_dropped(parser, suffix):
    # This is why products are unique on (store, external_id) and not on URL: the same
    # SSD arrives in as many spellings as there are places to share it from.
    assert parser.normalise_url(PRODUCT + suffix) == PRODUCT


def test_http_and_bare_host_are_normalised(parser):
    assert parser.normalise_url(PRODUCT.replace("https://www.", "http://")) == PRODUCT


def test_the_oxid_internal_url_keeps_the_query_that_is_its_address(parser):
    # Here the query string is not tracking, it is the whole address. Stripping it
    # would leave a link to the shop's front door.
    anid = "c156664ff9062de87fc3bf694dbb8eae"
    pasted = f"https://www.cyberpuerta.mx/index.php?cl=details&anid={anid}&utm_source=x"
    expected = f"https://www.cyberpuerta.mx/index.php?cl=details&anid={anid}"
    assert parser.normalise_url(pasted) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cyberpuerta.mx/Computo-Hardware/Discos-Duros-SSD-NAS/SSD/",  # category
        "https://www.cyberpuerta.mx/",  # front page
        "https://www.cyberpuerta.mx/index.php?cl=search&searchparam=ssd",  # search
        "https://www.cyberpuerta.mx/index.php?cl=details&anid=nope",  # not an anid
        "https://www.liverpool.com.mx/tienda/pdp/algo/1141535451",  # another store
        "https://www.cyberpuerta.mx.evil.example/p.html",  # lookalike host
        "ftp://www.cyberpuerta.mx/p.html",
        "hola que tal",
        "",
    ],
)
def test_rejects_what_is_not_a_cyberpuerta_product_page(parser, url):
    with pytest.raises(UnsupportedUrlError):
        parser.normalise_url(url)


def test_supports_mirrors_normalise(parser):
    assert parser.supports(PRODUCT + "?utm_source=x")
    assert not parser.supports("https://www.liverpool.com.mx/tienda/pdp/algo/1141535451")
