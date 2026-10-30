"""Markdown that a chat model writes, turned into Telegram HTML safely.

The problem this solves
-----------------------
A model does not send plain sentences: it sends ``**bold**``, ``*italics*``,
``> a quote``, `` `code` `` and fenced blocks, because that is how it was trained
to write. Every other part of this bot builds Telegram HTML from escaped pieces,
so that text arrived *escaped* — the owner saw the asterisks. Her formatting was
correct and invisible, which looks exactly like a model that cannot format.

How it stays safe
-----------------
Input is **escaped first**, then a small, fixed set of patterns is recognised in
the escaped text and replaced by real tags. Nothing in the message can introduce
markup of its own: a ``<`` is already ``&lt;`` before any pattern runs, so the
only tags in the output are the ones this module added. Patterns are also
line-bounded and non-greedy, so an unbalanced ``*`` produces a literal asterisk
instead of swallowing the rest of the message.

What it recognises
------------------
``**bold**`` · ``__bold__`` · ``*italic*`` · ``_italic_`` · ``~~strike~~`` ·
``||spoiler||`` · `` `code` `` · fenced blocks · ``> quote`` · ``# heading`` ·
``- bullet`` / ``1. numbered``, plus three of the project's own marks:
``>> whisper`` (a collapsible quote), ``++underline++`` and ``^^small caps^^`` /
``%%spaced%%`` for the typographic variants Telegram has no entity for. Persian
text is left alone where markdown would mangle it: underscores inside a word
(``خیلی_خوب``) are not italic markers, and a lone ``*`` in the middle of a
sentence stays a character.

Feature area: 30 (formatting toolkit), 96 (presentation).
"""

from __future__ import annotations

import re

from .formatting import escape
from .typeface import small_caps, spaced

#: Telegram rejects a message whose tags do not nest; this module cannot produce
#: one, but the tests check that anyway.
__all__ = ["rich", "plain_preview", "looks_formatted"]

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]{0,16})\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]{1,400})`")
_BOLD_STAR_RE = re.compile(r"\*\*(?=\S)([^\n]{1,600}?)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<![\w])__(?=\S)([^\n]{1,600}?)__(?![\w])")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]{1,400}?)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w])_(?=\S)([^_\n]{1,400}?)_(?![\w])")
_STRIKE_RE = re.compile(r"~~(?=\S)([^\n]{1,400}?)~~")
_SPOILER_RE = re.compile(r"\|\|(?=\S)([^\n]{1,400}?)\|\|")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_QUOTE_RE = re.compile(r"(?m)^\s{0,3}&gt;\s?(.*)$")
#: ``>> line`` is a quote the reader has to tap to open. Telegram supports it as
#: an attribute of <blockquote>, so nothing new is sent — only the attribute.
_WHISPER_RE = re.compile(r"(?m)^\s{0,3}&gt;&gt;\s?(.*)$")
_UNDERLINE_RE = re.compile(r"\+\+(?=\S)([^+\n]{1,200}?)\+\+")
_SMALL_RE = re.compile(r"\^\^(?=\S)([^^\n]{1,60}?)\^\^")
_SPACED_RE = re.compile(r"%%(?=\S)([^%\n]{1,60}?)%%")
_BULLET_RE = re.compile(r"(?m)^\s{0,3}[-*+]\s+(.+)$")
_NUMBERED_RE = re.compile(r"(?m)^\s{0,3}(\d{1,3})[.)]\s+(.+)$")

#: An emphasis run must contain at least one letter or digit. Persian kaomoji
#: (``╰(*´︶`*)╯``) are made of stars and glyphs, and a model that ends a line with
#: one must not have it turned into half an italic tag.
_WORD_RE = re.compile(r"\w", re.UNICODE)


def _emphasise(tag: str):
    """Replacement factory: wrap ``group(1)`` in ``tag``, or leave it alone."""

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if not _WORD_RE.search(inner):
            return match.group(0)
        return f"<{tag}>{inner}</{tag}>"

    return _replace


#: Glyphs used for the two block styles that Telegram has no entity for.
QUOTE_MARK = "▏"
BULLET = "•"


def looks_formatted(text: str) -> bool:
    """True when ``text`` carries any markdown worth rendering."""
    if not text:
        return False
    return any(
        marker in text
        for marker in ("**", "__", "`", "~~", "||", "++", "^^", "%%", "> ", "# ", "\n- ", "\n* ", "\n1. ")
    ) or bool(re.search(r"(?m)^(\s{0,3}[-*+#>]|\s{0,3}\d{1,3}[.)]\s)", text))


def rich(text: str) -> str:
    """Telegram HTML for a model-written message. Never raises."""
    if not text:
        return ""
    # Escape first: from here on, every ``<`` in the string is already ``&lt;``
    # and only the tags added below are real.
    body = escape(text)

    # Fenced blocks first, so their contents are never treated as markdown.
    placeholders: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        language = match.group(1).strip()
        # The newline before the closing fence is a delimiter, not content: a
        # block that keeps it renders with a blank last line.
        code = match.group(2).rstrip("\n")
        tag = (
            f'<pre><code class="language-{language}">{code}</code></pre>'
            if language
            else f"<pre>{code}</pre>"
        )
        placeholders.append(tag)
        return f"\x00{len(placeholders) - 1}\x00"

    body = _FENCE_RE.sub(_stash, body)
    del _stash

    # Inline code next: a star inside `code` must stay a star.
    inline_stash: list[str] = []

    def _inline(match: re.Match[str]) -> str:
        inline_stash.append(f"<code>{match.group(1)}</code>")
        return f"\x01{len(inline_stash) - 1}\x01"

    body = _INLINE_CODE_RE.sub(_inline, body)

    body = _BOLD_STAR_RE.sub(_emphasise("b"), body)
    body = _BOLD_UNDER_RE.sub(_emphasise("b"), body)
    body = _ITALIC_STAR_RE.sub(_emphasise("i"), body)
    body = _ITALIC_UNDER_RE.sub(_emphasise("i"), body)
    body = _STRIKE_RE.sub(_emphasise("s"), body)
    body = _SPOILER_RE.sub(_emphasise("tg-spoiler"), body)
    body = _UNDERLINE_RE.sub(_emphasise("u"), body)
    # These two are not tags: the substitution *is* the styling.
    body = _SMALL_RE.sub(lambda m: small_caps(m.group(1)), body)
    body = _SPACED_RE.sub(lambda m: spaced(m.group(1)), body)

    # Block styles, line by line. A heading becomes a bold line, a quote becomes
    # a Telegram blockquote, a bullet gets a glyph.
    lines: list[str] = []
    quote_buffer: list[str] = []
    whisper_buffer: list[str] = []

    def flush_quote() -> None:
        if quote_buffer:
            lines.append("<blockquote>" + "\n".join(quote_buffer) + "</blockquote>")
            quote_buffer.clear()
        if whisper_buffer:
            lines.append(
                '<blockquote expandable>' + "\n".join(whisper_buffer) + "</blockquote>"
            )
            whisper_buffer.clear()

    for line in body.split("\n"):
        heading = _HEADING_RE.match(line)
        if heading is not None:
            flush_quote()
            lines.append(f"<b>{heading.group(1)}</b>")
            continue
        whisper = _WHISPER_RE.match(line)
        if whisper is not None:
            if quote_buffer:
                flush_quote()
            whisper_buffer.append(whisper.group(1))
            continue
        quote = _QUOTE_RE.match(line)
        if quote is not None:
            if whisper_buffer:
                flush_quote()
            quote_buffer.append(quote.group(1))
            continue
        flush_quote()
        bullet = _BULLET_RE.match(line)
        if bullet is not None:
            lines.append(f"{BULLET} {bullet.group(1)}")
            continue
        numbered = _NUMBERED_RE.match(line)
        if numbered is not None:
            lines.append(f"{numbered.group(1)}. {numbered.group(2)}")
            continue
        lines.append(line)
    flush_quote()
    body = "\n".join(lines)

    # Put the stashed pieces back, innermost last.
    for index, snippet in enumerate(inline_stash):
        body = body.replace(f"\x01{index}\x01", snippet)
    for index, snippet in enumerate(placeholders):
        body = body.replace(f"\x00{index}\x00", snippet)
    return body


def plain_preview(text: str, limit: int = 80) -> str:
    """The rendered message as text, for logs and transcripts."""
    return re.sub(r"<[^>]{1,64}>", "", rich(text))[:limit]
