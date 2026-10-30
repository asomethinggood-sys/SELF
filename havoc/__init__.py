"""HAVOC — a bilingual (English/Farsi) modular Telegram userbot.

The package is deliberately import-light at the top level so that tests and
tooling can import submodules (config, localization, database, ...) without
pulling in Telethon or performing network access.
"""

from __future__ import annotations

from .version import VERSION as _VERSION

#: One number, one place. ``havoc/version.py`` owns the release; this is the
#: import everyone reaches for. Two copies is how the CLI said 1.6 while the about
#: card said something else.
__version__ = _VERSION
__all__ = ["__version__"]

# Persistence is verified on every boot by `havoc data --boot-probe`.
