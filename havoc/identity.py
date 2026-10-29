"""Who the bot is: Yura (یورا).

The codename is user-visible in exactly two places — the bot's display name and
the text it prints — so it lives here instead of being scattered as string
literals. ``HAVOC`` survives as an internal name for the codebase, the package,
the environment variables and the on-disk files, because renaming those would
break every deployment that already exists and buy nothing a user can see.
"""

from __future__ import annotations

#: What the bot calls itself, per language.
NAMES: dict[str, str] = {
    "en": "Yura",
    "fa": "یورا",
}

#: Short line that follows the name in a greeting or an about screen.
TAGLINES: dict[str, str] = {
    "en": "your assistant in Telegram — a little too caring, never a stranger",
    "fa": "دستیار تو در تلگرام — کمی زیادی مهربان، ولی هیچ‌وقت غریبه",
}

#: How to print the bot's name inside a message of the given language.
def name(language: str = "en") -> str:
    return NAMES.get(language, NAMES["en"])


def tagline(language: str = "en") -> str:
    return TAGLINES.get(language, TAGLINES["en"])


def display(language: str = "en") -> str:
    """``Yura`` for English, ``یورا`` for Farsi — never both at once."""
    return name(language)


def bilingual() -> str:
    """The form used in documentation and one-off notices."""
    return f"{NAMES['en']} / {NAMES['fa']}"
