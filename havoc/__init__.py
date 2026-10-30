"""HAVOC — a bilingual (English/Farsi) modular Telegram userbot.

The package is deliberately import-light at the top level so that tests and
tooling can import submodules (config, localization, database, ...) without
pulling in Telethon or performing network access.
"""

from __future__ import annotations

__version__ = "1.6"
__all__ = ["__version__"]

# Persistence is verified on every boot by `havoc data --boot-probe`.
