"""Typed repository layer over SQLite.

Every feature talks to :class:`Store`, never to ``sqlite3`` directly. That
keeps SQL parameterized and in one place, and makes the storage backend
replaceable later (feature requirement in §2/§8).

Value encoding
--------------
Booleans are stored as ``0``/``1``, datetimes as UTC ISO strings, structured
values (lists, dicts, quiet-hour config) as JSON in TEXT columns. Decoders are
tolerant: a corrupt row yields a default and a logged warning rather than a
crash, and callers can always see the failure via ``.store.decode_errors``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..database import Database, DatabaseError, Row
from ..utils.time import iso, parse_iso, utc_now

# ---------------------------------------------------------------------------
# Feature switch defaults (all OFF by default — spec §9)
# ---------------------------------------------------------------------------
DEFAULT_FEATURES: dict[str, bool] = {
    "autoreply": False,
    "away": False,
    "triggers": False,
    "watches": False,
    "reactions": False,
    "notifications": False,
    "clock": False,
    "bio_rotation": False,
    "profile_scheduler": False,
    "backups": False,
    "reminder_intent": False,
    "summaries": False,
    "auto_translate": False,
    # On by default: it only ever answers the owner's own messages in the
    # owner's own chat, which is the whole point of the companion. Every other
    # automation stays off until asked for. `.yura off` silences it.
    "persona": True,
    "media": False,
    "poetry": False,
    "roasts": False,
}

#: Human-readable grouping used by ``.features`` and the docs.
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "messaging": ("autoreply", "away", "triggers", "reactions", "auto_translate"),
    "detection": ("watches", "notifications", "reminder_intent"),
    "profile": ("clock", "bio_rotation", "profile_scheduler"),
    "operations": ("backups", "summaries"),
    "persona": ("persona", "media"),
    "fun": ("poetry", "roasts"),
}


@dataclass
class SettingRecord:
    key: str
    value: str
    updated_at: datetime | None


#: Numeric strings are stored and read back as numbers.
_NUMBER_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _encode(value: Any) -> str:
    """Store settings as text while keeping their type recoverable.

    Plain strings are written verbatim (so the database stays readable and old
    rows keep working); booleans, numbers and containers use JSON syntax.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _decode(raw: str | None, default: Any = None) -> Any:
    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    stripped = raw.strip()
    if stripped.startswith(("{", "[", '"', "t", "f", "n", "-")) or _NUMBER_RE.match(stripped):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return raw
    return raw


def _as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class Store:
    """High-level data access used by all features."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.decode_errors: list[str] = []

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self.db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return default if row is None else _decode(row["value"], default)

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, _encode(value), iso(utc_now())),
        )

    async def delete_setting(self, key: str) -> None:
        await self.db.execute("DELETE FROM settings WHERE key = ?", (key,))

    async def all_settings(self) -> dict[str, Any]:
        rows = await self.db.fetch_all("SELECT key, value FROM settings ORDER BY key")
        return {row["key"]: _decode(row["value"]) for row in rows}

    async def get_bool(self, key: str, default: bool = False) -> bool:
        return _as_bool(await self.get_setting(key, "1" if default else "0"), default)

    # ------------------------------------------------------------------
    # Per-chat settings
    # ------------------------------------------------------------------
    async def get_chat_setting(self, chat_id: int, key: str, default: Any = None) -> Any:
        row = await self.db.fetch_one(
            "SELECT value FROM chat_settings WHERE chat_id = ? AND key = ?", (chat_id, key)
        )
        return default if row is None else _decode(row["value"], default)

    async def set_chat_setting(self, chat_id: int, key: str, value: Any) -> None:
        await self.db.execute(
            """
            INSERT INTO chat_settings(chat_id, key, value, updated_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (chat_id, key, _encode(value), iso(utc_now())),
        )

    async def delete_chat_setting(self, chat_id: int, key: str | None = None) -> int:
        if key is None:
            return await self.db.execute("DELETE FROM chat_settings WHERE chat_id = ?", (chat_id,))
        return await self.db.execute(
            "DELETE FROM chat_settings WHERE chat_id = ? AND key = ?", (chat_id, key)
        )

    async def chat_settings(self, chat_id: int) -> dict[str, Any]:
        rows = await self.db.fetch_all(
            "SELECT key, value FROM chat_settings WHERE chat_id = ? ORDER BY key", (chat_id,)
        )
        return {row["key"]: _decode(row["value"]) for row in rows}

    # ------------------------------------------------------------------
    # Language preferences
    # ------------------------------------------------------------------
    async def set_language(self, scope: str, scope_id: str, language: str) -> None:
        await self.db.execute(
            """
            INSERT INTO language_prefs(scope, scope_id, language, updated_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(scope, scope_id) DO UPDATE SET language = excluded.language, updated_at = excluded.updated_at
            """,
            (scope, scope_id, language, iso(utc_now())),
        )

    async def get_language(self, scope: str, scope_id: str = "") -> str | None:
        row = await self.db.fetch_one(
            "SELECT language FROM language_prefs WHERE scope = ? AND scope_id = ?", (scope, scope_id)
        )
        return row["language"] if row else None

    async def language_prefs(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM language_prefs ORDER BY scope, scope_id")

    # ------------------------------------------------------------------
    # Feature switches
    # ------------------------------------------------------------------
    async def ensure_features(self) -> None:
        for name, enabled in DEFAULT_FEATURES.items():
            await self.db.execute(
                "INSERT OR IGNORE INTO features(name, enabled, updated_at) VALUES(?, ?, ?)",
                (name, 1 if enabled else 0, iso(utc_now())),
            )

    async def set_feature(self, name: str, enabled: bool) -> None:
        await self.db.execute(
            """
            INSERT INTO features(name, enabled, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (name, 1 if enabled else 0, iso(utc_now())),
        )

    async def feature_enabled(self, name: str) -> bool:
        row = await self.db.fetch_one("SELECT enabled FROM features WHERE name = ?", (name,))
        if row is None:
            return DEFAULT_FEATURES.get(name, False)
        return bool(row["enabled"])

    async def features(self) -> dict[str, bool]:
        rows = await self.db.fetch_all("SELECT name, enabled FROM features ORDER BY name")
        out = {row["name"]: bool(row["enabled"]) for row in rows}
        for name, default in DEFAULT_FEATURES.items():
            out.setdefault(name, default)
        return out

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------
    async def emergency_stop(self) -> bool:
        return await self.get_bool("automation.stopped", False)

    async def set_emergency_stop(self, stopped: bool) -> None:
        await self.set_setting("automation.stopped", stopped)

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    async def add_template(self, name: str, body: str, language: str = "auto") -> int:
        return await self.db.insert(
            "INSERT INTO templates(name, body, language, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (name, body, language, iso(utc_now()), iso(utc_now())),
        )

    async def update_template(self, name: str, body: str) -> int:
        return await self.db.execute(
            "UPDATE templates SET body = ?, updated_at = ? WHERE name = ?",
            (body, iso(utc_now()), name),
        )

    async def get_template(self, name: str) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM templates WHERE name = ?", (name,))

    async def list_templates(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM templates ORDER BY name")

    async def delete_template(self, name: str) -> int:
        return await self.db.execute("DELETE FROM templates WHERE name = ?", (name,))

    # ------------------------------------------------------------------
    # Auto replies
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Outbox, plans and the people she must not answer
    # ------------------------------------------------------------------
    async def add_outbox(
        self,
        chat_id: int,
        *,
        message_id: int | None = None,
        text: str = "",
        kind: str = "text",
        status: str = "sent",
        note: str | None = None,
        plan_id: int | None = None,
    ) -> int:
        """Record one outbound thing. A send with no row here did not happen."""
        return await self.db.insert(
            """
            INSERT INTO outbox(chat_id, message_id, kind, text, status, note, plan_id, sent_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(chat_id), message_id, kind, text[:4000], status, note, plan_id, iso(utc_now())),
        )

    async def outbox(self, limit: int = 10, *, chat_id: int | None = None) -> list[Any]:
        if chat_id is None:
            return await self.db.fetch_all(
                "SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            )
        return await self.db.fetch_all(
            "SELECT * FROM outbox WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (int(chat_id), max(1, int(limit))),
        )

    async def outbox_row(self, row_id: int) -> Any:
        return await self.db.fetch_one("SELECT * FROM outbox WHERE id = ?", (int(row_id),))

    async def mark_deleted(self, row_id: int, *, note: str = "") -> bool:
        changed = await self.db.execute(
            "UPDATE outbox SET status = 'deleted', deleted_at = ?, note = COALESCE(NULLIF(?, ''), note) WHERE id = ?",
            (iso(utc_now()), note, int(row_id)),
        )
        return bool(changed)

    async def count_outbox(self, *, since_seconds: int = 86400) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM outbox WHERE sent_at >= ?",
            (iso(utc_now() - timedelta(seconds=since_seconds)),),
        )
        return int(row["n"]) if row else 0

    # -- plans ---------------------------------------------------------
    async def add_plan(
        self, *, owner_id: int | None, label: str, said: str, status: str = "pending"
    ) -> int:
        return await self.db.insert(
            "INSERT INTO plans(owner_id, label, said, status, created_at) VALUES(?, ?, ?, ?, ?)",
            (owner_id, label[:200], said[:2000], status, iso(utc_now())),
        )

    async def add_plan_step(self, plan_id: int, idx: int, at: str, action: str) -> int:
        return await self.db.insert(
            "INSERT INTO plan_steps(plan_id, idx, at, action, status) VALUES(?, ?, ?, ?, 'pending')",
            (int(plan_id), int(idx), at, action[:500]),
        )

    async def plan(self, plan_id: int) -> Any:
        return await self.db.fetch_one("SELECT * FROM plans WHERE id = ?", (int(plan_id),))

    async def plan_steps(self, plan_id: int) -> list[Any]:
        return await self.db.fetch_all(
            "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY idx", (int(plan_id),)
        )

    async def plans(self, limit: int = 10, *, status: str | None = None) -> list[Any]:
        if status is None:
            return await self.db.fetch_all(
                "SELECT * FROM plans ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            )
        return await self.db.fetch_all(
            "SELECT * FROM plans WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, max(1, int(limit))),
        )

    async def newest_plan(self, *, pending_only: bool = True) -> Any:
        if pending_only:
            return await self.db.fetch_one(
                "SELECT * FROM plans WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
            )
        return await self.db.fetch_one("SELECT * FROM plans ORDER BY id DESC LIMIT 1")

    async def find_plan(self, needle: str) -> Any:
        """A plan by number or by a word of its label/sentence."""
        text = str(needle or "").strip().lstrip("#")
        if text.isdigit():
            return await self.plan(int(text))
        like = f"%{text.lower()}%"
        return await self.db.fetch_one(
            "SELECT * FROM plans WHERE LOWER(label) LIKE ? OR LOWER(said) LIKE ? ORDER BY id DESC LIMIT 1",
            (like, like),
        )

    async def close_plan(self, plan_id: int, status: str, *, note: str = "") -> bool:
        changed = await self.db.execute(
            "UPDATE plans SET status = ?, stopped_at = ?, note = ? WHERE id = ? AND status = 'pending'",
            (status, iso(utc_now()), note[:300], int(plan_id)),
        )
        return bool(changed)

    async def step_fired(self, step_id: int, *, status: str = "done", note: str = "") -> bool:
        """Claim a step. One runner wins; the other sees ``False``."""
        changed = await self.db.execute(
            "UPDATE plan_steps SET status = ?, fired_at = ?, note = ? WHERE id = ? AND status = 'pending'",
            (status, iso(utc_now()), note[:300], int(step_id)),
        )
        return bool(changed)

    async def pending_steps(self, plan_id: int) -> list[Any]:
        return await self.db.fetch_all(
            "SELECT * FROM plan_steps WHERE plan_id = ? AND status = 'pending' ORDER BY idx",
            (int(plan_id),),
        )

    # -- people she must not answer ------------------------------------
    async def ignore_person(self, user_id: int, name: str = "", reason: str = "") -> None:
        await self.db.execute(
            """
            INSERT INTO ignored(user_id, name, reason, created_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, reason = excluded.reason
            """,
            (int(user_id), name[:120], reason[:200], iso(utc_now())),
        )

    async def unignore_person(self, user_id: int) -> bool:
        changed = await self.db.execute("DELETE FROM ignored WHERE user_id = ?", (int(user_id),))
        return bool(changed)

    async def is_ignored(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        row = await self.db.fetch_one("SELECT user_id FROM ignored WHERE user_id = ?", (int(user_id),))
        return row is not None

    async def ignored_people(self) -> list[Any]:
        return await self.db.fetch_all("SELECT * FROM ignored ORDER BY created_at DESC")

    async def add_autoreply(
        self,
        pattern: str,
        response: str,
        *,
        match_type: str = "contains",
        language: str = "auto",
        scope_chat_id: int | None = None,
        cooldown: int = 300,
        enabled: bool = True,
        scope: str = "any",
    ) -> int:
        if scope not in {"any", "private", "group"}:
            raise ValueError(f"unknown autoreply scope: {scope!r}")
        if match_type == "any":
            # A standing instruction replaces the one it supersedes. "If anyone
            # messages me say X" then "…say Y" is a correction, not two rules that
            # both fire and race — the newest wins, and the old one is disabled
            # rather than deleted so `.autoreply list` still shows what changed.
            await self.db.execute(
                "UPDATE autoreplies SET enabled = 0 WHERE match_type = 'any' AND scope = ? AND enabled = 1",
                (scope,),
            )
        return await self.db.insert(
            """
            INSERT INTO autoreplies(pattern, match_type, response, language, scope_chat_id, cooldown, enabled, scope, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pattern, match_type, response, language, scope_chat_id, cooldown, int(enabled), scope, iso(utc_now())),
        )

    async def list_autoreplies(self, scope_chat_id: int | None = None, enabled_only: bool = False) -> list[Row]:
        sql = "SELECT * FROM autoreplies"
        clauses: list[str] = []
        params: list[Any] = []
        if scope_chat_id is not None:
            clauses.append("(scope_chat_id IS NULL OR scope_chat_id = ?)")
            params.append(scope_chat_id)
        if enabled_only:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return await self.db.fetch_all(sql, params)

    async def set_autoreply_enabled(self, rule_id: int, enabled: bool) -> int:
        return await self.db.execute(
            "UPDATE autoreplies SET enabled = ? WHERE id = ?", (int(enabled), rule_id)
        )

    async def delete_autoreply(self, rule_id: int) -> int:
        return await self.db.execute("DELETE FROM autoreplies WHERE id = ?", (rule_id,))

    async def clear_autoreplies(self) -> int:
        return await self.db.execute("DELETE FROM autoreplies")

    # ------------------------------------------------------------------
    # Schedules (messages) and reminders
    # ------------------------------------------------------------------
    async def add_schedule(
        self,
        chat_id: int,
        text: str,
        run_at: datetime,
        *,
        recurrence: str | None = None,
        timezone_name: str = "UTC",
        created_by: int | None = None,
        kind: str = "message",
        meta: dict[str, Any] | None = None,
    ) -> int:
        return await self.db.insert(
            """
            INSERT INTO schedules(kind, chat_id, text, run_at, recurrence, timezone, enabled, created_by, created_at, meta)
            VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                kind,
                chat_id,
                text,
                iso(run_at),
                recurrence,
                timezone_name,
                created_by,
                iso(utc_now()),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )

    async def list_schedules(self, enabled_only: bool = False) -> list[Row]:
        sql = "SELECT * FROM schedules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY run_at"
        return await self.db.fetch_all(sql)

    async def due_schedules(self, now: datetime, limit: int = 50) -> list[Row]:
        return await self.db.fetch_all(
            "SELECT * FROM schedules WHERE enabled = 1 AND run_at <= ? ORDER BY run_at LIMIT ?",
            (iso(now), limit),
        )


    async def get_schedule(self, schedule_id: int) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))

    async def mark_schedule_run(
        self,
        schedule_id: int,
        *,
        slot: str,
        next_run: datetime | None,
        error: str | None = None,
    ) -> None:
        """Idempotently record a run.

        The ``last_slot`` guard means two workers (or a reconnect racing a
        restart) cannot send the same slot twice: the UPDATE only applies when
        the stored slot differs.
        """
        if next_run is None:
            await self.db.execute(
                """
                UPDATE schedules SET last_run_at = ?, last_slot = ?, enabled = 0, last_error = ?
                WHERE id = ? AND (last_slot IS NULL OR last_slot != ?)
                """,
                (iso(utc_now()), slot, error, schedule_id, slot),
            )
        else:
            await self.db.execute(
                """
                UPDATE schedules SET last_run_at = ?, last_slot = ?, run_at = ?, last_error = ?
                WHERE id = ? AND (last_slot IS NULL OR last_slot != ?)
                """,
                (iso(utc_now()), slot, iso(next_run), error, schedule_id, slot),
            )

    async def cancel_schedule(self, schedule_id: int) -> bool:
        changed = await self.db.execute(
            "UPDATE schedules SET enabled = 0 WHERE id = ? AND enabled = 1", (schedule_id,)
        )
        return changed > 0

    async def reschedule(self, schedule_id: int, run_at: datetime) -> int:
        return await self.db.execute(
            "UPDATE schedules SET run_at = ?, last_error = NULL WHERE id = ?", (iso(run_at), schedule_id)
        )

    async def add_reminder(
        self,
        chat_id: int,
        text: str,
        remind_at: datetime,
        *,
        recurrence: str | None = None,
        timezone_name: str = "UTC",
        created_by: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        return await self.db.insert(
            """
            INSERT INTO reminders(chat_id, text, remind_at, recurrence, timezone, status, created_by, created_at, meta)
            VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                chat_id,
                text,
                iso(remind_at),
                recurrence,
                timezone_name,
                created_by,
                iso(utc_now()),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )

    async def list_reminders(
        self,
        *,
        status: str | None = "pending",
        chat_id: int | None = None,
        limit: int = 50,
    ) -> list[Row]:
        sql = "SELECT * FROM reminders"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY remind_at LIMIT ?"
        params.append(limit)
        return await self.db.fetch_all(sql, params)

    async def get_reminder(self, reminder_id: int) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))

    async def due_reminders(self, now: datetime, limit: int = 50) -> list[Row]:
        return await self.db.fetch_all(
            "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= ? ORDER BY remind_at LIMIT ?",
            (iso(now), limit),
        )


    async def complete_reminder(self, reminder_id: int, *, next_at: datetime | None = None, slot: str | None = None) -> bool:
        if next_at is None:
            changed = await self.db.execute(
                "UPDATE reminders SET status = 'done', last_fired_at = ?, last_slot = ? WHERE id = ? AND status = 'pending'",
                (iso(utc_now()), slot, reminder_id),
            )
        else:
            changed = await self.db.execute(
                """
                UPDATE reminders SET remind_at = ?, last_fired_at = ?, last_slot = ?
                WHERE id = ? AND status = 'pending' AND (last_slot IS NULL OR last_slot != ?)
                """,
                (iso(next_at), iso(utc_now()), slot, reminder_id, slot or ""),
            )
        return changed > 0

    async def update_reminder(self, reminder_id: int, *, text: str | None = None, remind_at: datetime | None = None) -> int:
        sets: list[str] = []
        params: list[Any] = []
        if text is not None:
            sets.append("text = ?")
            params.append(text)
        if remind_at is not None:
            sets.append("remind_at = ?")
            params.append(iso(remind_at))
        if not sets:
            return 0
        params.append(reminder_id)
        return await self.db.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id = ?", params)

    async def cancel_reminder(self, reminder_id: int) -> bool:
        changed = await self.db.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (reminder_id,)
        )
        return changed > 0

    # ------------------------------------------------------------------
    # Watches / triggers / reactions
    # ------------------------------------------------------------------
    async def add_watch(
        self,
        keyword: str,
        *,
        scope_chat_id: int | None = None,
        notify_chat_id: int | None = None,
        language: str = "auto",
    ) -> int:
        return await self.db.insert(
            "INSERT INTO watches(keyword, scope_chat_id, notify_chat_id, language, enabled, created_at) VALUES(?, ?, ?, ?, 1, ?)",
            (keyword, scope_chat_id, notify_chat_id, language, iso(utc_now())),
        )

    async def list_watches(self, enabled_only: bool = True) -> list[Row]:
        sql = "SELECT * FROM watches"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return await self.db.fetch_all(sql)

    async def delete_watch(self, watch_id: int) -> int:
        return await self.db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))

    async def add_trigger(
        self,
        name: str,
        pattern: str,
        *,
        event_type: str = "message",
        match_type: str = "contains",
        action_type: str = "notify",
        payload: str | None = None,
        scope_chat_id: int | None = None,
        language: str = "auto",
    ) -> int:
        return await self.db.insert(
            """
            INSERT INTO triggers(name, event_type, pattern, match_type, action_type, payload,
                                 scope_chat_id, language, enabled, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (name, event_type, pattern, match_type, action_type, payload, scope_chat_id, language, iso(utc_now())),
        )

    async def list_triggers(self, enabled_only: bool = True) -> list[Row]:
        sql = "SELECT * FROM triggers"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return await self.db.fetch_all(sql)

    async def set_trigger_enabled(self, trigger_id: int, enabled: bool) -> int:
        return await self.db.execute(
            "UPDATE triggers SET enabled = ? WHERE id = ?", (int(enabled), trigger_id)
        )

    async def delete_trigger(self, trigger_id: int) -> int:
        return await self.db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))

    async def add_reaction_rule(
        self,
        pattern: str,
        emoji: str,
        *,
        match_type: str = "contains",
        scope_chat_id: int | None = None,
        cooldown: int = 60,
    ) -> int:
        return await self.db.insert(
            """
            INSERT INTO reaction_rules(pattern, match_type, emoji, scope_chat_id, cooldown, enabled, created_at)
            VALUES(?, ?, ?, ?, ?, 1, ?)
            """,
            (pattern, match_type, emoji, scope_chat_id, cooldown, iso(utc_now())),
        )

    async def list_reaction_rules(self, enabled_only: bool = True) -> list[Row]:
        sql = "SELECT * FROM reaction_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return await self.db.fetch_all(sql)

    async def delete_reaction_rule(self, rule_id: int) -> int:
        return await self.db.execute("DELETE FROM reaction_rules WHERE id = ?", (rule_id,))

    # ------------------------------------------------------------------
    # Bookmarks / notes
    # ------------------------------------------------------------------
    async def add_bookmark(
        self,
        chat_id: int,
        *,
        message_id: int | None = None,
        kind: str = "message",
        title: str | None = None,
        content: str | None = None,
        sender_id: int | None = None,
        labels: Sequence[str] | None = None,
    ) -> int:
        return await self.db.insert(
            """
            INSERT INTO bookmarks(chat_id, message_id, kind, title, content, sender_id, labels, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                message_id,
                kind,
                title,
                content,
                sender_id,
                json.dumps(list(labels or []), ensure_ascii=False),
                iso(utc_now()),
            ),
        )

    async def list_bookmarks(self, *, chat_id: int | None = None, limit: int = 50) -> list[Row]:
        if chat_id is None:
            return await self.db.fetch_all(
                "SELECT * FROM bookmarks ORDER BY id DESC LIMIT ?", (limit,)
            )
        return await self.db.fetch_all(
            "SELECT * FROM bookmarks WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )

    async def search_bookmarks(self, query: str, limit: int = 20) -> list[Row]:
        pattern = f"%{query}%"
        return await self.db.fetch_all(
            """
            SELECT * FROM bookmarks
            WHERE title LIKE ? OR content LIKE ? OR labels LIKE ?
            ORDER BY id DESC LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        )

    async def get_bookmark(self, bookmark_id: int) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM bookmarks WHERE id = ?", (bookmark_id,))

    async def delete_bookmark(self, bookmark_id: int) -> int:
        return await self.db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))

    async def add_note(
        self,
        chat_id: int,
        body: str,
        *,
        title: str | None = None,
        labels: Sequence[str] | None = None,
    ) -> int:
        return await self.db.insert(
            "INSERT INTO notes(chat_id, title, body, labels, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                title,
                body,
                json.dumps(list(labels or []), ensure_ascii=False),
                iso(utc_now()),
                iso(utc_now()),
            ),
        )

    async def list_notes(self, chat_id: int | None = None, limit: int = 50) -> list[Row]:
        if chat_id is None:
            return await self.db.fetch_all("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        return await self.db.fetch_all(
            "SELECT * FROM notes WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
        )

    async def search_notes(self, query: str, limit: int = 20) -> list[Row]:
        pattern = f"%{query}%"
        return await self.db.fetch_all(
            "SELECT * FROM notes WHERE title LIKE ? OR body LIKE ? ORDER BY id DESC LIMIT ?",
            (pattern, pattern, limit),
        )

    async def delete_note(self, note_id: int) -> int:
        return await self.db.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    # ------------------------------------------------------------------
    # Aliases
    # ------------------------------------------------------------------
    async def add_alias(self, alias: str, target: str) -> None:
        await self.db.execute(
            """
            INSERT INTO aliases(alias, target, created_at) VALUES(?, ?, ?)
            ON CONFLICT(alias) DO UPDATE SET target = excluded.target
            """,
            (alias, target, iso(utc_now())),
        )

    async def resolve_alias(self, alias: str, max_depth: int = 5) -> str | None:
        """Follow alias chains & reject loops (feature area 79)."""
        current = alias
        seen: set[str] = set()
        for _ in range(max_depth):
            row = await self.db.fetch_one("SELECT target FROM aliases WHERE alias = ?", (current,))
            if row is None:
                return current if current != alias else None
            target = row["target"]
            if target in seen or target == alias:
                return None
            seen.add(target)
            current = target
        return None

    async def list_aliases(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM aliases ORDER BY alias")

    async def delete_alias(self, alias: str) -> int:
        return await self.db.execute("DELETE FROM aliases WHERE alias = ?", (alias,))

    async def clear_aliases(self) -> int:
        return await self.db.execute("DELETE FROM aliases")

    # ------------------------------------------------------------------
    # Counters / stats / diagnostics
    # ------------------------------------------------------------------
    async def increment_counter(self, name: str, by: int = 1) -> int:
        await self.db.execute(
            """
            INSERT INTO counters(name, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET value = counters.value + excluded.value, updated_at = excluded.updated_at
            """,
            (name, by, iso(utc_now())),
        )
        return int(await self.db.scalar("SELECT value FROM counters WHERE name = ?", (name,), 0))

    async def get_counter(self, name: str) -> int:
        return int(await self.db.scalar("SELECT value FROM counters WHERE name = ?", (name,), 0) or 0)

    async def reset_counter(self, name: str) -> None:
        await self.db.execute(
            "UPDATE counters SET value = 0, updated_at = ? WHERE name = ?", (iso(utc_now()), name)
        )

    async def bump_usage(self, feature: str, counter: str = "calls", by: int = 1) -> None:
        await self.db.execute(
            """
            INSERT INTO usage_stats(feature, counter, value, updated_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(feature, counter) DO UPDATE SET value = usage_stats.value + excluded.value, updated_at = excluded.updated_at
            """,
            (feature, counter, by, iso(utc_now())),
        )

    async def usage_stats(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM usage_stats ORDER BY feature, counter")


    # ------------------------------------------------------------------
    # Profile snapshots / presets / history
    # ------------------------------------------------------------------
    async def save_profile_snapshot(self, field: str, value: str | None) -> None:
        await self.db.execute(
            """
            INSERT INTO profile_snapshots(field, value, captured_at) VALUES(?, ?, ?)
            ON CONFLICT(field) DO UPDATE SET value = excluded.value, captured_at = excluded.captured_at
            """,
            (field, value, iso(utc_now())),
        )

    async def get_profile_snapshot(self, field: str) -> str | None:
        row = await self.db.fetch_one("SELECT value FROM profile_snapshots WHERE field = ?", (field,))
        return row["value"] if row else None


    async def add_profile_preset(
        self,
        name: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        bio: str | None = None,
    ) -> int:
        return await self.db.insert(
            """
            INSERT INTO profile_presets(name, first_name, last_name, bio, created_at) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET first_name = excluded.first_name,
                last_name = excluded.last_name, bio = excluded.bio
            """,
            (name, first_name, last_name, bio, iso(utc_now())),
        )

    async def get_profile_preset(self, name: str) -> Row | None:
        return await self.db.fetch_one("SELECT * FROM profile_presets WHERE name = ?", (name,))

    async def list_profile_presets(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM profile_presets ORDER BY name")

    async def delete_profile_preset(self, name: str) -> int:
        return await self.db.execute("DELETE FROM profile_presets WHERE name = ?", (name,))

    async def add_profile_history(self, field: str, old: str | None, new: str | None, reason: str = "") -> int:
        return await self.db.insert(
            "INSERT INTO profile_history(field, old_value, new_value, reason, created_at) VALUES(?, ?, ?, ?, ?)",
            (field, old, new, reason, iso(utc_now())),
        )

    async def profile_history(self, limit: int = 10) -> list[Row]:
        return await self.db.fetch_all(
            "SELECT * FROM profile_history ORDER BY id DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------------
    # Chat labels
    # ------------------------------------------------------------------
    async def add_chat_label(self, chat_id: int, label: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO chat_labels(chat_id, label, created_at) VALUES(?, ?, ?)",
            (chat_id, label, iso(utc_now())),
        )

    async def chat_labels(self, chat_id: int) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT label FROM chat_labels WHERE chat_id = ? ORDER BY label", (chat_id,)
        )
        return [row["label"] for row in rows]

    async def remove_chat_label(self, chat_id: int, label: str) -> int:
        return await self.db.execute(
            "DELETE FROM chat_labels WHERE chat_id = ? AND label = ?", (chat_id, label)
        )

    # ------------------------------------------------------------------
    # Plugins
    # ------------------------------------------------------------------
    async def record_plugin(self, name: str, version: str | None, enabled: bool = True, error: str | None = None) -> None:
        await self.db.execute(
            """
            INSERT INTO plugins(name, version, enabled, error, loaded_at) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET version = excluded.version, enabled = excluded.enabled,
                error = excluded.error, loaded_at = excluded.loaded_at
            """,
            (name, version, int(enabled), error, iso(utc_now())),
        )

    async def list_plugins(self) -> list[Row]:
        return await self.db.fetch_all("SELECT * FROM plugins ORDER BY name")

    async def set_plugin_enabled(self, name: str, enabled: bool) -> int:
        return await self.db.execute(
            "UPDATE plugins SET enabled = ? WHERE name = ?", (int(enabled), name)
        )

    # ------------------------------------------------------------------
    # Dedupe cache (bounded)
    # ------------------------------------------------------------------
    async def seen_recently(self, key: str, within_seconds: int) -> bool:
        if within_seconds <= 0:
            return False
        cutoff = iso(utc_now() - timedelta(seconds=within_seconds))
        row = await self.db.fetch_one(
            "SELECT key FROM dedupe WHERE key = ? AND seen_at >= ?", (key, cutoff)
        )
        return row is not None

    async def mark_seen(self, key: str) -> None:
        await self.db.execute(
            """
            INSERT INTO dedupe(key, seen_at) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET seen_at = excluded.seen_at
            """,
            (key, iso(utc_now())),
        )

    async def prune_dedupe(self, older_than_seconds: int = 86400) -> int:
        cutoff = iso(utc_now() - timedelta(seconds=older_than_seconds))
        return await self.db.execute("DELETE FROM dedupe WHERE seen_at < ?", (cutoff,))

    # ------------------------------------------------------------------
    # Interactive flows (confirmation / multi-step setup)
    # ------------------------------------------------------------------
    async def set_interactive(self, user_id: int, flow: str, step: str, data: dict[str, Any] | None = None) -> None:
        await self.db.execute(
            """
            INSERT INTO interactive_sessions(user_id, flow, step, data, updated_at) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET flow = excluded.flow, step = excluded.step,
                data = excluded.data, updated_at = excluded.updated_at
            """,
            (user_id, flow, step, json.dumps(data or {}, ensure_ascii=False), iso(utc_now())),
        )

    async def get_interactive(self, user_id: int, max_age_seconds: int = 600) -> Row | None:
        row = await self.db.fetch_one("SELECT * FROM interactive_sessions WHERE user_id = ?", (user_id,))
        if row is None:
            return None
        updated = parse_iso(row["updated_at"])
        if updated and (utc_now() - updated).total_seconds() > max_age_seconds:
            await self.clear_interactive(user_id)
            return None
        return row

    async def clear_interactive(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM interactive_sessions WHERE user_id = ?", (user_id,))

    # ------------------------------------------------------------------
    # Backup / restore support
    # ------------------------------------------------------------------
    BACKUP_TABLES: tuple[str, ...] = (
        "settings",
        "chat_settings",
        "language_prefs",
        "features",
        "templates",
        "autoreplies",
        "schedules",
        "reminders",
        "watches",
        "triggers",
        "reaction_rules",
        "bookmarks",
        "notes",
        "aliases",
        "counters",
        "profile_presets",
        "profile_snapshots",
        "chat_labels",
        "plugins",
    )

    async def export_tables(self, tables: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        """Dump selected tables as JSON-serializable dicts.

        Sessions, credentials and message histories are not stored in these
        tables at all, so a backup can never contain them.
        """
        result: dict[str, list[dict[str, Any]]] = {}
        for table in tables or self.BACKUP_TABLES:
            if table not in self.BACKUP_TABLES:
                continue
            rows = await self.db.fetch_all(f"SELECT * FROM {table}")  # fixed allowlist, no user input
            result[table] = [dict(row) for row in rows]
        return result

    async def import_tables(self, payload: dict[str, list[dict[str, Any]]], *, replace: bool = False) -> dict[str, int]:
        """Import a validated backup payload.

        Every row is inserted with an explicit column allowlist; unknown keys
        are ignored, so a malicious backup cannot inject SQL or extra columns.
        """
        counts: dict[str, int] = {}
        for table, rows in payload.items():
            if table not in self.BACKUP_TABLES or not isinstance(rows, list):
                continue
            columns = await self.db.fetch_all(f"PRAGMA table_info({table})")
            allowed = {row["name"] for row in columns}
            if replace:
                await self.db.execute(f"DELETE FROM {table}")
            inserted = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                cols = [key for key in row if key in allowed]
                if not cols:
                    continue
                placeholders = ", ".join("?" for _ in cols)
                sql = f"INSERT OR REPLACE INTO {table}({', '.join(cols)}) VALUES({placeholders})"
                try:
                    await self.db.execute(sql, [row[col] for col in cols])
                    inserted += 1
                except DatabaseError:
                    continue
            counts[table] = inserted
        return counts
