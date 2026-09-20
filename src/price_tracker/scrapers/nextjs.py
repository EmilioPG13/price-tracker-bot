"""Read the state a Next.js App Router page ships inside its own HTML.

This is the counterpart to `jsonld.py`, for stores that publish no structured data. It
carries no knowledge of any store: what it knows is a framework's wire format.

A React Server Components page does not put its data in one tag. It streams the page as
a sequence of script tags, each appending one string to a global queue:

    self.__next_f.push([1,"2a:{\\"productInfo\\":{\\"title\\":\\"Licuadora ...

Concatenating those strings in document order reassembles the payload the server sent —
what React calls the Flight stream. It is not valid JSON as a whole (it is numbered rows
of a stream, and it carries references like `"$undefined"` where a value was elided),
which is why `json_object` reads one object out of it by matching braces rather than
parsing the lot.

**Why this is worth preferring over CSS selectors.** The alternative for a store with no
JSON-LD is reading rendered markup, and Liverpool's markup is generated class names and
`data-testid` attributes — strings a build tool emits, which change when the build does.
The payload is the data the page's own components are rendered *from*, so a field only
disappears from it when the store stops using that field. That is a weaker contract than
JSON-LD, which is a published vocabulary, but a considerably stronger one than a class
name. `docs/liverpool-parser.md` has the full argument.

The cost, stated plainly: this is still a private format. Next.js changed the global
from `__NEXT_DATA__` to `__next_f` between the Pages and App routers, and could change
it again. When it does, `rsc_payload` returns `""` and the parser raises
`LayoutChangedError` — which is the correct outcome, and the reason that error type
exists.
"""

from __future__ import annotations

import json
import re

# One streamed chunk: `self.__next_f.push([<row>,"<a JSON string literal>"])`.
#
# The inner group matches a JSON string with escapes, so an embedded `\"` does not end
# it. Pushes that carry no string — `self.__next_f.push([0])`, `push([2,null])` — are
# bookkeeping and are skipped by not matching at all.
_CHUNK = re.compile(r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")')


def rsc_payload(html: str) -> str:
    """Reassemble the Flight stream out of the page's streamed chunks.

    Returns `""` when the page carries none, which is the honest answer for a page that
    is not a Next.js App Router page at all — including an anti-bot interstitial wearing
    a `200`, the failure `docs/store-viability.md` caught Walmart doing.
    """
    chunks = []
    for literal in _CHUNK.findall(html):
        try:
            chunks.append(json.loads(literal))
        except json.JSONDecodeError:
            # One malformed chunk must not take the page down, for the same reason
            # `extract_jsonld_product` skips a broken script tag: a partial read that
            # still finds the price is worth more than an exception.
            continue
    return "".join(chunks)


def json_object(text: str, key: str) -> dict | None:
    """The JSON object that `"key":` opens, or `None` if there is no readable one.

    Brace-matched rather than parsed, because the surrounding text is a stream and not a
    document. The scan tracks whether it is inside a string so that a `{` in a product
    name — or the `}` in a Flight reference — does not end the object early.

    Takes the first occurrence. In the pages measured there is exactly one `productInfo`
    in a payload that also holds 111 other products for the recommendation carousels,
    and that asymmetry is the whole reason this function looks for a key instead of
    taking the first price it finds. See `liverpool.py`.
    """
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\{{', text)
    if match is None:
        return None

    start = text.index("{", match.start())
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None

    # Ran off the end: the stream was truncated mid-object.
    return None
