"""Outbound message sending with safety rails.

Every outgoing Telegram call in HAVOC goes through :class:`Sender`:

* respects the global outbound rate window,
* honours dry-run mode (logs instead of sending),
* handles ``FloodWaitError`` by backing off globally and never by looping,
* splits long messages on safe boundaries,
* returns a typed :class:`SendResult` so callers can tell a real send from a
  simulated one (feature area 97) and never report false success.

The client is injected and only used through ``send_message`` /
``edit_message`` / ``delete_messages`` / ``send_file``, which keeps tests free
of Telethon.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..utils.formatting import prepare
from .rate_limiter import FloodGuard, RateLimited, RateLimiter


class SendError(Exception):
    def __init__(self, message: str, kind: str = "send_error") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass
class SendResult:
    ok: bool
    dry_run: bool = False
    message: Any | None = None
    messages: list[Any] = field(default_factory=list)
    text: str = ""
    error: str | None = None


class Sender:
    def __init__(
        self,
        client: Any,
        config: Any,
        limiter: RateLimiter,
        logger: Any,
        *,
        dry_run: bool | None = None,
        max_flood_wait: float = 10.0,
    ) -> None:
        self.client = client
        self.config = config
        self.limiter = limiter
        self.logger = logger
        self.dry_run = config.dry_run if dry_run is None else dry_run
        self.flood = FloodGuard(limiter)
        self.max_flood_wait = max_flood_wait
        self.sent_count = 0
        self.blocked_count = 0
        #: Filled in by the runtime: every message this process sends is recorded
        #: here, so a reply to one of them can be told apart from a reply to the
        #: owner's own message. Optional — tests and dry runs may not have one.
        self.ledger: Any | None = None

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------
    async def send(
        self,
        chat_id: Any,
        text: str,
        *,
        reply_to: int | None = None,
        parse_mode: str | None = "html",
        silent: bool = False,
        link_preview: bool = False,
        dry_run: bool | None = None,
        file: str | None = None,
        caption: str | None = None,
        voice_note: bool = False,
    ) -> SendResult:
        simulate = self.dry_run if dry_run is None else dry_run
        # Whitespace-only bodies are a bug in the caller, not a message.
        chunks = [chunk for chunk in prepare(text) if chunk.strip()] if text and text.strip() else []
        if file is not None:
            # A file is sent exactly once, with its caption attached. Splitting
            # it into "chunks" would either resend the file or silently drop
            # every caption after the first.
            if len(chunks) > 1:
                raise SendError(
                    "a file can only carry one caption; shorten the text", kind="caption_too_long"
                )
            chunks = chunks or [caption or ""]
        if not chunks:
            raise SendError("refusing to send an empty message", kind="empty")

        if simulate:
            preview = (caption or " ").join(chunks)[:500]
            self.logger.info("DRY-RUN send -> %s: %s", chat_id, preview)
            return SendResult(ok=True, dry_run=True, text="\n".join(chunks))

        results: list[Any] = []
        try:
            await self.limiter.acquire_outbound()
            self.limiter.check_flood()
        except RateLimited as exc:
            self.blocked_count += 1
            raise SendError(
                f"outbound limit reached; retry in {exc.retry_after:.1f}s", kind="rate_limited"
            ) from exc

        for index, chunk in enumerate(chunks or [""]):
            try:
                if file is not None:
                    message = await self._with_flood_retry(
                        lambda chunk=chunk, index=index: self.client.send_file(
                            chat_id,
                            file,
                            caption=chunk or caption or "",
                            parse_mode=parse_mode,
                            reply_to=reply_to if index == 0 else None,
                            silent=silent,
                            **({"voice_note": True} if voice_note else {}),
                        )
                    )
                else:
                    message = await self._with_flood_retry(
                        lambda chunk=chunk, index=index: self.client.send_message(
                            chat_id,
                            chunk,
                            parse_mode=parse_mode,
                            link_preview=link_preview,
                            reply_to=reply_to if index == 0 else None,
                            silent=silent,
                        )
                    )
                results.append(message)
                self.sent_count += 1
            except SendError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalized below
                self.blocked_count += 1
                raise SendError(_describe(exc), kind=_kind(exc)) from exc

        if self.ledger is not None:
            # Recorded here rather than by each caller: every path that sends —
            # the ambient answer, a command, a file, a voice note — is remembered,
            # and none of them can forget.
            for message in results:
                await self.ledger.note(chat_id, getattr(message, "id", None))
        return SendResult(ok=True, message=results[0] if results else None, messages=results, text="\n".join(chunks))

    async def _with_flood_retry(self, operation) -> Any:
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001
            wait = self.flood.handle(exc)
            if wait is None:
                raise
            if wait > self.max_flood_wait:
                raise SendError(
                    f"Telegram asked to wait {wait:.0f}s before sending again", kind="floodwait"
                ) from exc
            self.logger.warning("FloodWait: sleeping %.1fs before retry", wait)
            await asyncio.sleep(max(0.0, wait) + 0.5)
            return await operation()

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------
    async def respond(
        self,
        event: Any,
        text: str,
        *,
        reply: bool = True,
        **kwargs: Any,
    ) -> SendResult:
        """Send to the event's chat, optionally as a reply to its message.

        An answer to something a person just typed is *always* delivered, even in
        dry run. Dry run exists to hold back side effects — a scheduled message, an
        auto-reply — not to leave someone staring at a chat where the bot silently
        swallowed every reply, which is what happened after ``.dryrun on``.
        """
        kwargs.setdefault("dry_run", False)
        return await self.send(
            event.chat_id,
            text,
            reply_to=getattr(event, "id", None) if reply else None,
            **kwargs,
        )

    async def send_media(
        self,
        chat_id: Any,
        media: Any,
        *,
        caption: str = "",
        reply_to: int | None = None,
        silent: bool = False,
        parse_mode: str | None = "html",
        dry_run: bool | None = None,
        voice_note: bool = False,
    ) -> SendResult:
        """Send one file or an album (a list), with the usual safety rails.

        ``voice_note`` lets a file arrive as a playable voice message rather than
        a document. Telethon takes it as a send_file keyword; a fake client that
        does not know it is fine, it is passed through in the recorded kwargs.
        """
        if voice_note:
            return await self.send(
                chat_id,
                caption,
                file=media,
                caption=caption,
                reply_to=reply_to,
                silent=silent,
                parse_mode=parse_mode,
                dry_run=dry_run,
                voice_note=True,
            )
        return await self.send(
            chat_id,
            caption,
            file=media,
            caption=caption,
            reply_to=reply_to,
            silent=silent,
            parse_mode=parse_mode,
            dry_run=dry_run,
        )

    async def react(self, chat_id: Any, message_id: int, emoji: str) -> bool:
        """React to a message. ``False`` when the client cannot do it.

        Telegram has an allowed set of reaction emoji and rejects anything else,
        so a failure here is a "no" rather than an error the caller has to catch.
        """
        if self.dry_run:
            self.logger.info("DRY-RUN react -> %s on %s: %s", chat_id, message_id, emoji)
            return True
        for name in ("send_reaction", "react"):
            sender = getattr(self.client, name, None)
            if sender is None:
                continue
            try:
                result = sender(chat_id, message_id, emoji)
                if asyncio.iscoroutine(result):
                    await result
                return True
            except Exception as exc:  # noqa: BLE001 - a reaction is a flourish
                self.logger.warning("react failed: %s", exc)
                continue
        return False

    async def notify_owner(self, text: str, owner_id: int | None = None, **kwargs: Any) -> SendResult:
        """Deliver to Saved Messages (``me``) unless an explicit target is set.

        ``OWNER_IDS`` wins when configured so notifications land in a place the
        owner actually reads, and so tests can assert on a concrete chat id.
        """
        if owner_id is not None:
            target = owner_id
        else:
            configured = getattr(self.config, "owner_ids", ()) or ()
            target = configured[0] if configured else "me"
        return await self.send(target, text, **kwargs)

    async def edit(self, entity: Any, message: Any, text: str, *, dry_run: bool | None = None) -> SendResult:
        simulate = self.dry_run if dry_run is None else dry_run
        if simulate:
            self.logger.info("DRY-RUN edit -> %s", text[:300])
            return SendResult(ok=True, dry_run=True, text=text)
        try:
            await self.limiter.acquire_outbound()
            updated = await self.client.edit_message(entity, message, text, parse_mode="html")
            return SendResult(ok=True, message=updated, text=text)
        except Exception as exc:  # noqa: BLE001
            if self.flood.handle(exc) is not None:
                raise SendError("Telegram asked us to slow down", kind="floodwait") from exc
            raise SendError(_describe(exc), kind=_kind(exc)) from exc

    async def delete(self, entity: Any, message_ids: int | list[int], *, dry_run: bool | None = None) -> SendResult:
        simulate = self.dry_run if dry_run is None else dry_run
        if simulate:
            self.logger.info("DRY-RUN delete -> %s %s", entity, message_ids)
            return SendResult(ok=True, dry_run=True)
        try:
            await self.client.delete_messages(entity, message_ids)
            return SendResult(ok=True)
        except Exception as exc:  # noqa: BLE001
            raise SendError(_describe(exc), kind=_kind(exc)) from exc

    async def forward(self, entity: Any, message: Any, to_chat: Any) -> SendResult:
        if self.dry_run:
            self.logger.info("DRY-RUN forward -> %s", to_chat)
            return SendResult(ok=True, dry_run=True)
        try:
            await self.limiter.acquire_outbound()
            forwarded = await self.client.forward_messages(to_chat, message, entity)
            return SendResult(ok=True, message=forwarded)
        except Exception as exc:  # noqa: BLE001
            raise SendError(_describe(exc), kind=_kind(exc)) from exc


def _describe(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text[:300]


def _kind(exc: BaseException) -> str:
    name = type(exc).__name__
    mapping = {
        "FloodWaitError": "floodwait",
        "ChatWriteForbiddenError": "forbidden",
        "UserBannedInChannelError": "forbidden",
        "ChannelPrivateError": "inaccessible",
        "ChatAdminRequiredError": "forbidden",
        "MessageTooLongError": "too_long",
        "SlowModeWaitError": "slowmode",
        "TopicClosedError": "forbidden",
        "MessageIdInvalidError": "invalid_message",
    }
    return mapping.get(name, "send_error")
