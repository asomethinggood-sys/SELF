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


class Permissions:
    def __init__(self, config: Any, store: Any) -> None:
        self.config = config
        self.store = store
        self._owner_ids: set[int] = set(config.owner_ids)
        self._self_id: int | None = None

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
        if self._owner_ids:
            return set(self._owner_ids)
        return {self._self_id} if self._self_id else set()

    def is_owner(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        owners = self.owner_ids
        if not owners:
            # Before login we cannot verify anyone; commands are rejected.
            return False
        return user_id in owners

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
