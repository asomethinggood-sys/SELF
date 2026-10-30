"""Access control, allow/deny lists and quiet hours.

Policy (deny wins, then allow):
1. A chat in the denylist never gets automation, even if allowlisted.
2. A non-empty allowlist means *only* those chats get automation.
3. Private chats are allowed by default only with ``automation.allow_private``
   (default ON, because a personal userbot mainly chats privately) — this is
   explicit and resettable.
4. The emergency stop disables *acting* (sending, profile changes) while
   leaving read-only commands available.

Commands themselves are owner-only by default: a userbot's account should not
run arbitrary commands for strangers.

Feature areas: 21, 22, 59, 71, 72, 96, 9.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils.time import in_quiet_hours, utc_now


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str = ""
    code: str = "allowed"

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


#: Where the handles in the config were last turned into ids. Persisted, because
#: ``get_entity`` is a network call and a message must never wait for one.
RESOLVED_SETTING = "owner.resolved"


class Permissions:
    def __init__(self, config: Any, store: Any) -> None:
        self.config = config
        self.store = store
        self._owner_ids: set[int] = set(getattr(config, "owner_ids", ()) or ())
        self._self_id: int | None = None
        #: Handles from the config, and the ids they were resolved to.
        self._handles: tuple[str, ...] = tuple(getattr(config, "owner_usernames", ()) or ())
        self._resolved: dict[str, int] = {}
        self._load_error = ""

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------
    def set_self_id(self, user_id: int | None) -> None:
        self._self_id = user_id

    @property
    def self_id(self) -> int | None:
        return self._self_id

    @property
    def owner_ids(self) -> set[int]:
        """Everyone who counts as an owner: configured ids, the account itself,
        and the ids the configured handles resolved to."""
        owners = set(self._owner_ids) | set(self._resolved.values())
        if self._self_id:
            owners.add(self._self_id)
        return owners

    @property
    def handles(self) -> tuple[str, ...]:
        return self._handles

    @property
    def handles_resolved(self) -> dict[str, int]:
        """``{"edited_by_nyx": 123456}`` — what the configured names became."""
        return dict(self._resolved)

    def is_owner(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        owners = self.owner_ids
        if not owners:
            # Before login we cannot verify anyone; commands are rejected.
            return False
        return user_id in owners

    # ------------------------------------------------------------------
    # Handles → ids
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """Read what was resolved last run, so the first message needs no network."""
        try:
            stored = await self.store.get_setting(RESOLVED_SETTING, {}) or {}
        except Exception as exc:  # noqa: BLE001 - an unreadable setting is not a lockout
            self._load_error = str(exc)
            return
        if not isinstance(stored, dict):
            return
        for handle, value in stored.items():
            try:
                self._resolved[str(handle).lower()] = int(value)
            except (TypeError, ValueError):
                continue

    def add_id(self, user_id: int, *, handle: str = "") -> bool:
        """Make somebody an owner, in memory and (via the caller) in the database."""
        try:
            value = int(user_id)
        except (TypeError, ValueError):
            return False
        self._owner_ids.add(value)
        if handle:
            self._resolved[handle.lstrip("@").lower()] = value
        return True

    def remove_id(self, user_id: int) -> bool:
        try:
            value = int(user_id)
        except (TypeError, ValueError):
            return False
        removed = value in self._owner_ids
        self._owner_ids.discard(value)
        for handle, resolved in list(self._resolved.items()):
            if resolved == value:
                self._resolved.pop(handle, None)
        return removed

    async def save(self) -> None:
        if self.store is None:
            return
        try:
            await self.store.set_setting(RESOLVED_SETTING, dict(self._resolved))
            await self.store.set_setting("owner.ids", sorted(self._owner_ids))
        except Exception:  # noqa: BLE001 - persistence is a convenience here
            return

    async def resolve_handles(self, client: Any) -> dict[str, int]:
        """Turn ``@edited_by_nyx`` into a number — once, at login.

        A failure is recorded and never raises: a network hiccup must not stop a
        bot from booting, and an unresolved handle simply cannot vouch for
        anyone until the next attempt.
        """
        if client is None or not self._handles:
            return {}
        found: dict[str, int] = {}
        for handle in self._handles:
            if handle in self._resolved:
                found[handle] = self._resolved[handle]
                continue
            try:
                entity = await client.get_entity(f"@{handle}")
            except Exception:  # noqa: BLE001 - unknown handle, network, flood
                continue
            user_id = getattr(entity, "id", None)
            if user_id is None:
                continue
            self._resolved[handle] = int(user_id)
            found[handle] = int(user_id)
        if found:
            await self.save()
        return found

    def is_self(self, user_id: int | None) -> bool:
        return user_id is not None and user_id == self._self_id

    # ------------------------------------------------------------------
    # Automation scope
    # ------------------------------------------------------------------
    async def automation_scope(self, chat_id: int, *, is_private: bool | None = None) -> PermissionDecision:
        if chat_id in self.config.automation_denylist:
            return PermissionDecision(False, "chat is denylisted", "denylisted")
        if self.config.automation_allowlist and chat_id not in self.config.automation_allowlist:
            return PermissionDecision(False, "chat is not in the allowlist", "not_allowlisted")
        chat_denied = await self.store.get_chat_setting(chat_id, "automation.denied", False)
        if _truthy(chat_denied):
            return PermissionDecision(False, "automation disabled for this chat", "chat_disabled")
        chat_allowed = await self.store.get_chat_setting(chat_id, "automation.allowed", None)
        if chat_allowed is not None:
            if not _truthy(chat_allowed):
                return PermissionDecision(False, "automation disabled for this chat", "chat_disabled")
            return PermissionDecision(True, code="chat_allowed")
        if is_private is False:
            return PermissionDecision(True, code="group_default")
        if is_private is True:
            allow_private = await self.store.get_bool("automation.allow_private", True)
            if not allow_private:
                return PermissionDecision(False, "private chats are not allowlisted", "private_disabled")
            return PermissionDecision(True, code="private_default")
        return PermissionDecision(True, code="default")

    async def can_act(self, chat_id: int, feature: str, *, is_private: bool | None = None) -> PermissionDecision:
        """Full check used before any automatic outbound action."""
        if await self.store.emergency_stop():
            return PermissionDecision(False, "emergency stop is active", "emergency_stop")
        if not await self.store.feature_enabled(feature):
            return PermissionDecision(False, f"feature '{feature}' is disabled", "feature_disabled")
        if await self.in_quiet_hours(chat_id):
            return PermissionDecision(False, "quiet hours are active", "quiet_hours")
        return await self.automation_scope(chat_id, is_private=is_private)

    # ------------------------------------------------------------------
    # Quiet hours
    # ------------------------------------------------------------------
    async def quiet_hours_config(self, chat_id: int | None = None) -> dict[str, str] | None:
        if chat_id is not None:
            per_chat = await self.store.get_chat_setting(chat_id, "quiet_hours", None)
            if per_chat:
                return per_chat
        return await self.store.get_setting("quiet_hours", None)

    async def in_quiet_hours(self, chat_id: int | None = None, *, moment=None) -> bool:
        config = await self.quiet_hours_config(chat_id)
        if not config:
            return False
        start = config.get("start")
        end = config.get("end")
        if not start or not end:
            return False
        zone = config.get("timezone") or self.config.timezone
        return in_quiet_hours(moment or utc_now(), start, end, zone)

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------
    async def dry_run_active(self) -> bool:
        """Dry-run can be toggled at runtime; config is the default."""
        override = await self.store.get_setting("runtime.dry_run", None)
        if override is None:
            return bool(self.config.dry_run)
        return _truthy(override)

    async def set_dry_run(self, enabled: bool) -> None:
        await self.store.set_setting("runtime.dry_run", bool(enabled))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
