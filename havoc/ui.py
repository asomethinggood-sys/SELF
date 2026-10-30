"""The look of everything Yura says.

Telegram HTML gives us bold, italic, code, pre, blockquote, spoiler and links —
more than enough to make output feel designed instead of dumped. The rules this
module enforces:

* **Never hand-format inside a feature.** Every message goes through these
  helpers, so the whole bot has one voice and one spacing rhythm.
* **Escape first, style second.** All text is escaped before it is wrapped, so a
  chat title or a note body containing ``<`` cannot break the message.
* **Keep it scannable.** A title, then sections with a small icon and a line of
  items, then a quiet italic footer. No walls of labels.

Feature area: 30 (message formatting toolkit), 63 (Persian display).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence

from .utils.formatting import bold, code, escape, italic, pre, quote

#: Section icons, one per command category. Deliberately geometric rather than
#: emoji soup: consistent weight, works in every client.
CATEGORY_ICONS: dict[str, str] = {
    "core": "◈",
    "profile": "◐",
    "messaging": "✉",
    "detection": "◉",
    "information": "◇",
    "scheduling": "◷",
    "management": "⚙",
    "fun": "✦",
    "language": "⌘",
    "utilities": "⌬",
    "persona": "♡",
    "media": "▣",
}

DOT = "·"
LINE = "━" * 12


def title(*parts: object) -> str:
    """``❥ **Yura** · commands`` — the first line of any screen.

    The first part is usually an icon and the second a translated header. Those
    headers often carry their own emoji (``⏰ Reminders``), which would print as
    ``⏰ · ⏰ Reminders``; the duplicate leading symbol is dropped here instead of
    asking every locale to be written twice.
    """
    # The icon (first part) is the screen's marker and is never rewritten; any
    # later part loses a leading symbol so a header that carries its own emoji
    # cannot print as "⏰ · ⏰ Reminders".
    cleaned: list[object] = []
    for index, part in enumerate(parts):
        text = str(part) if index == 0 else _strip_leading_symbol(str(part))
        if text.strip():
            cleaned.append(text if text.startswith("<") else bold(text))
    return f" {DOT} ".join(str(piece) for piece in cleaned)


#: Symbols a translated header may start with. Stripping one keeps
#: ``title(icon, header)`` from printing the same glyph twice.
_LEADING_SYMBOLS = set(
    "❥♡✦✧❦☾☀⚙⚔⚗⚯⛁✚✎✉▣▤▦▫◈◐◉◎●○◍◔◫◷⌖⌕⌾⌫⌬⋮⊕⊗⊞⊹∑№⟐⟰↩↪↻❀❐❏❝❞⚠■□▶▷◆◇⌘⌬ϟ✹❧≋⏚⧉ᴀ"
)


def _strip_leading_symbol(text: str) -> str:
    """Drop a leading emoji/symbol and the space after it, if there is one."""
    stripped = text.lstrip()
    index = 0
    while index < len(stripped) and stripped[index] in _LEADING_SYMBOLS:
        index += 1
    while index < len(stripped) and stripped[index] == "\ufe0f":
        index += 1
    if index and (index == len(stripped) or stripped[index].isspace()):
        return stripped[index:].lstrip()
    return text


def heading(icon: str, text: str) -> str:
    return f"{icon} {bold(text)}" if icon else bold(text)


def section(icon: str, name: str, items: Sequence[str] = ()) -> str:
    """A heading plus its items, separated by a blank line from the caller."""
    body = [heading(icon, name)]
    body.extend(items)
    return "\n".join(body)


def item(name: str, description: str = "", *, alias: str = "", marker: str = "") -> str:
    """One command line: ``.help · Show help  (alias: ?)``."""
    label = code(name)
    line = f"{marker + ' ' if marker else ''}{label}"
    if description:
        line += f" {italic(DOT)} {escape(description)}"
    if alias:
        line += f"  {italic('(' + alias + ')')}"
    return line


#: Icons for the kinds of scheduled work, so a list is scannable at a glance.
KIND_ICONS: dict[str, str] = {
    "reminder": "◷",
    "schedule": "✉",
    "job": "⚙",
    "task": "•",
}


def icon(kind: str, default: str = "•") -> str:
    return KIND_ICONS.get(str(kind).lower(), default)


def note(text: object) -> str:
    return italic(text)


def panel(lines: Iterable[str]) -> str:
    """Group related lines in a blockquote — the bot's card look.

    The content is treated as **already formatted HTML** (built from ``bold``,
    ``code``, ``note`` …). Passing raw user text here would leak its markup, so
    escape it first.
    """
    return f"<blockquote>{chr(10).join(str(line) for line in lines)}</blockquote>"


def footer(text: object) -> str:
    """The quiet hint at the bottom of a screen."""
    return f"<blockquote>{italic(text)}</blockquote>"


def rule() -> str:
    return italic(LINE)


def panel_text(lines: Iterable[str]) -> str:
    """Like :func:`panel` but for plain text, which is escaped for you."""
    return quote("\n".join(str(line) for line in lines))


def split_sections(sections: Sequence[str]) -> str:
    """Join blocks with a blank line, dropping the empty ones."""
    return "\n\n".join(section for section in sections if str(section).strip())


def quote_card(
    text: str, author: str = "", *, expandable: bool = False, italics: bool = False
) -> str:
    """A poem or quote: the body in a blockquote, the author right under it.

    ``author`` is printed in italics on its own line, which is the shape people
    expect from a quotation — and it keeps Farsi and Latin names readable in the
    same message.
    """
    body = italic(escape(text)) if italics else escape(text)
    block = f"<blockquote expandable>{body}</blockquote>" if expandable else f"<blockquote>{body}</blockquote>"
    if not author:
        return block
    return f"{block}\n{italic('— ' + author)}"


def meter(value: int, total: int, width: int = 8) -> str:
    """A tiny inline bar for ``4/9`` style facts."""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * value / total)))
    return "▰" * filled + "▱" * (width - filled)


def state(enabled: bool, *, on: str = "on", off: str = "off") -> str:
    """● on / ⚪ off, used consistently everywhere."""
    return f"● {escape(on)}" if enabled else f"⚪ {escape(off)}"


def code_block(lines: Iterable[str]) -> str:
    return pre("\n".join(str(line) for line in lines))


def numbered(options: Sequence[str], *, start: int = 1) -> str:
    """A menu: ``1 · Yura's voice`` so a reply of ``1`` is an obvious answer."""
    return "\n".join(f"{code(str(index))} {italic(DOT)} {option}" for index, option in enumerate(options, start))


# ---------------------------------------------------------------------------
# The immersive card
# ---------------------------------------------------------------------------
#: The line the owner asked for: a drawn rule made of Arabic tatweel, the one
#: character that renders as a clean horizontal stroke in every client — Latin
#: box-drawing characters look thin on Android and disappear on some fonts.
RULE_CHAR = "ـ"
#: Glyphs that sit *in* the rule, and glyphs that close a card. Kept apart from the
#: kaomoji box: these are typographic, not faces.
RULE_GLYPHS: tuple[str, ...] = ("◈", "❖", "⟡", "✦", "✧", "❦", "⌁", "⟐", "◉", "❈", "⊹", "⌘")
CLOSING_GLYPHS: tuple[str, ...] = (
    "☾", "✧", "❧", "◗", "⁘", "⌬", "❈", "⟡", "⸙", "◈", "⊹", "❖", "᯽", "✶",
)
#: Recently used, so two cards in a row never open on the same mark. Two windows,
#: not one: a closing glyph must not be able to "spend" a rule mark, or a short
#: run of cards starts repeating long before the pools are exhausted.
_recent_rules: deque[str] = deque(maxlen=8)
_recent_ends: deque[str] = deque(maxlen=8)


def _pick(pool: Sequence[str], *, avoid: Iterable[str] = (), recent: deque[str] | None = None) -> str:
    """A glyph from ``pool`` that has not been used lately, else the oldest one."""
    window = recent if recent is not None else _recent_rules
    blocked = set(avoid) | set(window)
    for candidate in pool:
        if candidate not in blocked:
            window.append(candidate)
            return candidate
    choice = window[0] if window else pool[0]
    window.append(choice)
    return choice


def drawn_rule(line: str = "", *, width: int = 9, glyph: str = "") -> str:
    """``ـــ⟡ـــ`` — a drawn line, optionally with a mark in the middle.

    ``line`` (a word or two) is printed *after* the rule in bold, which is the
    shape the owner drew: the mark, the rule, the name of the thing.
    """
    mark = glyph or _pick(RULE_GLYPHS)
    drawn = f"{RULE_CHAR * width}{mark}{RULE_CHAR * width}"
    if not line:
        return bold(drawn)
    return f"{bold(drawn)}  {bold(line)}"


def mark() -> str:
    """A fresh rule mark, for a header drawn outside :func:`card`."""
    return _pick(RULE_GLYPHS, recent=_recent_rules)


def end_mark() -> str:
    """A fresh closing glyph, for the last line of a screen."""
    return _pick(CLOSING_GLYPHS, recent=_recent_ends)


def closing(glyph: str = "") -> str:
    """The one-glyph signature at the bottom of a card."""
    return escape(glyph or _pick(CLOSING_GLYPHS, recent=_recent_ends))


def card(
    title: str,
    lines: Sequence[str] = (),
    *,
    icon: str = "◈",
    glyph: str = "",
    end: str = "",
    footer: str = "",
) -> str:
    """A designed action card: mark, bold title, drawn rule, italic facts, signature.

    The shape, exactly as asked for:

    ``◈ **Tool called**``
    ``ـــ⟡ـــ``
    ``*Reminder set for 30 minutes*``
    ``*You'll be reminded in the same chat*``
    ``☾``

    Every part is escaped here, so callers pass words, not markup. The rule mark
    and the closing glyph are different on every card — that is the whole point of
    a signature: it should look like it was drawn for this one message.
    """
    mark = glyph or _pick(RULE_GLYPHS, recent=_recent_rules)
    body = [f"{escape(icon)} {bold(title)}" if icon else bold(title)]
    body.append(drawn_rule(width=9, glyph=mark))
    body.extend(italic(line) for line in lines if str(line).strip())
    tail = closing(end)
    body.append(tail)
    if footer:
        body.append(footer_block(footer))
    return "\n".join(body)


def footer_block(text: object) -> str:
    """A quieter closing note, indented by a mark rather than another paragraph."""
    return f"<blockquote>{italic(text)}</blockquote>"


def fact_line(label: str, value: object) -> str:
    """``⏱ 30 minutes`` — an italic label and a plain value, for card bodies."""
    return f"{italic(label + ' ' + DOT)} {escape(value)}"
