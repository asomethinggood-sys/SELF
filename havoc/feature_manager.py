"""Feature registry with dependencies and a global emergency stop.

A *feature* is a user-visible capability (``autoreply``, ``clock``, ...) that
can be switched on and off independently. Commands belong to features, so
disabling a feature disables its commands' side effects while keeping help
readable.

Default state is **off** for anything that sends messages, changes the
profile, or talks to a third-party service (spec §9). Dependencies are
declared, checked and reported instead of failing mysteriously.

Feature areas: 7, 91, 96.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .services.storage import DEFAULT_FEATURES


@dataclass(frozen=True)
class Feature:
    name: str
    group: str
    default_enabled: bool = False
    #: Other feature names or service tokens (``ai``, ``translation``).
    dependencies: tuple[str, ...] = ()
    #: Safe to run while the emergency stop is active (read-only).
    read_only: bool = False
    title_key: str = ""
    description_key: str = ""
    commands: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return self.title_key or f"feature.{self.name}.title"

    @property
    def description(self) -> str:
        return self.description_key or f"feature.{self.name}.description"


@dataclass
class FeatureState:
    feature: Feature
    enabled: bool
    available: bool = True
    missing: tuple[str, ...] = ()


class FeatureManager:
    def __init__(self, store: Any, *, config: Any = None, ai_available: bool = False, translation_available: bool = False) -> None:
        self.store = store
        self.config = config
        self._features: dict[str, Feature] = {}
        self.ai_available = ai_available
        self.translation_available = translation_available
        self._cache: dict[str, bool] = {}
        self._stopped = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, feature: Feature) -> Feature:
        self._features[feature.name] = feature
        return feature

    def register_many(self, features: Iterable[Feature]) -> None:
        for feature in features:
            self.register(feature)

    def get(self, name: str) -> Feature | None:
        return self._features.get(name)

    def names(self) -> list[str]:
        return sorted(self._features)

    def by_group(self) -> dict[str, list[Feature]]:
        groups: dict[str, list[Feature]] = {}
        for feature in self._features.values():
            groups.setdefault(feature.group, []).append(feature)
        for items in groups.values():
            items.sort(key=lambda f: f.name)
        return groups

    # ------------------------------------------------------------------
    # Dependency checks
    # ------------------------------------------------------------------
    def missing_dependencies(self, name: str, seen: set[str] | None = None) -> tuple[str, ...]:
        feature = self._features.get(name)
        if feature is None:
            return ()
        seen = seen or set()
        missing: list[str] = []
        for dep in feature.dependencies:
            if dep == "ai" and not self.ai_available:
                missing.append("ai")
                continue
            if dep == "translation" and not self.translation_available:
                missing.append("translation")
                continue
            if dep in self._features:
                if dep in seen:
                    continue
                seen.add(dep)
                if not self._enabled_sync(dep):
                    missing.append(dep)
                else:
                    missing.extend(
                        m for m in self.missing_dependencies(dep, seen) if m not in missing
                    )
        return tuple(missing)

    def _enabled_sync(self, name: str) -> bool:
        """Best-effort synchronous view backed by the async-loaded cache."""
        if self._stopped:
            return False
        return self._cache.get(name, DEFAULT_FEATURES.get(name, False))

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    async def refresh(self) -> dict[str, bool]:
        stored = await self.store.features()
        for name, feature in self._features.items():
            stored.setdefault(name, feature.default_enabled)
        self._cache = stored
        self._stopped = await self.store.emergency_stop()
        return stored

    async def ensure_defaults(self) -> None:
        for name, feature in self._features.items():
            await self.store.db.execute(
                "INSERT OR IGNORE INTO features(name, enabled, updated_at) VALUES(?, ?, datetime('now'))",
                (name, 1 if feature.default_enabled else 0),
            )
        await self.refresh()

    async def is_enabled(self, name: str) -> bool:
        if await self.store.emergency_stop():
            feature = self._features.get(name)
            return bool(feature and feature.read_only)
        if not self._cache:
            await self.refresh()
        return bool(self._cache.get(name, DEFAULT_FEATURES.get(name, False)))

    async def is_available(self, name: str) -> FeatureState:
        feature = self._features.get(name) or Feature(name=name, group="other")
        enabled = await self.is_enabled(name)
        missing = self.missing_dependencies(name)
        return FeatureState(feature=feature, enabled=enabled, available=not missing, missing=missing)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._features:
            raise KeyError(name)
        if enabled:
            missing = self.missing_dependencies(name)
            if missing:
                raise ValueError(f"missing dependencies: {', '.join(missing)}")
        await self.store.set_feature(name, enabled)
        self._cache[name] = enabled

    async def toggle(self, name: str) -> bool:
        new_state = not await self.is_enabled(name)
        await self.set_enabled(name, new_state)
        return new_state

    async def snapshot(self) -> list[FeatureState]:
        states: list[FeatureState] = []
        for name in self.names():
            states.append(await self.is_available(name))
        return states

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------
    async def stop_all(self) -> None:
        await self.store.set_emergency_stop(True)
        self._stopped = True

    async def resume(self) -> None:
        await self.store.set_emergency_stop(False)
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    async def validate(self) -> list[str]:
        problems: list[str] = []
        snapshot = await self.snapshot()
        for state in snapshot:
            if state.enabled and not state.available:
                problems.append(
                    f"feature '{state.feature.name}' is enabled but missing: {', '.join(state.missing)}"
                )
        return problems

    @property
    def features(self) -> dict[str, Feature]:
        return dict(self._features)


def builtin_features(config: Any = None, *, ai_available: bool = False, translation_available: bool = False) -> list[Feature]:
    """Declarative catalogue of HAVOC features."""
    return [
        Feature("autoreply", "messaging", False, (), False, commands=("autoreply",)),
        Feature("away", "messaging", False, (), False, commands=("away",)),
        Feature("triggers", "detection", False, (), False, commands=("trigger",)),
        Feature("watches", "detection", False, (), False, commands=("watch",)),
        Feature("reactions", "messaging", False, (), False, commands=("react",)),
        Feature("notifications", "detection", False, (), False, commands=("detect",)),
        Feature("reminder_intent", "detection", False, (), False, commands=()),
        Feature("auto_translate", "messaging", False, ("translation",), False, commands=()),
        Feature("clock", "profile", False, (), False, commands=("clock",)),
        Feature("bio_rotation", "profile", False, (), False, commands=("bio",)),
        Feature("profile_scheduler", "profile", False, (), False, commands=("preset", "profile")),
        Feature("backups", "operations", False, (), False, commands=("backup",)),
        Feature("summaries", "information", False, (), True, commands=("summarize",)),
        # Sends messages in the owner's own chat, so it is off until asked for.
        Feature("persona", "persona", False, (), False, commands=("yura",)),
        Feature("media", "persona", False, (), False, commands=("pic", "sticker")),
        # Reading a poem or drawing a fal is always available offline; these
        # switches exist so the whole family can be turned off in one gesture.
        Feature("poetry", "fun", False, (), True, commands=("poem", "fal")),
        Feature("roasts", "fun", False, (), True, commands=("roast",)),
    ]
