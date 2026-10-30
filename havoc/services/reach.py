"""Reaching people: resolve a name, then send exactly what was asked for.

This is the hand behind every "tell him…", "send this to…", "react to that…".
It exists because a chat target is not a chat id: he says "Sara", "@hmmm_6",
"that group", "saved messages", "the third one in my DMs". Resolving that safely
is most of the work, and doing it in one place is the only way the answer to
"who did you send it to?" is ever the truth.

Rules this module holds itself to, all of them his:

* **Resolve, then look at it.** A name that matches two people is not a guess; it
  is a question.
* **A send is a row.** Every message that leaves this account is written to
  ``outbox`` with the chat, the message id and the text — or it did not happen.
* **Options are the sentence.** Repeats, spoilers, silence, self-destruct and
  "later" are not features bolted on afterwards; they are arguments to the same
  send, so every path (command, sentence, plan, plan-step) gets all of them.
* **Pacing is Telegram's.** A repeat waits; a flood waits longer; a refusal drops
  that target and says why.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ..utils.security import redact_text
from ..utils.time import TimeParseError, iso, parse_when, utc_now

#: How a target was understood. ``saved`` is Saved Messages, i.e. his own chat.
KINDS = ("saved", "user", "group", "channel", "unknown")

#: A repeat is a message, a pause and a message again. Telegram's own pace wins
#: when it is slower than this.
MIN_GAP_SECONDS = 1.0
#: Past this a "repeat 500 times" is a broadcast, and broadcasts are confirmed.
MAX_REPEAT = 500


class ReachError(Exception):
    """Something about the target or the text makes the send impossible."""

    def __init__(self, message: str, kind: str = "reach_error") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class Target:
    """A chat, resolved. ``title`` is for the human, ``id`` is the truth."""

    id: int
    kind: str = "user"
    title: str = ""
    username: str = ""

    @property
    def label(self) -> str:
        name = self.title or (f"@{self.username}" if self.username else "")
        return name or str(self.id)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "username": self.username}


@dataclass
class SendReport:
    """What a send did, in a shape a reply can describe."""

    ok: bool = True
    target: Target | None = None
    sent: int = 0
    message_ids: list[int] = field(default_factory=list)
    text: str = ""
    error: str = ""
    dry_run: bool = False
    scheduled_for: str = ""
    deletes_at: str = ""

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if self.scheduled_for:
            return f"scheduled for {self.scheduled_for}"
        if self.dry_run:
            return "dry run — nothing was sent"
        if self.sent:
            return f"sent ×{self.sent}" if self.sent > 1 else "sent"
        return "nothing to send"


# ---------------------------------------------------------------------------
# Parsing the little language of a send
# ---------------------------------------------------------------------------
_DURATION = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)?$", re.I)
_UNIT_SECONDS = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}


def parse_duration(raw: str) -> float | None:
    """``10``, ``10s``, ``2m``, ``1h`` → seconds. ``None`` when it is not one."""
    match = _DURATION.match(str(raw or "").strip())
    if not match:
        return None
    try:
        value = float(match.group("value"))
    except (TypeError, ValueError):
        return None
    return value * _UNIT_SECONDS.get((match.group("unit") or "").lower(), 1.0)


def spoiler(text: str) -> str:
    """Wrap text the way Telegram hides it behind a tap."""
    stripped = str(text or "")
    if not stripped.strip():
        return stripped
    return f'<span class="tg-spoiler">{stripped}</span>'


class Reach:
    """Resolve, send, react, unsend — and record all of it."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    # -- properties ----------------------------------------------------
    @property
    def store(self) -> Any:
        return self.runtime.store

    @property
    def sender(self) -> Any:
        return self.runtime.sender

    @property
    def client(self) -> Any:
        return getattr(self.runtime, "client", None)

    @property
    def self_id(self) -> int | None:
        return getattr(self.runtime.permissions, "self_id", None)

    # ------------------------------------------------------------------
    # Resolving a target
    # ------------------------------------------------------------------
    async def resolve(self, value: Any, *, search_dialogs: bool = True) -> Target | None:
        """Turn what he said into one chat, or ``None``.

        Order matters and is deliberate: an explicit id, then Saved Messages, then
        a stored alias, then a username, then a dialog whose name matches. A
        numeric string is an id and never a name, because a person called "999"
        is a coincidence and a chat id is not.
        """
        raw = str(value or "").strip().strip(",")
        if not raw:
            return None
        lowered = raw.lower().lstrip("@")
        self_id = self.self_id
        if lowered in {"me", "self", "saved", "saved messages", "savedmessages", "myself"}:
            return Target(id=int(self_id or 0), kind="saved", title="Saved Messages")
        if re.fullmatch(r"-?\d{4,}", raw):
            return Target(id=int(raw), kind="unknown")

        alias = None
        try:
            alias = await self.store.resolve_alias(lowered)
        except Exception:  # noqa: BLE001 - an alias table is a convenience
            alias = None
        if alias:
            raw = str(alias)
            if re.fullmatch(r"-?\d{4,}", raw):
                return Target(id=int(raw), kind="unknown")

        entity = await self._entity(lowered)
        if entity is not None:
            return self._target_from(entity)
        if search_dialogs:
            found = await self._search_dialogs(raw)
            if found is not None:
                return found
        return None

    async def resolve_or_raise(self, value: Any) -> Target:
        found = await self.resolve(value)
        if found is None:
            raise ReachError(f"there is no chat called {value!r}", kind="unknown_target")
        return found

    async def resolve_many(
        self, values: list[str], *, limit: int | None = None
    ) -> tuple[list[Target], list[str]]:
        """Several targets, in order, with the ones that could not be found."""
        targets: list[Target] = []
        missing: list[str] = []
        seen: set[int] = set()
        for value in values:
            found = await self.resolve(value)
            if found is None:
                missing.append(str(value))
                continue
            if found.id in seen:
                continue
            seen.add(found.id)
            targets.append(found)
            if limit is not None and len(targets) >= limit:
                break
        return targets, missing

    async def _entity(self, value: str) -> Any:
        if self.client is None:
            return None
        try:
            return await self.client.get_entity(value)
        except Exception:  # noqa: BLE001 - an unknown name is not an error yet
            return None

    def _target_from(self, entity: Any) -> Target:
        chat_id = getattr(entity, "id", None)
        username = getattr(entity, "username", None) or ""
        title = (
            getattr(entity, "title", None)
            or " ".join(
                part
                for part in (
                    getattr(entity, "first_name", "") or "",
                    getattr(entity, "last_name", "") or "",
                )
                if part
            ).strip()
            or (f"@{username}" if username else "")
        )
        if getattr(entity, "bot", False) and not title:
            title = "a bot"
        if isinstance(chat_id, int) and chat_id > 0 and not getattr(entity, "title", None):
            kind = "user"
        elif getattr(entity, "broadcast", False):
            kind = "channel"
        elif chat_id is not None:
            kind = "group"
        else:
            kind = "unknown"
        return Target(id=int(chat_id or 0), kind=kind, title=str(title), username=str(username))

    async def _search_dialogs(self, needle: str) -> Target | None:
        """A name that matches exactly one of his chats.

        Two matches are not a choice to make on his behalf: the caller gets
        ``None`` and :meth:`candidates` is what asks the question.
        """
        matches = await self.candidates(needle, limit=2)
        return matches[0] if len(matches) == 1 else None

    async def candidates(self, needle: str, *, limit: int = 10) -> list[Target]:
        if self.client is None or not needle.strip():
            return []
        dialogs = getattr(self.client, "get_dialogs", None)
        if dialogs is None:
            return []
        try:
            rows = await dialogs(limit=300)
        except Exception:  # noqa: BLE001 - offline or rate limited
            return []
        wanted = needle.strip().lower().lstrip("@")
        found: list[Target] = []
        for dialog in rows or []:
            entity = getattr(dialog, "entity", None) or dialog
            target = self._target_from(entity)
            haystack = f"{target.title} {target.username}".lower()
            if wanted and wanted in haystack:
                found.append(target)
            if len(found) >= limit:
                break
        return found

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def deliver(
        self,
        target: Target,
        text: str,
        *,
        spoiler_hidden: bool = False,
        silent: bool = False,
        reply_to: int | None = None,
        repeat: int = 1,
        every: float = 0.0,
        delete_after: float | None = None,
        at: Any = None,
        plan_id: int | None = None,
        dry_run: bool | None = None,
    ) -> SendReport:
        """Send ``text`` to ``target`` with every option the sentence asked for."""
        body = str(text or "")
        if not body.strip():
            raise ReachError("there is nothing to send", kind="empty")
        if spoiler_hidden:
            body = spoiler(body)
        repeat = max(1, min(int(repeat or 1), MAX_REPEAT))
        report = SendReport(target=target, text=body)

        # "later" is not a send: it is one job, and the job does the send.
        if at:
            if isinstance(at, str):
                try:
                    when = parse_when(at, self.runtime.config.timezone)
                except TimeParseError as exc:
                    raise ReachError(f"I could not read the time {at!r}", kind="bad_time") from exc
            else:
                when = at
            await self.runtime.scheduler.enqueue(
                "reach_send",
                when,
                {
                    "chat_id": target.id,
                    "text": body,
                    "silent": bool(silent),
                    "reply_to": reply_to,
                    "delete_after": delete_after,
                    "plan_id": plan_id,
                },
            )
            report.scheduled_for = iso(when)
            return report

        # A repeat is a first send and the rest on the clock, so a restart does
        # not lose the tail and no loop sleeps inside a reply.
        gaps = max(float(every or 0.0), 0.0)
        if repeat > 1:
            for index in range(1, repeat):
                when = utc_now() + timedelta(seconds=gaps * index)
                await self.runtime.scheduler.enqueue(
                    "reach_send",
                    when,
                    {
                        "chat_id": target.id,
                        "text": body,
                        "silent": bool(silent),
                        "reply_to": reply_to,
                        "delete_after": delete_after,
                        "plan_id": plan_id,
                    },
                )

        result = await self._send_once(target, body, silent=silent, reply_to=reply_to, dry_run=dry_run)
        report.sent = 1 if result.get("ok") and not result.get("dry_run") else 0
        report.dry_run = bool(result.get("dry_run"))
        report.message_ids = [result["message_id"]] if result.get("message_id") else []
        if not result.get("ok"):
            report.ok = False
            report.error = str(result.get("error") or "the send failed")
            return report
        if delete_after and report.message_ids:
            when = utc_now() + timedelta(seconds=float(delete_after))
            await self.runtime.scheduler.enqueue(
                "reach_unsend",
                when,
                {"chat_id": target.id, "message_id": report.message_ids[0], "outbox_id": result.get("outbox_id")},
            )
            report.deletes_at = iso(when)
        report.sent = report.sent or (repeat if repeat > 1 else 0)
        if repeat > 1:
            report.message_ids = report.message_ids  # the rest are queued
        return report

    async def _send_once(
        self,
        target: Target,
        body: str,
        *,
        silent: bool,
        reply_to: int | None,
        dry_run: bool | None,
    ) -> dict[str, Any]:
        chat_id: Any = target.id if target.id else "me"
        if target.kind == "saved":
            chat_id = "me"
        try:
            result = await self.sender.send(
                chat_id,
                body,
                reply_to=reply_to,
                silent=silent,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - a refused send is a report, not a crash
            self.runtime.health.record_error("reach", exc)
            await self.store.add_outbox(
                target.id, text=body, status="failed", note=redact_text(str(exc))[:300]
            )
            return {"ok": False, "error": redact_text(str(exc))}
        message = getattr(result, "message", None)
        message_id = getattr(message, "id", None)
        simulated = bool(getattr(result, "dry_run", False))
        recorded_id = int(target.id or (self.self_id or 0))
        outbox_id = await self.store.add_outbox(
            recorded_id,
            message_id=message_id,
            text=body,
            status="dry" if simulated else "sent",
            note=None if getattr(result, "ok", False) else str(getattr(result, "error", "") or ""),
        )
        if not getattr(result, "ok", False):
            return {"ok": False, "error": str(getattr(result, "error", "") or "the send failed")}
        return {"ok": True, "message_id": message_id, "dry_run": simulated, "outbox_id": outbox_id}

    async def send_later(self, payload: dict[str, Any]) -> None:
        """The scheduler's job: a send that was asked for earlier."""
        target = Target(
            id=int(payload.get("chat_id") or 0),
            kind="saved" if not payload.get("chat_id") else "unknown",
        )
        report = await self.deliver(
            target,
            str(payload.get("text") or ""),
            silent=bool(payload.get("silent")),
            reply_to=payload.get("reply_to"),
            delete_after=payload.get("delete_after"),
            plan_id=payload.get("plan_id"),
        )
        if not report.ok:
            self.runtime.logger.warning("scheduled send failed: %s", report.error)

    async def unsend(self, payload: dict[str, Any]) -> None:
        """The scheduler's job: delete a message that was told to self-destruct."""
        chat_id = int(payload.get("chat_id") or 0)
        message_id = payload.get("message_id")
        if not message_id:
            return
        await self.delete(chat_id, int(message_id))
        outbox_id = payload.get("outbox_id")
        if outbox_id:
            await self.store.mark_deleted(int(outbox_id), note="self-destruct")

    # ------------------------------------------------------------------
    # Everything else he can do to a message
    # ------------------------------------------------------------------
    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        return bool(await self.sender.react(chat_id, message_id, emoji))

    async def delete(self, chat_id: int, message_id: int) -> bool:
        try:
            result = await self.sender.delete(chat_id, [int(message_id)])
        except Exception as exc:  # noqa: BLE001 - already gone is fine
            self.runtime.logger.warning("unsend failed: %s", redact_text(str(exc)))
            return False
        return bool(getattr(result, "ok", False))

    async def edit(self, chat_id: int, message_id: int, text: str) -> bool:
        try:
            result = await self.sender.edit(chat_id, message_id, text)
        except Exception as exc:  # noqa: BLE001
            self.runtime.logger.warning("edit failed: %s", redact_text(str(exc)))
            return False
        return bool(getattr(result, "ok", False))

    # ------------------------------------------------------------------
    # Reading back
    # ------------------------------------------------------------------
    async def recent(self, limit: int = 10) -> list[Any]:
        return await self.store.outbox(limit)

    async def pause(self, seconds: float) -> None:
        """A deliberate wait between messages in a broadcast."""
        await asyncio.sleep(max(0.0, seconds))


__all__ = [
    "MAX_REPEAT",
    "Reach",
    "ReachError",
    "SendReport",
    "Target",
    "parse_duration",
    "spoiler",
]
