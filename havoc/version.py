"""Single source of truth for the runtime version string.

The name people see is **Y.U.R.A** — the letters are the point, not an
abbreviation of anything in particular — and the number is what the owner calls
it, not what the schema calls it. ``SCHEMA``-style numbers live in
:mod:`havoc.migrations`; this file is only ever the label on the box.
"""

from __future__ import annotations

VERSION = "1.9"
#: What users see. "HAVOC" is the codebase name and stays out of the chat.
CODENAME = "Y.U.R.A"
#: The word after the number on the about card. It is honest: the hands are new.
STAGE = "Open beta"


def label() -> str:
    """``Y.U.R.A 1.9 Open beta`` — one spelling, everywhere."""
    return f"{CODENAME} {VERSION} {STAGE}"
