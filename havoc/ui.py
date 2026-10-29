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
