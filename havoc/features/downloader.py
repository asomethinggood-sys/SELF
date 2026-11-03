"""A link is an order: fetch the video, and say nothing else about it.

Drop a YouTube, TikTok or Instagram link into Saved Messages and she does not
answer with words — she answers with the video. The whole point is the absence
of commentary: a download is a job he asked for, and a paragraph about the video
he already decided to download is noise.

The shape of the exchange, and why it is this shape:

1. **the thumbnail first** — a picture is how you recognise the thing you just
   sent a link to, before you spend time on the file;
2. **the title next** — the site's title, not a guess;
3. **the qualities as a menu you answer with a number** — every video has a
   different ladder (2160p exists for one link and not the next), so a fixed
   "high/medium/low" would either lie or fail. The numbers come from the formats
   the site actually reported, and the last row is *sound only*;
4. **then the file** — sent as media, with the title under it, and the working
   message deleted so the chat holds the result and nothing else.

Everything the model would otherwise do with this message is suppressed by
claiming it (see :mod:`havoc.services.claims`). She does not comment on the
video, does not describe it, does not offer to send it — she sends it.

Feature areas: 45 (media), 96 (feature switches).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..command_registry import Command, CommandContext
from ..event_engine import EventType, NormalizedEvent
from ..services import downloader
from ..services.claims import install as install_claims
from ..services.downloader import DownloadError, Item, Quality
from ..services.sender import SendError
from ..ui import closing, drawn_rule, panel
from ..utils.formatting import bold, code, escape, italic, pre

#: How long a menu waits for a number. Five minutes is long enough to read the
#: title and pick, short enough that a stray "2" an hour later is not an order.
SESSION_TTL = 300.0
#: One menu per chat; the store is a dict so a leak is bounded, but it is also
#: pruned on every event so an abandoned session cannot linger.
MAX_SESSIONS = 64

#: The word for the sound-only row. Kept here rather than in a locale because it
#: sits inside a menu that is otherwise built from the site's own labels.
AUDIO_ROW = {"en": "sound only", "fa": "فقط صدا"}
CANCEL_WORDS = {"0", "cancel", "stop", "لغو", "صفر", "نه"}


@dataclass
class Target:
    """Where an answer goes: the chat, and the message it points at.

    The subscriber has a ``NormalizedEvent`` (``chat_id``/``message_id``); the
    command has a ``CommandContext`` whose event is the router's own shape. Both
    know enough to send, neither agrees on the attribute names — and the first
    version of this file asked the command path for ``event.message_id``, which
    it does not have, so `.download <link>` answered with an AttributeError. One
    small record, built once at each entry point, ends that class of bug.
    """

    chat_id: int
    message_id: int | None = None
    sender_id: int | None = None
    is_private: bool = False
    #: Whether this account wrote the message. It is the only honest way to read
    #: a missing ``sender_id``: outgoing messages are the owner's by definition,
    #: while an incoming one whose sender cannot be read is a stranger.
    is_outgoing: bool = False


def target_of(event: Any) -> Target:
    """Read a chat and a message id out of anything the router hands over."""
    message = getattr(event, "message", None)
    message_id = getattr(event, "message_id", None)
    if message_id is None:
        message_id = getattr(message, "id", None)
    chat_id = getattr(event, "chat_id", None) or getattr(message, "chat_id", None) or 0
    sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
    is_private = bool(
        getattr(event, "is_private", None)
        if getattr(event, "is_private", None) is not None
        else getattr(message, "is_private", False)
    )
    # ``NormalizedEvent`` spells it ``is_outgoing``; a Telethon message spells it
    # ``out``, and the command path may hand over either the message itself or a
    # wrapper around it. Read all three, in that order.
    is_outgoing = getattr(event, "is_outgoing", None)
    if is_outgoing is None:
        is_outgoing = getattr(event, "out", None)
    if is_outgoing is None:
        is_outgoing = getattr(message, "out", False)
    return Target(
        int(chat_id or 0),
        message_id,
        sender_id,
        is_private,
        is_outgoing=bool(is_outgoing),
    )


@dataclass
class Session:
    """One menu waiting for one number."""

    item: Item
    chat_id: int
    created_at: float = field(default_factory=time.monotonic)
    language: str = "en"
    #: The message holding the menu, so it can be edited after a pick instead of
    #: leaving a stale list of qualities that no longer mean anything.
    menu_id: int | None = None
    #: The quality picked, if a download is already running for this chat.
    busy: bool = False
    picks: int = 0

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > SESSION_TTL


def _size_text(size: int) -> str:
    """'' for unknown, '≈ 148 MB' otherwise — never a fake zero."""
    if size <= 0:
        return ""
    mb = size / (1024 * 1024)
    if mb >= 1024:
        return f"≈ {mb / 1024:.1f} GB"
    if mb >= 10:
        return f"≈ {mb:.0f} MB"
    return f"≈ {mb:.1f} MB"


def _clock(seconds: float) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return ""
    return f"{total // 60}:{total % 60:02d}"


class Downloader:
    """The link handler, the numeric menu, and the command in front of both."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        self.claims = install_claims(runtime)
        self.sessions: dict[int, Session] = {}
        self.started = 0
        self.delivered = 0
        self.failed = 0
        self.refused = 0

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    async def _enabled(self) -> bool:
        try:
            return await self.runtime.feature_manager.is_enabled("downloader")
        except Exception:  # noqa: BLE001 - an unreadable switch is not a licence
            return False

    async def _stopped(self) -> bool:
        try:
            return await self.runtime.store.emergency_stop()
        except Exception:  # noqa: BLE001
            return False

    def _owner(self, event: Target) -> bool:
        sender = event.sender_id
        if sender is None:
            # An outgoing message from this account has no sender id; the account
            # *is* the owner, and nothing else can write with it. An *incoming*
            # message whose sender cannot be read is a stranger, not the owner —
            # answering "unknown" with "yes" hands the download gate to whoever
            # can produce a message this shape cannot attribute.
            return bool(event.is_outgoing)
        return bool(self.runtime.permissions.is_owner(sender))

    async def _language(self, chat_id: int) -> str:
        owner = self.runtime.permissions.self_id
        for scope, key in (("chat", str(chat_id)), ("user", str(owner or ""))):
            if not key:
                continue
            try:
                stored = await self.runtime.store.get_language(scope, key)
            except Exception:  # noqa: BLE001
                stored = None
            if stored in {"en", "fa"}:
                return stored
        return self.runtime.config.default_language

    def _t(self, key: str, language: str, **variables: Any) -> str:
        try:
            return self.runtime.t(key, language, **variables)
        except Exception:  # noqa: BLE001 - a missing key must not silence a send
            return variables.get("default", key)

    # ------------------------------------------------------------------
    # The event: a link, or a number
    # ------------------------------------------------------------------
    async def on_message(self, event: NormalizedEvent) -> None:
        text = (event.text or "").strip()
        if not text:
            return
        target = target_of(event)
        self._prune()
        if self.claims.claimed_event(event):
            return
        if not await self._enabled() or await self._stopped():
            return
        session = self.sessions.get(target.chat_id)
        if session is not None and session.expired:
            self.sessions.pop(target.chat_id, None)
            session = None
        if session is not None and self._looks_like_choice(text):
            # The menu belongs to whoever ordered it. Without this gate a second
            # participant in a private chat could answer the owner's menu — pick
            # a quality, spend the owner's bandwidth and account, and have the
            # file land in that chat — or cancel a download the owner asked for.
            # A bare number is not a claim on somebody else's order.
            if not self._owner(target):
                self.refused += 1
                return
            await self._choose(target, session, text)
            return
        if not downloader.is_link(text):
            return
        if not self._owner(target):
            self.refused += 1
            return
        if not self._scoped(target):
            return
        self.claims.claim_event(event)
        await self._start(target, text)

    def _scoped(self, event: Target) -> bool:
        """Where a bare link counts as an order.

        Saved Messages (a link to yourself) and a private chat where the owner is
        the one writing. In a group a link is somebody sharing something, and
        downloading every YouTube link in a room is a different feature with a
        different consent.
        """
        self_id = self.runtime.permissions.self_id
        if self_id is not None and event.chat_id == self_id:
            return True
        return bool(event.is_private)

    @staticmethod
    def _looks_like_choice(text: str) -> bool:
        stripped = " ".join(text.split()).lower()
        if stripped in CANCEL_WORDS:
            return True
        return stripped.isdigit() and 0 <= int(stripped) <= 9

    # ------------------------------------------------------------------
    # Start: look at the link, draw the menu
    # ------------------------------------------------------------------
    async def _start(
        self,
        event: Target,
        url: str,
        *,
        language: str | None = None,
        prompt_message_id: int | None = None,
    ) -> None:
        lane = language or await self._language(event.chat_id)
        self.started += 1
        if not downloader.installed():
            await self._say(event, self._t("download.not_installed", lane, default=""), reply_to=prompt_message_id)
            return
        notice = await self._say(
            event, self._t("download.checking", lane, default=""), reply_to=prompt_message_id
        )
        try:
            item = await downloader.inspect(url)
        except DownloadError as exc:
            self.failed += 1
            await self._replace(
                event,
                notice,
                self._error_card(exc, lane),
            )
            return
        # The thumbnail is its own message, exactly as asked: picture, then the
        # title and the ladder. One message holding both would put the options
        # under a picture nobody can scroll past on a phone.
        await self._send_thumbnail(event, item, lane, reply_to=prompt_message_id)
        menu_id = await self._send_menu(event, item, lane)
        session = Session(
            item=item,
            chat_id=event.chat_id,
            language=lane,
            menu_id=menu_id,
        )
        self.sessions[event.chat_id] = session
        await self._discard_notice(event, notice)

    async def _send_thumbnail(
        self, event: Target, item: Item, language: str, *, reply_to: int | None
    ) -> None:
        data = await downloader.thumbnail(item.thumbnail)
        if not data:
            return
        caption = " · ".join(
            part
            for part in (
                escape(item.site.title() if item.site else item.title[:60]),
                escape(_clock(item.duration)),
            )
            if part
        )
        try:
            await self.runtime.sender.send_media(
                event.chat_id,
                data,
                caption=f"{italic(caption)}" if caption else "",
                reply_to=reply_to,
            )
        except SendError as exc:
            self.logger.debug("downloader: thumbnail refused: %s", exc)

    async def _send_menu(self, event: Target, item: Item, language: str) -> int | None:
        rows = []
        for row in item.qualities:
            label = AUDIO_ROW.get(language, AUDIO_ROW["en"]) if row.audio_only else row.label
            mark = "♪" if row.audio_only else "▣"
            size = _size_text(row.filesize)
            tail = f" {italic(escape(size))}" if size else ""
            rows.append(f"{code(str(row.index))} {italic('·')} {mark} {escape(label)}{tail}")
        facts = [
            part
            for part in (
                escape(item.uploader) if item.uploader else "",
                escape(_clock(item.duration)),
                escape(item.site.title() if item.site else ""),
            )
            if part
        ]
        body = [
            f"◈ {bold(escape(item.title))}",
            drawn_rule(width=9),
        ]
        if facts:
            body.append(italic(" · ".join(facts)))
        if item.source_note:
            body.append(italic(escape(item.source_note)))
        body.append(panel(rows))
        body.append(
            italic(escape(self._t("download.menu_hint", language, default="")))
        )
        body.append(escape(closing()))
        try:
            result = await self.runtime.sender.send(
                event.chat_id, "\n".join(body), reply_to=event.message_id, dry_run=False
            )
        except SendError as exc:
            self.logger.warning("downloader: menu could not be sent: %s", exc)
            return None
        return getattr(getattr(result, "message", None), "id", None)

    # ------------------------------------------------------------------
    # The pick
    # ------------------------------------------------------------------
    async def _choose(self, event: Target, session: Session, text: str) -> None:
        self.claims.claim(event.chat_id, event.message_id)
        language = session.language or await self._language(event.chat_id)
        stripped = " ".join(text.split()).lower()
        if stripped in CANCEL_WORDS:
            self.sessions.pop(event.chat_id, None)
            await self._say(event, self._t("download.cancelled", language, default=""))
            return
        quality = session.item.quality(int(stripped))
        if quality is None:
            await self._say(
                event,
                self._t(
                    "download.unknown_choice",
                    language,
                    options=str(len(session.item.qualities)),
                    default="",
                ),
            )
            return
        if session.busy:
            await self._say(event, self._t("download.busy", language, default=""))
            return
        session.busy = True
        session.picks += 1
        label = (
            AUDIO_ROW.get(language, AUDIO_ROW["en"]) if quality.audio_only else quality.label
        )
        notice = await self._say(
            event,
            self._t("download.pulling", language, quality=escape(label), default=""),
        )
        path = None
        try:
            path = await downloader.fetch(session.item.url, quality)
            await downloader.validate_file(path, session.item, quality)
        except DownloadError as exc:
            downloader.discard(path)
            self.failed += 1
            session.busy = False
            await self._replace(event, notice, self._error_card(exc, language, can_pick=True))
            return
        except asyncio.CancelledError:
            downloader.discard(path)
            session.busy = False
            raise
        try:
            result = await self.runtime.sender.send_media(
                event.chat_id,
                str(path),
                caption=self._caption(session.item, quality, label),
                reply_to=event.message_id,
            )
            if result.dry_run or not result.ok:
                raise SendError("the file was not uploaded (dry run or unsuccessful send)", kind="upload")
            self.delivered += 1
            session.busy = False
            self.sessions.pop(event.chat_id, None)
            await self._discard_notice(event, notice)
        except SendError as exc:
            self.failed += 1
            session.busy = False
            await self._replace(
                event,
                notice,
                self._error_card(
                    DownloadError(f"Telegram refused the file: {exc}", kind="upload"), language
                ),
            )
        finally:
            downloader.discard(path)

    def _caption(self, item: Item, quality: Quality, label: str) -> str:
        parts = [bold(escape(item.title))]
        line = " · ".join(
            part
            for part in (
                escape(label),
                escape(_clock(item.duration)),
                escape(item.uploader) if item.uploader else "",
            )
            if part
        )
        if line:
            parts.append(italic(line))
        if item.source_note:
            parts.append(italic(escape(item.source_note)))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------
    async def _say(self, event: Target, text: str, *, reply_to: int | None = None) -> Any:
        if not text:
            return None
        try:
            return await self.runtime.sender.send(
                event.chat_id,
                text,
                reply_to=event.message_id if reply_to is None else reply_to,
                dry_run=False,
            )
        except SendError as exc:
            self.logger.warning("downloader: could not speak: %s", exc)
            return None

    async def _replace(self, event: Target, notice: Any, text: str) -> None:
        """Turn the working message into the outcome, when there is one to edit."""
        message = getattr(notice, "message", None)
        if message is not None and getattr(message, "id", None) is not None and text:
            try:
                await self.runtime.sender.edit(event.chat_id, message.id, text)
                return
            except SendError as exc:
                self.logger.debug("downloader: edit failed, sending instead: %s", exc)
        await self._say(event, text)

    async def _discard_notice(self, event: Target, notice: Any) -> None:
        message = getattr(notice, "message", None)
        message_id = getattr(message, "id", None)
        if message_id is None:
            return
        try:
            await self.runtime.sender.delete(event.chat_id, message_id)
        except SendError as exc:
            self.logger.debug("downloader: working message left behind: %s", exc)

    def _error_card(self, exc: DownloadError, language: str, *, can_pick: bool = False) -> str:
        detail = " ".join(str(exc).split())[:200]
        lines = [f"◈ {bold(escape(self._t('download.failed_title', language, default='')))}"]
        lines.append(drawn_rule(width=9))
        if detail:
            lines.append(italic(escape(detail)))
        hint = self._hint_for(exc, language)
        if hint:
            lines.append(italic(escape(hint)))
        if can_pick and exc.kind in {"timeout", "unavailable", "failed"}:
            lines.append(
                italic(escape(self._t("download.retry_hint", language, default="")))
            )
        lines.append(escape(closing()))
        return "\n".join(line for line in lines if line.strip())

    def _hint_for(self, exc: DownloadError, language: str) -> str:
        if exc.kind == "not_installed":
            return self._t("download.install_hint", language, default="")
        if exc.kind == "too_big":
            return self._t("download.too_big_hint", language, mb=downloader.MAX_MB, default="")
        return ""

    def _prune(self) -> None:
        now = time.monotonic()
        for chat_id, session in list(self.sessions.items()):
            if now - session.created_at > SESSION_TTL:
                self.sessions.pop(chat_id, None)
        while len(self.sessions) > MAX_SESSIONS:
            oldest = min(self.sessions, key=lambda key: self.sessions[key].created_at)
            self.sessions.pop(oldest, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "installed": downloader.installed(),
            "ffmpeg": downloader.has_ffmpeg(),
            "max_mb": downloader.MAX_MB,
            "sessions": len(self.sessions),
            "started": self.started,
            "delivered": self.delivered,
            "failed": self.failed,
            "refused": self.refused,
        }


# ----------------------------------------------------------------------
# The command
# ----------------------------------------------------------------------
def _url_from(ctx: CommandContext) -> str:
    for word in ctx.args:
        found = downloader.find_link(word)
        if found:
            return found
    found = downloader.find_link(ctx.raw_args or "")
    if found:
        return found
    # Not in the command: the link may be the message it replies to.
    return ""


async def _reply_url(ctx: CommandContext) -> str:
    message = await ctx.reply_message()
    if message is None:
        return ""
    text = getattr(message, "raw_text", "") or getattr(message, "message", "") or ""
    return downloader.find_link(str(text))


def _info_card(ctx: CommandContext) -> str:
    state = getattr(ctx.runtime, "downloader", None)
    snapshot = state.snapshot() if state is not None else {}
    yes = ctx.t("common.yes")
    no = ctx.t("common.no")
    rows = [
        f"{ctx.t('download.info_engine')} {code('yt-dlp')} "
        f"{italic(escape(yes if snapshot.get('installed') else no))}",
        f"{ctx.t('download.info_ffmpeg')} "
        f"{italic(escape(yes if snapshot.get('ffmpeg') else no))}",
        f"{ctx.t('download.info_limit')} {escape(str(snapshot.get('max_mb') or downloader.MAX_MB))} MB",
        f"{ctx.t('download.info_sites')} {escape(', '.join(sorted({name.title() for name, _ in downloader.SITE_PATTERNS})))}",
    ]
    if snapshot.get("sessions"):
        rows.append(f"{ctx.t('download.info_open')} {escape(str(snapshot['sessions']))}")
    body = [
        f"◈ {bold(escape(ctx.t('download.info_header')))}",
        drawn_rule(width=9),
        panel(rows),
        italic(escape(ctx.t("download.info_hint", prefix=ctx.runtime.prefix))),
        escape(closing()),
    ]
    if not snapshot.get("installed", True):
        body.insert(3, pre("pip install 'havoc-userbot[download]'"))
    return "\n".join(body)


async def cmd_download(ctx: CommandContext) -> None:
    action = (ctx.sub or "").lower()
    state: Downloader | None = getattr(ctx.runtime, "downloader", None)
    if state is None:
        await ctx.reply(_info_card(ctx))
        return
    if action in {"info", "status", "help", "state"} or (not ctx.args and not action):
        if action or not (await _reply_url(ctx)):
            await ctx.reply(_info_card(ctx))
            return
    url = _url_from(ctx) or await _reply_url(ctx)
    if url:
        # A command is already claimed by the dispatcher: she answers with the
        # menu and the file, and the model never sees the link.
        await state._start(
            target_of(ctx.event),
            url,
            language=ctx.language,
            prompt_message_id=getattr(ctx.message, "id", None),
        )
        return
    take = action or (ctx.args[0].lower() if ctx.args else "")
    session = state.sessions.get(ctx.chat_id)
    if session is not None and Downloader._looks_like_choice(take):
        await state._choose(target_of(ctx.event), session, take)
        return
    await ctx.reply(_info_card(ctx))


def register(runtime: Any) -> None:
    runtime.registry.register(
        Command(
            "download",
            cmd_download,
            category="media",
            aliases=("dl", "grab", "vid"),
            usage="<link> | <number> | info",
            changes_state=True,
            subcommands=("info", "status", "help"),
        )
    )
    collector = getattr(runtime, "downloader", None)
    if not isinstance(collector, Downloader):
        collector = Downloader(runtime)
        runtime.downloader = collector
    else:
        collector.claims = install_claims(runtime)
    # Priority 30 is *before* the persona subscribers (40/45): whoever claims the
    # message first decides whether she talks about it, and a link she is already
    # downloading must never reach the model.
    runtime.event_engine.subscribe(
        "downloader.link",
        collector.on_message,
        {EventType.MESSAGE, EventType.OUTGOING},
        priority=30,
        description="a video link becomes a video, not a conversation",
    )
