"""Event normalization, routing and isolation.

Pipeline
--------
``Telegram event`` → :func:`normalize_message` → :class:`NormalizedEvent`
→ :class:`EventEngine` → subscribers (features) in priority order.

Design rules (spec §4)
----------------------
* Detection is separated from action. A subscriber that only watches never
  sends; sending requires an explicit feature switch.
* Every normalized event is deduplicated by a correlation id so reconnects and
  edits do not trigger automation twice.
* Subscriber failures are isolated, counted and logged — one broken feature
  cannot take down the router.
* Message text is kept only in memory for subscribers, never logged.

Feature areas: 5, 31-40, 89.
"""

from __future__ import annotations

import asyncio
import enum
import time
import traceback
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .language_detection import detect_language
from .services.cache import TTLCache, dedupe_key
from .utils.security import scrub_message_preview
from .utils.text import extract_hashtags, extract_mentions, extract_urls
from .utils.time import utc_now


class EventType(enum.StrEnum):
    MESSAGE = "message"
    OUTGOING = "outgoing"
    EDIT = "edit"
    DELETED = "deleted"
    COMMAND = "command"
    MENTION = "mention"
    REPLY = "reply"
    URL = "url"
    HASHTAG = "hashtag"
    MEDIA = "media"
    CHAT_ACTION = "chat_action"
    NEW_CHAT = "new_chat"
    BURST = "burst"
    SCHEDULER_JOB = "scheduler_job"
    SCHEDULER_FAILURE = "scheduler_failure"
    NETWORK_DISCONNECT = "network_disconnect"
    NETWORK_RECONNECT = "network_reconnect"
    FLOOD_WAIT = "flood_wait"
    CONFIG_ERROR = "config_error"
    DATABASE_ERROR = "database_error"
    FEATURE_ERROR = "feature_error"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"


MESSAGE_TYPES = (
    "text", "photo", "video", "document", "audio", "voice", "video_note",
    "sticker", "animation", "poll", "geo", "contact", "dice", "service", "unknown",
)

MEDIA_TYPES = {"photo", "video", "document", "audio", "voice", "video_note", "sticker", "animation"}


@dataclass
class NormalizedEvent:
    """What subscribers receive — only fields they legitimately need."""

    type: EventType
    chat_id: int
    message_id: int | None = None
    sender_id: int | None = None
    timestamp: float = field(default_factory=lambda: time.time())
    text: str = ""
    language: str = "unknown"
    confidence: float = 0.0
    message_type: str = "text"
    is_outgoing: bool = False
    is_private: bool = False
    is_group: bool = False
    is_channel: bool = False
    is_reply: bool = False
    reply_to_id: int | None = None
    mentions_me: bool = False
    replies_to_me: bool = False
    urls: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    media_type: str | None = None
    forward_from: int | None = None
    edit_date: float | None = None
    correlation_id: str = ""
    raw: Any = field(default=None, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)



    def safe_preview(self, limit: int = 60) -> str:
        return scrub_message_preview(self.text, limit)


def _media_type(message: Any) -> str:
    raw = getattr(message, "media", None)
    if raw is None:
        if getattr(message, "action", None) is not None:
            return "service"
        return "text" if (getattr(message, "text", "") or "") else "unknown"
    for attribute in (
        "photo", "video", "document", "audio", "voice", "video_note", "sticker",
        "gif", "poll", "geo", "contact", "dice",
    ):
        if getattr(raw, attribute, None) is not None:
            return "animation" if attribute == "gif" else attribute
    return "unknown"


def normalize_message(
    message: Any,
    *,
    self_id: int | None = None,
    prefix: str = ".",
    event_type: EventType | None = None,
) -> NormalizedEvent:
    """Convert a Telethon message-like object into a normalized event.

    Uses duck typing (``getattr``) so it works with the real library, with
    lightweight fakes in tests, and with future Telethon versions.
    """
    text = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "")
    if not isinstance(text, str):
        text = str(text)
    estimate = detect_language(text)
    sender_id = getattr(message, "sender_id", None)
    chat_id = getattr(message, "chat_id", None) or (getattr(message, "chat", None) and getattr(message.chat, "id", None)) or 0
    message_id = getattr(message, "id", None)
    is_outgoing = bool(getattr(message, "out", False))
    is_private = bool(getattr(message, "is_private", False))
    is_group = bool(getattr(message, "is_group", False))
    is_channel = bool(getattr(message, "is_channel", False))
    reply_to = getattr(message, "reply_to_msg_id", None) or getattr(message, "reply_to", None)
    if hasattr(reply_to, "reply_to_msg_id"):
        reply_to = reply_to.reply_to_msg_id
    mentioned = bool(getattr(message, "mentioned", False))
    replies_to_me = False
    if reply_to is not None:
        replied = getattr(message, "reply_to", None)
        replied_sender = getattr(replied, "sender_id", None) if replied is not None else None
        replies_to_me = bool(self_id and replied_sender == self_id)
    forward_from = None
    fwd = getattr(message, "fwd_from", None)
    if fwd is not None:
        forward_from = getattr(fwd, "from_id", None)
        if hasattr(forward_from, "user_id"):
            forward_from = forward_from.user_id
    edit_date = getattr(message, "edit_date", None)
    date = getattr(message, "date", None)

    if event_type is not None:
        resolved_type = event_type
    elif edit_date is not None:
        resolved_type = EventType.EDIT
    elif is_outgoing:
        resolved_type = EventType.OUTGOING
    else:
        resolved_type = EventType.MESSAGE

    mtype = _media_type(message)
    event = NormalizedEvent(
        type=resolved_type,
        chat_id=int(chat_id or 0),
        message_id=message_id,
        sender_id=sender_id,
        timestamp=date.timestamp() if hasattr(date, "timestamp") else time.time(),
        text=text,
        language=estimate.language,
        confidence=estimate.confidence,
        message_type=mtype,
        is_outgoing=is_outgoing,
        is_private=is_private,
        is_group=is_group,
        is_channel=is_channel,
        is_reply=reply_to is not None,
        reply_to_id=reply_to,
        mentions_me=mentioned or (bool(self_id) and bool(text) and _mentions_user(text, self_id)),
        replies_to_me=replies_to_me,
        urls=tuple(extract_urls(text)),
        hashtags=tuple(extract_hashtags(text)),
        mentions=tuple(extract_mentions(text)),
        media_type=mtype if mtype != "text" else None,
        forward_from=forward_from,
        edit_date=edit_date.timestamp() if hasattr(edit_date, "timestamp") else None,
        correlation_id=dedupe_key("msg", chat_id, message_id, resolved_type.value, edit_date),
        raw=message,
    )
    return event


def _mentions_user(text: str, self_id: int) -> bool:
    return f"tg://user?id={self_id}" in text


Subscriber = Callable[[NormalizedEvent], Awaitable[None]]


@dataclass
class Subscription:
    name: str
    handler: Subscriber
    event_types: frozenset[EventType] | None = None
    priority: int = 100
    description: str = ""
    enabled: bool = True
    failures: int = 0
    last_error: str = ""

    def wants(self, event: NormalizedEvent) -> bool:
        if not self.enabled:
            return False
        return self.event_types is None or event.type in self.event_types


@dataclass
class EventMetrics:
    processed: int = 0
    dropped_duplicates: int = 0
    subscriber_errors: int = 0
    bursts: int = 0
    by_type: Counter = field(default_factory=Counter)
    by_subscriber: Counter = field(default_factory=Counter)
    latencies: deque = field(default_factory=lambda: deque(maxlen=500))

    def observe(self, seconds: float) -> None:
        self.latencies.append(seconds)

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return round(ordered[index] * 1000, 2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "dropped_duplicates": self.dropped_duplicates,
            "subscriber_errors": self.subscriber_errors,
            "bursts": self.bursts,
            "by_type": dict(self.by_type),
            "by_subscriber": dict(self.by_subscriber),
            "p95_latency_ms": self.p95_latency_ms,
        }


class EventEngine:
    """Routes normalized events to subscribers with dedupe and metering."""

    def __init__(
        self,
        *,
        logger: Any,
        dedupe_ttl: float = 120.0,
        burst_window: float = 10.0,
        burst_threshold: int = 40,
        debug: bool = False,
    ) -> None:
        self.logger = logger
        self.subscriptions: list[Subscription] = []
        self.metrics = EventMetrics()
        self._seen = TTLCache[bool](maxsize=4096, ttl=dedupe_ttl)
        self._burst_window = burst_window
        self._burst_threshold = burst_threshold
        self._burst_events: deque = deque(maxlen=burst_threshold * 4)
        self._burst_notified_at = 0.0
        self.debug = debug

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------
    def subscribe(
        self,
        name: str,
        handler: Subscriber,
        event_types: Iterable[EventType] | None = None,
        priority: int = 100,
        description: str = "",
    ) -> Subscription:
        if any(sub.name == name for sub in self.subscriptions):
            raise ValueError(f"duplicate subscriber name: {name}")
        subscription = Subscription(
            name=name,
            handler=handler,
            event_types=frozenset(event_types) if event_types else None,
            priority=priority,
            description=description,
        )
        self.subscriptions.append(subscription)
        self.subscriptions.sort(key=lambda sub: (sub.priority, sub.name))
        return subscription

    def unsubscribe(self, name: str) -> bool:
        before = len(self.subscriptions)
        self.subscriptions = [sub for sub in self.subscriptions if sub.name != name]
        return len(self.subscriptions) != before

    def subscribers_for(self, event: NormalizedEvent) -> list[Subscription]:
        return [sub for sub in self.subscriptions if sub.wants(event)]

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------
    async def emit(self, event: NormalizedEvent) -> None:
        """Deliver ``event`` to interested subscribers immediately (awaited)."""
        if event.correlation_id and self._seen.get(event.correlation_id):
            self.metrics.dropped_duplicates += 1
            if self.debug:
                self.logger.debug("dedupe: dropped %s %s", event.type.value, event.correlation_id)
            return
        if event.correlation_id:
            self._seen.set(event.correlation_id, True)

        self.metrics.processed += 1
        self.metrics.by_type[event.type.value] += 1
        self._track_burst(event)

        for subscription in self.subscribers_for(event):
            started = time.monotonic()
            try:
                await subscription.handler(event)
                self.metrics.by_subscriber[subscription.name] += 1
            except Exception as exc:  # noqa: BLE001 - isolation by design
                subscription.failures += 1
                subscription.last_error = f"{type(exc).__name__}: {exc}"[:200]
                self.metrics.subscriber_errors += 1
                self.logger.error(
                    "subscriber %s failed on %s: %s\n%s",
                    subscription.name,
                    event.type.value,
                    exc,
                    traceback.format_exc(limit=5),
                )
            finally:
                self.metrics.observe(time.monotonic() - started)

        if self.debug and not event.is_outgoing:
            self.logger.debug(
                "event %s chat=%s lang=%s(%s) preview=%s",
                event.type.value,
                event.chat_id,
                event.language,
                event.confidence,
                event.safe_preview(30),
            )

    # ------------------------------------------------------------------
    # Burst detection
    # ------------------------------------------------------------------
    def _track_burst(self, event: NormalizedEvent) -> None:
        now = time.monotonic()
        self._burst_events.append(now)
        while self._burst_events and now - self._burst_events[0] > self._burst_window:
            self._burst_events.popleft()
        if len(self._burst_events) < self._burst_threshold:
            return
        if now - self._burst_notified_at < self._burst_window:
            return
        self._burst_notified_at = now
        self.metrics.bursts += 1
        burst = NormalizedEvent(
            type=EventType.BURST,
            chat_id=event.chat_id,
            text="",
            correlation_id=dedupe_key("burst", int(now // self._burst_window)),
            extra={
                "count": len(self._burst_events),
                "window": self._burst_window,
                "sample_chat": event.chat_id,
            },
        )
        asyncio.create_task(self.emit(burst))

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.snapshot(),
            "subscribers": [
                {
                    "name": sub.name,
                    "priority": sub.priority,
                    "failures": sub.failures,
                    "last_error": sub.last_error,
                }
                for sub in self.subscriptions
            ],
        }


def now_event(event_type: EventType, *, chat_id: int = 0, **extra: Any) -> NormalizedEvent:
    """Build a synthetic system event (startup, shutdown, errors)."""
    return NormalizedEvent(
        type=event_type,
        chat_id=chat_id,
        timestamp=utc_now().timestamp(),
        correlation_id=dedupe_key(event_type.value, time.time()),
        extra=extra,
    )
