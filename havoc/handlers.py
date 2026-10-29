"""Telegram event handlers — thin adapters between Telethon and the engine.

The router does only four things:
1. normalize the raw update into a :class:`NormalizedEvent`,
2. publish it to the event engine (dedupe + subscribers),
3. recognize commands and hand them to the dispatcher,
4. resolve pending confirmation flows.

No business logic lives here (spec §5, "keep Telegram handlers thin"), and
every handler is wrapped so an exception becomes a health entry instead of a
frozen client.
"""

from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from .event_engine import EventType, NormalizedEvent, normalize_message, now_event
from .utils.parsing import looks_like_command, parse_command, strip_prefix
from .utils.security import redact_text


class Router:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        self.handled_events = 0
        self.handler_errors = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, client: Any) -> None:
        from telethon import events

        client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        client.add_event_handler(self.on_edited, events.MessageEdited())
        client.add_event_handler(self.on_deleted, events.MessageDeleted())
        client.add_event_handler(self.on_chat_action, events.ChatAction())
        self.logger.info("telegram handlers registered")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def self_id(self) -> int | None:
        return self.runtime.permissions.self_id

    async def _emit(self, event: NormalizedEvent) -> None:
        self.handled_events += 1
        self.runtime.stats["events"] = self.runtime.stats.get("events", 0) + 1
        await self.runtime.event_engine.emit(event)

    async def _safe(self, name: str, function: Callable[[Any], Awaitable[None]], event: Any) -> None:
        try:
            await function(event)
        except Exception as exc:  # noqa: BLE001 - the client must keep running
            self.handler_errors += 1
            self.runtime.health.record_error(f"handler:{name}", exc)
            self.logger.error(
                "unhandled error in %s: %s\n%s",
                name,
                redact_text(str(exc)),
                traceback.format_exc(limit=6),
            )

    # ------------------------------------------------------------------
    # Entry points registered with Telethon
    # ------------------------------------------------------------------
    async def on_outgoing(self, event: Any) -> None:
        await self._safe("outgoing", self._handle_outgoing, event)

    async def on_incoming(self, event: Any) -> None:
        await self._safe("incoming", self._handle_incoming, event)

    async def on_edited(self, event: Any) -> None:
        await self._safe("edited", self._handle_edited, event)

    async def on_deleted(self, event: Any) -> None:
        await self._safe("deleted", self._handle_deleted, event)

    async def on_chat_action(self, event: Any) -> None:
        await self._safe("chat_action", self._handle_chat_action, event)

    # ------------------------------------------------------------------
    # Implementations
    # ------------------------------------------------------------------
    async def _handle_outgoing(self, event: Any) -> None:
        message = getattr(event, "message", None) or event
        text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
        chat_id = getattr(message, "chat_id", None)
        sender_id = getattr(message, "sender_id", None) or self.self_id
        language = await self.runtime.resolve_language(text, chat_id=chat_id, sender_id=sender_id)

        # Pending confirmation flows take precedence over command parsing.
        if sender_id and await self.runtime.store.get_interactive(int(sender_id)) is not None:
            if await self.runtime.dispatcher.handle_confirmation(event, message, text, language):
                return

        if not looks_like_command(text, self.runtime.prefix):
            # Some commands also answer to a bare word (``فال``, ``yura``).
            bare = self._bare_command(text)
            if bare is not None:
                normalized = normalize_message(
                    message, self_id=self.self_id, prefix=self.runtime.prefix, event_type=EventType.COMMAND
                )
                normalized.extra["command"] = bare.name
                normalized.extra["bare"] = True
                await self._emit(normalized)
                await self.runtime.dispatcher.handle(event, message, bare, language)
                return
            normalized = normalize_message(
                message, self_id=self.self_id, prefix=self.runtime.prefix, event_type=EventType.OUTGOING
            )
            await self._emit(normalized)
            return

        parsed = parse_command(text, self.runtime.prefix)
        if parsed is None:
            normalized = normalize_message(
                message, self_id=self.self_id, prefix=self.runtime.prefix, event_type=EventType.OUTGOING
            )
            await self._emit(normalized)
            return

        normalized = normalize_message(
            message, self_id=self.self_id, prefix=self.runtime.prefix, event_type=EventType.COMMAND
        )
        normalized.extra["command"] = parsed.name
        await self._emit(normalized)
        await self.runtime.dispatcher.handle(event, message, parsed, language)

    def _bare_command(self, text: str) -> Any:
        """Parse a prefix-less command, or return None.

        Only an exact first word from :data:`havoc.aliases.BARE_COMMANDS`
        triggers this, and only when it stands alone or is followed by one of
        that command's own subcommands — so a sentence that merely starts with
        the word "yura" is still just a sentence.
        """
        from .aliases import bare_command

        stripped = text.strip()
        if not stripped:
            return None
        head, _, rest = stripped.partition(" ")
        found = bare_command(head)
        if found is None:
            return None
        name, fixed = found
        command = self.runtime.registry.resolve(name)
        if command is None:  # the feature that owns it is not registered
            return None
        words = [part for part in rest.split() if part]
        if words:
            token = words[0].lower()
            if token not in command.subcommands:
                return None
            rest = " ".join(words)
        else:
            rest = fixed
        built = f"{self.runtime.prefix}{name} {rest}".strip()
        return parse_command(built, self.runtime.prefix)

    async def _handle_incoming(self, event: Any) -> None:
        message = getattr(event, "message", None) or event
        normalized = normalize_message(message, self_id=self.self_id, prefix=self.runtime.prefix)
        await self._emit(normalized)

    async def _handle_edited(self, event: Any) -> None:
        message = getattr(event, "message", None) or event
        normalized = normalize_message(
            message, self_id=self.self_id, prefix=self.runtime.prefix, event_type=EventType.EDIT
        )
        await self._emit(normalized)

    async def _handle_deleted(self, event: Any) -> None:
        ids = tuple(getattr(event, "deleted_ids", ()) or ())
        chat_id = getattr(event, "chat_id", None) or 0
        if not ids:
            return
        normalized = NormalizedEvent(
            type=EventType.DELETED,
            chat_id=int(chat_id),
            text="",
            correlation_id=f"del:{chat_id}:{','.join(str(i) for i in ids)}",
            extra={"deleted_ids": list(ids)},
        )
        await self._emit(normalized)

    async def _handle_chat_action(self, event: Any) -> None:
        message = getattr(event, "message", None)
        chat_id = getattr(event, "chat_id", None) or 0
        users = getattr(event, "users", None) or []
        user_ids = [getattr(user, "id", None) for user in users] if users else []
        joined = bool(getattr(event, "user_joined", False) or getattr(event, "user_added", False))
        normalized = NormalizedEvent(
            type=EventType.NEW_CHAT if joined else EventType.CHAT_ACTION,
            chat_id=int(chat_id),
            message_id=getattr(message, "id", None),
            sender_id=getattr(event, "user_id", None),
            text="",
            is_private=bool(getattr(message, "is_private", False)) if message is not None else False,
            is_group=bool(getattr(message, "is_group", False)) if message is not None else False,
            correlation_id=f"action:{chat_id}:{getattr(message, 'id', None)}:{user_ids}",
            raw=event,
            extra={"user_ids": user_ids, "created": bool(getattr(event, "created", False))},
        )
        await self._emit(normalized)


class SystemEventBridge:
    """Emits system events (disconnects, flood waits) into the engine."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def disconnected(self) -> None:
        self.runtime.health.mark_disconnected()
        await self.runtime.event_engine.emit(now_event(EventType.NETWORK_DISCONNECT))

    async def reconnected(self) -> None:
        self.runtime.health.mark_reconnect()
        await self.runtime.event_engine.emit(now_event(EventType.NETWORK_RECONNECT))

    async def flood_wait(self, seconds: float) -> None:
        self.runtime.health.mark_flood_wait()
        await self.runtime.event_engine.emit(now_event(EventType.FLOOD_WAIT, seconds=seconds))


def command_text(runtime: Any, message: Any) -> str | None:
    text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
    return strip_prefix(text, runtime.prefix)
