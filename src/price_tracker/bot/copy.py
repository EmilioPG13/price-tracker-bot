"""Everything the bot says to a user, in one file.

Bot copy is Spanish — the stores and the users are Mexican — while every other string in
this repo is English. Keeping the copy in one module is what makes that rule checkable
at a glance, and it gives whoever proofreads the Spanish one file to read instead of
four handlers. That is not hypothetical: the welcome message shipped with three missing
accents and a reflexive verb that told the user they would notify *themselves*, and it
was caught by reading it in a chat. No test could have caught it.

Two conventions worth knowing before editing anything below.

**No parse mode.** Every message here is sent as plain text. Product names come from a
store and contain whatever the store felt like — `&`, `<`, quotes — and the failure mode
of HTML parse mode is not an ugly font, it is Telegram rejecting the entire message with
a 400, so the user gets nothing instead of getting it unstyled. A URL on its own line is
still clickable without any markup, which is the only formatting these messages need.

**The store list is derived, not written.** `PARSERS` is the only place that knows which
stores exist. A sentence naming them by hand would be a second place, and it would be
wrong for however long it takes someone to notice after phase 5 adds Liverpool.
"""

from __future__ import annotations

from collections.abc import Sequence

from price_tracker.db.models import ProductStatus, Tracking
from price_tracker.money import format_cents
from price_tracker.scrapers import (
    PARSERS,
    FetchError,
    FetchTimeoutError,
    LayoutChangedError,
    PageGoneError,
    Parser,
    ProductData,
    ProductUnavailableError,
    RobotsDisallowedError,
    ScraperError,
    StoreRefusedError,
    UnsupportedUrlError,
)


def _store_names(parsers: Sequence[Parser] = PARSERS) -> str:
    """The supported stores, as a Spanish list, read off the parser table.

    Takes its parsers as an argument only so the joining can be tested with more than
    one store. There is exactly one today, so the branch that produces "Cyberpuerta y
    Liverpool" would otherwise first run in production, in phase 5.
    """
    names = sorted(parser.store.capitalize() for parser in parsers)
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} y {names[-1]}"


STORES = _store_names()

# A real product URL, short enough to read in a chat bubble. Used by every message that
# has to show the shape of a command rather than describe it.
EXAMPLE_URL = "https://www.cyberpuerta.mx/SSD-Kingston-A400-240GB-SATA-III-2-5-7mm.html"

HELP = (
    "Esto es lo que sé hacer:\n\n"
    "/add <link> <precio> — sigo ese producto y te aviso cuando baje al precio que pidas\n"
    "/list — te muestro lo que estoy siguiendo\n"
    "/remove <número> — dejo de seguir el que tenga ese número en /list\n\n"
    f"Por ahora solo leo links de {STORES}.\n\n"
    "Por ejemplo:\n"
    f"/add {EXAMPLE_URL} 800"
)

WELCOME = f"Hola. Vigilo precios de tiendas en línea y te aviso cuando bajan.\n\n{HELP}"

# What Telegram shows in the menu next to the text box. Short by necessity: the client
# truncates a long description well before the API's own limit.
COMMAND_MENU: tuple[tuple[str, str], ...] = (
    ("add", "Seguir un producto: /add <link> <precio>"),
    ("list", "Ver lo que estoy siguiendo"),
    ("remove", "Dejar de seguir: /remove <número>"),
    ("help", "Cómo usarme"),
)

ADD_USAGE = (
    "Me falta información. Necesito el link del producto y el precio al que quieres "
    "que te avise.\n\n"
    "Así:\n"
    "/add <link> <precio>\n\n"
    "Por ejemplo:\n"
    f"/add {EXAMPLE_URL} 800"
)

ZERO_TARGET = (
    "Un objetivo de cero nunca se va a cumplir. Dime el precio al que quieres que te avise."
)

LIST_EMPTY = f"Todavía no sigues nada.\n\nAgrega algo así:\n/add {EXAMPLE_URL} 800"

LIST_HEADER = "Esto es lo que sigo. El número de la izquierda es el que usa /remove."

REMOVE_USAGE = "Dime cuál. Se usa /remove <número>, con el número que aparece en /list."

REMOVE_NOT_FOUND = "No tengo nada con ese número. Mira /list para ver lo que sigues."

UNEXPECTED = "Algo se rompió de mi lado. Ya quedó registrado; inténtalo otra vez en un rato."


def bad_price(raw: str) -> str:
    """The reply when `money.to_cents` refused the user's text.

    Quotes what was actually typed, which also explains the commonest mistake without
    having to mention it: `/add 800 <link>` answers "no entendí «https://…» como
    precio", and the user sees the swapped arguments for themselves.
    """
    return (
        f"No entendí «{raw}» como precio.\n\n"
        "Escríbelo sin letras y con punto decimal: 800, 1899 o 1,899.00."
    )


def added(data: ProductData, target_cents: int, *, alerting: bool) -> str:
    """The reply to a successful `/add`.

    Deliberately the same sentence whether the tracking is new or the target moved.
    `Repository.set_tracking` does not report which of the two happened — asking twice
    is a change of mind rather than a second row — and inventing the distinction here
    would mean either a second query or a message that is sometimes a lie.
    """
    lines = [
        "Listo. Te aviso cuando baje.",
        "",
        data.name,
        f"Precio ahora: {format_cents(data.price_cents, data.currency)}",
        f"Tu objetivo: {format_cents(target_cents, data.currency)}",
        data.canonical_url,
    ]
    if not data.in_stock:
        lines += ["", "Ahora mismo está agotado, pero le sigo la pista al precio."]
    if alerting:
        lines += ["", "🎉 Ya está en tu objetivo o por debajo."]
    return "\n".join(lines)


def tracking_list(trackings: Sequence[Tracking]) -> str:
    """The `/list` reply. Callers must pass trackings with `product` already loaded."""
    blocks = [LIST_HEADER]
    for position, tracking in enumerate(trackings, start=1):
        product = tracking.product
        current = (
            "sin leer todavía"
            if product.last_price_cents is None
            else format_cents(product.last_price_cents, product.currency)
        )
        target = format_cents(tracking.target_price_cents, product.currency)
        entry = [
            f"{position}. {product.name}",
            f"   Ahora: {current} · objetivo: {target}",
        ]
        if product.status is ProductStatus.RETIRED:
            # Worth saying out loud. Otherwise a product the checker has given up on
            # looks exactly like one whose price simply has not moved.
            entry.append("   (en pausa: la tienda dejó de responder por esta página)")
        entry.append(f"   {product.canonical_url}")
        blocks.append("\n".join(entry))
    return "\n\n".join(blocks)


def removed(name: str) -> str:
    """Names the product that went, because the user chose it by position.

    An index is a fragile thing to act on — it means whatever the last `/list` said — so
    the confirmation repeats the name. If the wrong thing went, the user finds out in
    the same second rather than the next time they look at the list.
    """
    return f"Listo, ya no sigo:\n{name}"


# Which failure gets which reply. Each one tells the user what they can do about it,
# which is the whole reason `scrapers.errors` is six types and not one exception
# carrying a string.
_ERROR_REPLIES: dict[type[ScraperError], str] = {
    UnsupportedUrlError: (
        f"No reconozco esa tienda. Por ahora solo leo links de {STORES}.\n\n"
        "Revisa también que sea el link de un producto y no el de una búsqueda o "
        "una categoría."
    ),
    RobotsDisallowedError: (
        "El robots.txt de esa tienda no permite leer esa página, así que ni siquiera "
        "la voy a pedir. No es un error tuyo: son las reglas que la tienda publica y "
        "que este bot respeta."
    ),
    StoreRefusedError: "La tienda rechazó la petición. Puede ser pasajero; inténtalo más tarde.",
    PageGoneError: "Esa página ya no existe. ¿Seguro que el link sigue vivo?",
    FetchTimeoutError: "La tienda tardó demasiado en contestar. Inténtalo otra vez en un rato.",
    FetchError: "No pude conectarme con la tienda. Inténtalo otra vez en un rato.",
    ProductUnavailableError: (
        "La página cargó, pero no trae ningún precio. Suele pasar con páginas que no "
        "son de un producto, o con algo descontinuado."
    ),
    LayoutChangedError: (
        "La tienda cambió su página y mi lector se quedó atrás. Es un error mío, no "
        "tuyo; ya quedó registrado para arreglarlo."
    ),
}

UNKNOWN_ERROR = "No pude leer ese producto y no sé por qué. Ya quedó registrado."


def reply_for_error(error: ScraperError) -> str:
    """The reply for one scraper failure — most specific match first.

    Walks the exception's MRO instead of testing `isinstance` down a hand-ordered chain.
    An ordered chain is correct only as long as nobody inserts a subclass in the wrong
    place, and when it is wrong it is wrong silently: `StoreRefusedError` listed after
    `FetchError` simply never matches, and the user gets a vaguer message forever.

    The MRO walk also gives a new error type a sane default. A subclass added later with
    no entry of its own inherits its parent's message rather than falling through to
    "no sé por qué" — `RobotsDisallowedError` would read as a generic `FetchError`,
    which is imprecise but true. `tests/test_bot_copy.py` still asserts that every type
    that exists today has a message of its own.
    """
    for klass in type(error).__mro__:
        if klass in _ERROR_REPLIES:
            return _ERROR_REPLIES[klass]
    return UNKNOWN_ERROR
