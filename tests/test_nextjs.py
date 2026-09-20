"""Tests for reading a Next.js App Router page's embedded state.

`nextjs.py` knows about a framework's wire format and nothing about any store, so it is
tested that way — the Liverpool-specific reading of what the payload *means* is in
`test_liverpool_parser.py`.
"""

import json

import pytest

from price_tracker.scrapers import json_object, rsc_payload


def push(text: str) -> str:
    return f"<script>self.__next_f.push([1,{json.dumps(text)}])</script>"


# --- Reassembling the stream ---------------------------------------------------------


def test_chunks_are_joined_in_document_order():
    html = f"<html><body>{push('2:{"a"')}{push(':1}')}</body></html>"
    assert rsc_payload(html) == '2:{"a":1}'


def test_a_page_with_no_payload_reads_as_empty():
    # Not an error here. The caller decides what an empty payload means, and for a store
    # parser it means `LayoutChangedError` — including for an anti-bot interstitial
    # wearing a 200, which is a shape `docs/store-viability.md` has already met.
    assert rsc_payload("<html><body><p>hola</p></body></html>") == ""


def test_pushes_that_carry_no_string_are_skipped():
    html = (
        "<script>self.__next_f=self.__next_f||[]</script>"
        "<script>self.__next_f.push([0])</script>"
        "<script>self.__next_f.push([2,null])</script>"
        f"{push('hola')}"
    )
    assert rsc_payload(html) == "hola"


def test_escaped_quotes_inside_a_chunk_do_not_end_it():
    # The payload is JSON escaped inside a JSON string, so a quote in a product name
    # arrives as `\"` twice over. A chunk pattern that stopped at the first quote it saw
    # would return a fragment; what comes back has to be exactly what went in.
    payload = '{"name":"SSD 2.5\\" SATA"}'
    assert rsc_payload(push(payload)) == payload


def test_a_malformed_chunk_does_not_take_the_page_down():
    # Same rule as a broken JSON-LD script tag: read what is readable.
    html = '<script>self.__next_f.push([1,"\\q"])</script>' + push("bien")
    assert rsc_payload(html) == "bien"


# --- Reading one object out of it ----------------------------------------------------


def test_finds_the_object_a_key_opens():
    assert json_object('1:{"thing":{"a":1,"b":[2,3]}}', "thing") == {"a": 1, "b": [2, 3]}


def test_a_nested_object_does_not_end_the_match_early():
    payload = '{"thing":{"inner":{"deep":{"x":1}},"after":2}}'
    assert json_object(payload, "thing") == {"inner": {"deep": {"x": 1}}, "after": 2}


def test_braces_inside_a_string_are_not_counted():
    # A product name is a string a store wrote, and stores write whatever they like.
    payload = '{"thing":{"name":"Pack {2} piezas","x":1}}'
    assert json_object(payload, "thing") == {"name": "Pack {2} piezas", "x": 1}


def test_an_escaped_quote_does_not_end_the_string():
    payload = '{"thing":{"name":"SSD 2.5\\" SATA"}}'
    assert json_object(payload, "thing") == {"name": 'SSD 2.5" SATA'}


def test_the_object_is_read_even_when_the_stream_around_it_is_not_closed():
    # Deliberate, and the reason this is brace-matched rather than parsed: the payload
    # is a stream of numbered rows, not a document, and it is routinely incomplete
    # around the part we want. Only the object itself has to be whole.
    assert json_object('7:{"thing":{"a":1},"rest":', "thing") == {"a": 1}


def test_the_first_occurrence_wins():
    payload = '{"thing":{"n":1}} ... {"thing":{"n":2}}'
    assert json_object(payload, "thing") == {"n": 1}


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ('{"other":{"a":1}}', "key is not there"),
        ('{"thing":{"a":1', "object never closes"),
        ('{"thing":[1,2]}', "key opens a list, not an object"),
        ('{"thing":"a string"}', "key opens a string"),
        ('{"thing":{"a":}}', "object is not valid JSON"),
        ("", "nothing at all"),
    ],
)
def test_returns_none_when_there_is_no_readable_object(payload, reason):
    assert json_object(payload, "thing") is None


def test_a_key_that_is_a_substring_of_another_is_not_matched():
    # `"productInfo"` must not be found by looking for `"product"`.
    assert json_object('{"productInfo":{"a":1}}', "product") is None
