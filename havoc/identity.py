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

import re

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


#: How a sentence addresses her when it is not just her name: a vocative, a
#: greeting, or the punctuation people put around a name they are calling.
_VOCATIVE = r"(?:^|[\s،,؛;:.!?…\-—])(?:{names})(?:\s*(?:جان|جون|عزیزم|darling|honey|babe|baby|sweetie|dear))?(?=[\s،,؛;:.!?…\-—]|$)"
_ADDRESS_RE = re.compile(
    _VOCATIVE.format(names="|".join(re.escape(name) for name in sorted(ADDRESSES, key=len, reverse=True))),
    re.IGNORECASE,
)


def strip_address(text: str) -> str:
    """``text`` with her name taken out of it, spacing tidied.

    "yura what's the time in Tokyo" is a sentence with a vocative, and the
    instruction inside it is "what's the time in Tokyo". Without this the
    sentence floor saw her own name first and read the whole thing as chatter —
    so calling her by name, which is the only way she is called, disabled every
    ability she has.

    A sentence that is *only* her name is returned unchanged: that is a greeting,
    not an empty instruction.
    """
    raw = str(text or "")
    stripped = _ADDRESS_RE.sub(" ", raw)
    tidied = re.sub(r"\s{2,}", " ", stripped)
    # "hey yura, send hi" loses the name and leaves the comma hanging in front
    # of the instruction; nobody writes "hey , send hi".
    tidied = re.sub(r"\s+([،,؛;:.!?…])", r"\1", tidied)
    tidied = tidied.strip(" ،,؛;:.!?…-—")
    return tidied or raw.strip()
