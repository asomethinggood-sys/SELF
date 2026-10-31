"""Who the bot is: Y.U.R.A 1.9 (یورا).

The codename is user-visible in exactly two places — the bot's display name and
the text it prints — so it lives here instead of being scattered as string
literals. ``HAVOC`` survives as an internal name for the codebase, the package,
the environment variables and the on-disk files, because renaming those would
break every deployment that already exists and buy nothing a user can see.

Two names, on purpose:

* **Y.U.R.A 1.9** is the *display* name — the profile, the about card, the help
  header, the device Telegram records. It is a release, and it says so.
* **Yura** (``یورا``) is the *address*: what a person types to call her, and what
  she answers to. Nobody calls a friend "Y.U.R.A 1.9", and a reader that only
  knew the long name would stop understanding "yura, are you there".
"""

from __future__ import annotations

from .version import VERSION

#: The letters, spelled out — the way the name is written on the tin.
BRAND = "Y.U.R.A"
#: What the bot is called on its profile and in every heading. The number comes
#: from :mod:`havoc.version`, so the profile and ``--version`` cannot disagree.
DISPLAY_NAME = f"{BRAND} {VERSION}"

#: What it answers to, per language. Short, spoken, and never the version.
NAMES: dict[str, str] = {
    "en": "Yura",
    "fa": "یورا",
}

#: Every spelling that is still her name in a sentence: the spoken one, the
#: displayed one, and the dotted one people type on a keyboard.
ADDRESSES: tuple[str, ...] = (
    "yura",
    "y.u.r.a",
    "y.u.r.a 1.9",
    "یورا",
    "ی.و.ر.ا",
)

#: Short line that follows the name in a greeting or an about screen.
TAGLINES: dict[str, str] = {
    "en": "your assistant in Telegram — a little too caring, never a stranger",
    "fa": "دستیار تو در تلگرام — کمی زیادی مهربان، ولی هیچ‌وقت غریبه",
}


#: What she answers to when somebody says her name.
def name(language: str = "en") -> str:
    return NAMES.get(language, NAMES["en"])


#: What the profile says, in every language: the release name.
def display(language: str = "en") -> str:
    return DISPLAY_NAME


def version() -> str:
    return VERSION


def tagline(language: str = "en") -> str:
    return TAGLINES.get(language, TAGLINES["en"])


def answers_to(text: str) -> bool:
    """True when ``text`` says her name, however it is spelled."""
    lowered = str(text or "").lower()
    return any(address and address in lowered for address in ADDRESSES)
