"""Telegram-safe formatting helpers.

Telegram's Markdown/HTML parsers reject unbalanced markup. HAVOC builds every
message from small, escaped pieces so a user-supplied name containing ``*`` or
``<`` can never break the whole message (or be used to smuggle links).

The default rendering is HTML because entity escaping is unambiguous and it
composes better with Persian text (which Markdown V1 mangles around
underscores).

Feature area: 30 (message formatting toolkit), 63 (Persian display).
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence

from .text import split_message

MAX_MESSAGE_LENGTH = 4000


#: The entities this project produces. Only these are unescaped before escaping
#: again: a person who literally types ``&copy;`` must still see ``&amp;copy;``.
_OURS = re.compile(r"&(amp|lt|gt|quot|#x27);")


def escape(text: object) -> str:
    """Escape text for Telegram HTML parse mode — **idempotently**.

    ``bold()``, ``italic()``, ``code()`` and friends already escape their
    argument, and a hundred call sites do ``bold(escape(name))`` because it is
    the obvious thing to write. Escaping twice shipped the entity itself to the
    chat — a person called ``<b>Sara`` arrived as ``&amp;lt;b&amp;gt;Sara``, and
    every card that formatted a user's word showed the machinery instead of the
    word.

    Un-escaping our own entities first makes ``escape`` a fixed point:
    ``escape(escape(x)) == escape(x)`` for every ``x``. Foreign entities
    (``&copy;``) are left alone, because turning them into a copyright sign is
    not this function's job, and a user typing ``&amp;`` must still see
    ``&amp;``.
    """

    if text is None:
        return ""
    raw = str(text)
    if "&" in raw:
        # One pass is enough: unescape yields no new ``&entity;`` shapes of ours.
        raw = _OURS.sub(lambda match: html.unescape(match.group(0)), raw)
    return html.escape(raw, quote=False)


def bold(text: object) -> str:
    return f"<b>{escape(text)}</b>"


def italic(text: object) -> str:
    return f"<i>{escape(text)}</i>"






def code(text: object) -> str:
    return f"<code>{escape(text)}</code>"


def pre(text: object, language: str | None = None) -> str:
    body = escape(text)
    if language:
        return f'<pre><code class="language-{escape(language)}">{body}</code></pre>'
    return f"<pre>{body}</pre>"




def link(text: object, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{escape(text)}</a>'




def quote(text: object) -> str:
    """Telegram's blockquote entity."""
    return f"<blockquote>{escape(text)}</blockquote>"






def kv(key: str, value: object) -> str:
    """``key: value`` with a bold key — used across status output."""
    return f"{bold(key)}: {escape(value)}"






def key_value_block(pairs: Iterable[tuple[str, object]]) -> str:
    return "\n".join(kv(key, value) for key, value in pairs)


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_output(text: str) -> str:
    """Remove control chars that break Telegram rendering, keep RTL marks."""
    return _CTRL_RE.sub("", text)


def prepare(text: str, *, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Sanitize, repair stray markup, and split a message for sending."""
    # Imported here: markup imports :func:`escape` from this module.
    from .markup import rescue_inline

    body = sanitize_output(rescue_inline(repair_html(text)))
    # Three blank lines are never a shape; a model that leaves them makes the
    # message look broken. One blank line is the paragraph break.
    body = _MANY_BLANK_LINES.sub("\n\n", body)
    return [chunk for chunk in split_message(body, limit) if chunk]


_MANY_BLANK_LINES = re.compile(r"\n{3,}")

_HTML_TAG_RE = re.compile(r"<[^<>]{1,64}>")

#: Every tag Telegram's HTML parser actually knows. Anything else in angle
#: brackets is **text** — and Telegram either rejects the message or, worse,
#: shows the escaping to him. Locale strings use ``<name>`` for "put a name
#: here", which is exactly the shape that used to arrive as ``&lt;نام&gt;``.
_ALLOWED_TAGS = frozenset(
    {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code",
        "pre", "span", "tg-spoiler", "blockquote", "expandable_blockquote", "tg-emoji",
    }
)
_TAG_SHAPE = re.compile(r"^</?([a-zA-Z][a-zA-Z0-9-]*)(\s[^<>]*)?/?>$")
#: A ``<`` that does not open a tag: a kaomoji's, or an arithmetic sign.
_LONE_LT = re.compile(r"<(?!/?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>)")


def repair_html(text: str) -> str:
    """Escape angle-bracket text that is not a tag Telegram understands.

    Every message HAVOC sends goes through this: a locale saying ``<name>``, a
    folder called ``2 < 3`` or a model that invents ``<mood>`` all arrive as the
    characters the author meant instead of a Telegram parse error or a visible
    ``&lt;``.
    """
    if not text or "<" not in text:
        return text

    def fix(match: re.Match[str]) -> str:
        tag = match.group(0)
        shape = _TAG_SHAPE.match(tag)
        if shape and shape.group(1).lower() in _ALLOWED_TAGS:
            return tag
        return html.escape(tag, quote=False)

    body = _HTML_TAG_RE.sub(fix, text)
    # Whatever ``<`` is left either opens a tag we kept or opens nothing at all.
    # A kaomoji (``>_<``) right before a card used to be read *together with* the
    # card's first ``<b>`` as one broken tag, so the opening tag was escaped and
    # the closing ``</b>`` was not: Telegram then refused the whole message.
    return _LONE_LT.sub("&lt;", body)


def strip_html(text: str) -> str:
    """Plain-text rendering (used for logs, notifications, transcripts)."""
    plain = _HTML_TAG_RE.sub("", text)
    return html.unescape(plain)


def table(rows: Sequence[Sequence[object]], headers: Sequence[str] | None = None) -> str:
    """Monospace aligned table that survives Telegram and mixed RTL text."""
    data = ([list(headers)] if headers else []) + [list(str(c) for c in row) for row in rows]
    if not data:
        return ""
    widths = [max(len(str(row[i])) for row in data if i < len(row)) for i in range(max(len(r) for r in data))]
    lines = []
    for index, row in enumerate(data):
        cells = [str(cell).ljust(widths[i]) for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
        if index == 0 and headers:
            lines.append("  ".join("-" * width for width in widths))
    return pre("\n".join(lines))




