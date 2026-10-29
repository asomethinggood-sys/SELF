"""Feature modules.

Each module exposes ``register(runtime)`` which registers its commands and
(where needed) its event subscribers. Order matters only for help output; all
features are independent and may be disabled at runtime.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

MODULE_ORDER: tuple[str, ...] = (
    "core",
    "settings_menu",
    "language",
    "utilities",
    "poetry",
    "banter",
    "scheduling",
    "information",
    "profile",
    "messaging",
    "persona",
    "media",
    "detection",
    "management",
    "automation",
)


def all_feature_modules() -> list[ModuleType]:
    """Import and return feature modules in registration order."""
    modules: list[ModuleType] = []
    for name in MODULE_ORDER:
        modules.append(import_module(f"{__name__}.{name}"))
    return modules


__all__ = ["MODULE_ORDER", "all_feature_modules"]
