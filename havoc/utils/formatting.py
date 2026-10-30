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


def escape(text: object) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape("" if text is None else str(text), quote=False)


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
    """Sanitize and split a message for sending."""
    return [chunk for chunk in split_message(sanitize_output(text), limit) if chunk]


_HTML_TAG_RE = re.compile(r"<[^>]{1,64}>")


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




