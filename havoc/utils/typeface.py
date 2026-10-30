"""Typography: the part of her voice that is shape, not words.

Telegram gives a bot bold, italic, underline, strike, spoiler, code and
blockquotes — and nothing else. Everything that has to *look* like something
beyond that is Unicode: small capitals for a word she is mocking, spaced letters
for a whisper, a glyph bar for attention, a thin rule to end a message.

Two rules this module keeps:

* **Symbols, not colour emoji.** The owner's taste, and it survives a client
  that renders emoji differently.
* **Nothing here decides anything.** These are pure functions of text, so the
  tests can pin the exact output and a message can be rebuilt inside a test.
"""

from __future__ import annotations

import re

__all__ = [
    "BARS",
    "RULE",
    "mood_strip",
    "rule",
    "small_caps",
    "spaced",
    "time_band",
]

#: Filled and empty cells for a bar.
BARS = ("▱", "▰")
#: A thin rule to close a message. Not a dash: dashes look like typing.
RULE = "┄" * 9

_SMALL_CAPS = str.maketrans(
    {
        **dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ", strict=True)),
        # The handful of capitals that have a real small-cap form.
        "A": "ᴀ", "B": "ʙ", "C": "ᴄ", "D": "ᴅ", "E": "ᴇ", "F": "ꜰ", "G": "ɢ",
        "H": "ʜ", "I": "ɪ", "J": "ᴊ", "K": "ᴋ", "L": "ʟ", "M": "ᴍ", "N": "ɴ",
        "O": "ᴏ", "P": "ᴘ", "Q": "ǫ", "R": "ʀ", "S": "ꜱ", "T": "ᴛ", "U": "ᴜ",
        "V": "ᴠ", "W": "ᴡ", "X": "x", "Y": "ʏ", "Z": "ᴢ",
    }
)

#: Longest run either transform will touch. A whole message in small caps is
#: unreadable; a word or two is a tone of voice.
MAX_STYLED = 40


def small_caps(text: str) -> str:
    """``this`` → ``ᴛʜɪꜱ``. Latin only; Persian is left exactly as written."""
    body = text[:MAX_STYLED]
    if not re.search(r"[A-Za-z]", body):
        return text
    return body.translate(_SMALL_CAPS) + text[len(body):]


def spaced(text: str) -> str:
    """``quiet`` → ``q u i e t``. For a whisper, not for a sentence."""
    body = text[:MAX_STYLED]
    letters = [character for character in body if not character.isspace()]
    if len(letters) < 2:
        return text
    return " ".join(letters) + text[len(body):]


def rule(width: int = 9) -> str:
    """A thin rule, safely bounded."""
    return "┄" * max(3, min(24, width))


def mood_strip(level: float, label: str, width: int = 5) -> str:
    """``▰▰▰▱▱ attention · 60%`` — a bar she can put under a message."""
    ratio = min(1.0, max(0.0, float(level)))
    cells = max(1, width)
    filled = int(round(ratio * cells))
    bar = BARS[1] * filled + BARS[0] * (cells - filled)
    return f"{bar} {label} · {int(round(ratio * 100))}%"


#: The day in four bands. Boundaries are half-open: 05:00 is morning.
BANDS = ("night", "morning", "afternoon", "evening")


def time_band(hour: int) -> str:
    """Which part of the day ``hour`` belongs to."""
    hour = int(hour) % 24
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "evening"
    return "night"
