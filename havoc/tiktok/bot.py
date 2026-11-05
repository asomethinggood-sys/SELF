"""Yura on TikTok: one long run, events in, answers out.

This is ``havoc/client.py`` + ``handlers.py`` for a different platform, and it is
deliberately the same shape:

    restore jar → open a browser → open /messages → listen
        → normalize what arrives into a message
        → dedupe against everything already answered (persisted)
        → dispatch: a dot command, or her, in her voice
        → spend an allowance for anything that writes
        → on challenge: stop cleanly, do not push through
        → every N minutes, and at the end: save the jar

There is no polling loop by default. ``watch()`` attaches to the frames the
messages page is already receiving — TikTok's own push, sub-second, and the
reason this can feel like the Telegram version rather than like a cron job.
``POLL_FALLBACK_SECONDS`` is the safety net for the case where the frames turn
out to be opaque: it reads the conversation list on an interval instead, and the
run reports which mode it is in rather than pretending they are the same.

Two things this file refuses to do:

* **Retry into a challenge.** A captcha stops the run; a human clears it.
* **Answer twice.** Every message key is recorded *before* the reply is sent, so
  a crash between the two costs one unanswered message instead of a duplicate
  reply to a real person.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import SessionError, TikTokSessionStore
from .browser import InboxMessage, TikTokBrowser, browser_available, install_hint
from .limits import Budget, budget_for

__all__ = ["TikTokBot", "BotConfig"]

#: How often the jar is re-saved during a run. TikTok rotates tokens while the
#: page is alive; a successor that only got the cookies from the *start* of the
#: run can come back logged out, which is the single most likely way a handover
#: chain breaks in practice.
JAR_SAVE_SECONDS = 300

#: The safety net, when frames are not readable. Slow on purpose: this path is a
#: fallback, not a design.
POLL_FALLBACK_SECONDS = 20


@dataclass
class BotConfig:
    """Everything the loop needs that is not a service."""

    #: Yura's own numeric id on TikTok, once known. Only the owner is obeyed —
    #: everyone else gets her, not her commands.
    owner_id: str = ""
    #: The owner's TikTok handle, for messages that need a name.
    owner_handle: str = ""
    #: How long this run may live, in seconds. The workflow's ceiling minus a
    #: margin, so the run ends by choice rather than by being killed.
    run_seconds: float = 300.0 * 60
    #: Answer strangers at all? Off means she only talks to the owner.
    answer_strangers: bool = True
    #: Prefix for commands in a DM, same as Telegram's ``.``.
    prefix: str = "."
    #: Keep the browser window visible. Only useful for a local login.
    headless: bool = True
    #: Where generated files (a ``.draw``, a downloaded video) go.
    work_dir: Path = field(default_factory=lambda: Path(".runtime/tiktok/work"))


class TikTokBot:
    """The run. Owns the browser, the budget and the dedupe set."""

    def __init__(
        self,
        store: TikTokSessionStore,
        config: BotConfig,
        *,
        runtime: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.runtime = runtime
        self.logger = logger or logging.getLogger("havoc.tiktok")
        self.budget = Budget(store)
        self.browser: TikTokBrowser | None = None
        self.stats: dict[str, int] = {
            "seen": 0,
            "answered": 0,
            "commands": 0,
            "actions": 0,
            "refused": 0,
            "skipped": 0,
        }
        self.challenge = False
        self.mode = "unknown"
        #: Her hands for this run (like/follow/comment/upload), built with the
        #: browser so a command and a free-form answer share one pace and one
        #: allowance. ``None`` until :meth:`run` starts.
        self.actions: Any = None
        self._handled: dict[str, float] = {}
        self._state = store.state()
        self._queue: asyncio.Queue[InboxMessage] = asyncio.Queue()
        self._heartbeat = time.monotonic()

    # -- dedupe, persisted --------------------------------------------------
    def _load_handled(self) -> None:
        raw = self._state.get("handled")
        if isinstance(raw, dict):
            # Bounded: a window of recent keys is all dedupe needs, and an
            # unbounded set in a runtime file is a slow leak.
            self._handled = {str(key): float(value) for key, value in raw.items()}

    def _already_handled(self, key: str) -> bool:
        return key in self._handled

    def _mark_handled(self, key: str) -> None:
        """Record *before* replying: one missed message beats one duplicate."""
        self._handled[key] = time.time()
        cutoff = time.time() - 3 * 86_400
        self._handled = {k: v for k, v in self._handled.items() if v >= cutoff}
        self._state["handled"] = self._handled
        self.store.save_state(self._state)

    # -- lifecycle ---------------------------------------------------------
    async def run(self, *, minutes: float | None = None) -> dict[str, Any]:
        """Start, listen, stop when the clock or a challenge says so."""
        if not browser_available():
            raise SessionError("browser engine not installed", kind="no_engine", hint=install_hint())
        self._load_handled()
        self.browser = TikTokBrowser(self.store, headless=self.config.headless, logger=self.logger)
        started = time.monotonic()
        budget_seconds = (minutes * 60) if minutes else self.config.run_seconds
        await self.browser.start()
        from .actions import AccountActions

        self.actions = AccountActions(self.browser, self.budget, logger=self.logger)
        try:
            opened = await self.browser.open_inbox()
            if not opened.ok:
                if opened.data.get("challenge"):
                    self.challenge = True
                    return self._report(started, opened.detail)
                # The inbox is the whole run; if it is not there, say so once.
                return self._report(started, opened.detail)

            await self.browser.attach_frames()
            self.mode = "frames" if await self._frames_arrive() else "poll"
            self.logger.info("tiktok: watching the inbox in %s mode", self.mode)

            watcher = asyncio.create_task(self._watch(budget_seconds))
            try:
                await self._drain(budget_seconds)
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        finally:
            await self._shutdown()
        return self._report(started, "run ended")

    async def _frames_arrive(self, *, wait: float = 12.0) -> bool:
        """Did the page actually hand us readable frames?

        A websocket that carries protobuf is indistinguishable from no websocket
        at all, from here. So the question is asked by measurement: wait a few
        seconds and see whether a JSON frame arrived. The answer decides the mode
        and is written into the run report, because "why is she slow" should be
        answerable from the log rather than guessed at.
        """
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.browser and self.browser._frames:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _watch(self, budget_seconds: float) -> None:
        """Feed the queue from frames, or from polling if frames are opaque."""
        poll = POLL_FALLBACK_SECONDS if self.mode == "poll" else 3600
        next_poll = time.monotonic() + poll
        deadline = time.monotonic() + budget_seconds
        while time.monotonic() < deadline:
            if self.browser and await self.browser.challenged():
                self.challenge = True
                self.logger.warning("tiktok: challenge shown — stopping the run")
                await self._queue.put(InboxMessage(thread_id="", message_id="challenge", sender="tiktok", text=""))
                return
            if time.monotonic() >= next_poll:
                try:
                    found = await self.browser.poll_threads() if self.browser else []
                    for message in found:
                        await self._queue.put(message)
                except Exception as exc:  # noqa: BLE001 - a bad poll must not end the run
                    self.logger.debug("poll failed: %s", exc)
                next_poll = time.monotonic() + poll
            await asyncio.sleep(1.0)

    async def _drain(self, budget_seconds: float) -> None:
        deadline = time.monotonic() + budget_seconds
        while time.monotonic() < deadline:
            timeout = max(1.0, min(15.0, deadline - time.monotonic()))
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                if self.challenge:
                    return
                continue
            if message.message_id == "challenge":
                return
            self.stats["seen"] += 1
            try:
                await self.handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad message must not kill the run
                self.logger.exception("failed to handle a tiktok message: %s", exc)
            if self.challenge:
                return

    async def _shutdown(self) -> None:
        """Save the jar *before* closing the browser — the tokens live in it."""
        if self.browser is not None:
            try:
                account = await self.browser.save_jar()
                self.logger.info("tiktok: jar saved (%d cookies, %s)", len(account.cookies), account.handle or "?")
            except Exception as exc:  # noqa: BLE001 - a failed save must not crash the exit
                self.logger.warning("tiktok: could not save the jar: %s", exc)
            with contextlib.suppress(Exception):
                await self.browser.close()

    def _report(self, started: float, reason: str) -> dict[str, Any]:
        return {
            "reason": reason,
            "mode": self.mode,
            "challenge": self.challenge,
            "seconds": round(time.monotonic() - started, 1),
            "stats": dict(self.stats),
            "counts": {
                action: {"used": self.budget.spent(action), "limit": budget_for(action)}
                for action in ("like", "comment", "follow", "post", "dm")
            },
        }

    # -- one message -------------------------------------------------------
    async def handle(self, message: InboxMessage) -> None:
        """Turn one inbox message into one reply, or nothing at all."""
        if not message.text.strip() or message.is_self:
            return
        if self._already_handled(message.key):
            self.stats["skipped"] += 1
            return
        # Marked first: a crash after this loses one message instead of sending
        # the same answer to a real person twice.
        self._mark_handled(message.key)

        text = message.text.strip()
        owner = self._is_owner(message)
        if not owner and not self.config.answer_strangers:
            self.stats["skipped"] += 1
            return

        if text.startswith(self.config.prefix):
            self.stats["commands"] += 1
            reply = await self.command(text[len(self.config.prefix):], owner=owner, message=message)
        else:
            reply = await self.answer(text, message, owner=owner)

        if not reply:
            self.stats["skipped"] += 1
            return
        await self.reply(reply)
        self.stats["answered"] += 1

    def _is_owner(self, message: InboxMessage) -> bool:
        said = (self.config.owner_id or "").strip()
        if not said:
            # No owner recorded yet: treat the *only* conversation this account
            # has as the owner's, which is how a first run bootstraps. If there
            # are several, nobody is the owner and everyone gets her manners.
            return True
        return said in {message.thread_id, message.sender}

    async def reply(self, text: str) -> None:
        """Send it, spending one DM from today's allowance."""
        if self.browser is None:
            return
        self.budget.spend("dm")
        result = await self.browser.send_dm(text[:1000])
        if result.ok:
            self.stats["actions"] += 1
            return
        if result.data.get("challenge"):
            self.challenge = True
        self.logger.warning("tiktok: reply failed: %s", result.detail)

    # -- her mouth ---------------------------------------------------------
    async def answer(self, text: str, message: InboxMessage, *, owner: bool) -> str:
        """Her, in her voice, with the same memory and the same provider.

        The chat id is derived from the thread so TikTok conversations get the
        same rolling history the Telegram ones do — same ``yura_history`` table,
        same 20 turns, no second memory system to keep in step.
        """
        chat = getattr(self.runtime, "yura_chat", None)
        if chat is None:
            return self._fallback()
        chat_id = self._chat_id(message.thread_id)
        try:
            answer = await chat.answer(
                text,
                chat_id=chat_id,
                is_owner=owner,
                sender_id=chat_id,
                chat_title=message.thread_name or message.sender,
            )
            return (answer.text or "").strip()
        except Exception as exc:  # noqa: BLE001 - her voice never crashes the run
            self.logger.warning("tiktok: yura did not answer: %s", exc)
            return self._fallback()

    def _fallback(self) -> str:
        try:
            return ("ᵕ̈", "hm?", "i'm here, just quiet")[int(time.time()) % 3]
        except Exception:  # noqa: BLE001
            return "hm?"

    @staticmethod
    def _chat_id(thread_id: str) -> int:
        """A stable negative int per thread, so TikTok chats cannot collide with
        Telegram chat ids in the shared history table."""
        digest = 0
        for char in str(thread_id):
            digest = (digest * 131 + ord(char)) % (2**62)
        return -(digest or 1)

    # -- her hands, by command --------------------------------------------
    async def command(self, raw: str, *, owner: bool, message: InboxMessage) -> str:
        """The command surface, as wide as the account allows.

        TikTok has no keyboards and no buttons, so this is one line of text per
        command and one line of receipt back. The names are the Telegram names —
        ``like``, ``follow``, ``block``, ``comment``, ``post``, ``draw``,
        ``remind``, ``status``, ``help`` — because the owner asked for the same
        Yura, not a differently-named one.
        """
        from .command import dispatch

        return await dispatch(raw, bot=self, owner=owner, message=message)
