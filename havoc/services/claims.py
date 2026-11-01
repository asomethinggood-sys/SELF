"""Messages that were already answered — and must not be answered twice.

A userbot routes one incoming message to every subscriber, and subscribers do
not know about each other. That is fine while they are doing different jobs
(reminder parsing, a watch keyword, an auto-reply), and it is wrong the moment
two of them both think the message is *for them*. The downloader is exactly
that case: a YouTube link in Saved Messages is an order to fetch a video, and if
she also sends it to the model the owner gets a video **and** a paragraph about
the video he already asked for.

Every handler that has claimed a message registers it here first; every handler
that answers must ask before it speaks. The window is short (a claim outlives
its usefulness in under a minute) and the store is tiny — this is a mailbox for
"hands off", not a history.

Feature area: 30 (one voice per message).
"""

from __future__ import annotations

from typing import Any

from .cache import TTLCache

#: Long enough to cover the slowest subscriber in the chain (a model call), and
#: short enough that the same message id in another chat is never confused.
CLAIM_TTL = 120.0
CLAIM_MAXSIZE = 1024


def _key(chat_id: Any, message_id: Any) -> str:
    return f"{chat_id}:{message_id}"


class Claims:
    """Which ``(chat, message)`` pairs have already been dealt with."""

    def __init__(self, *, ttl: float = CLAIM_TTL, maxsize: int = CLAIM_MAXSIZE) -> None:
        self._seen = TTLCache[bool](maxsize=maxsize, ttl=ttl)

    def claim(self, chat_id: Any, message_id: Any) -> None:
        """Mark this message as handled. Silently ignores a missing id."""
        if chat_id is None or message_id is None:
            return
        self._seen.set(_key(chat_id, message_id), True)

    def claimed(self, chat_id: Any, message_id: Any) -> bool:
        if chat_id is None or message_id is None:
            return False
        return bool(self._seen.get(_key(chat_id, message_id)))

    def claimed_event(self, event: Any) -> bool:
        """Convenience for a subscriber that only has the event at hand."""
        return self.claimed(getattr(event, "chat_id", None), getattr(event, "message_id", None))

    def claim_event(self, event: Any) -> None:
        self.claim(getattr(event, "chat_id", None), getattr(event, "message_id", None))


def install(runtime: Any) -> Claims:
    """Attach the shared instance to the runtime, once."""
    existing = getattr(runtime, "claims", None)
    if isinstance(existing, Claims):
        return existing
    claims = Claims()
    runtime.claims = claims
    return claims


__all__ = ["Claims", "CLAIM_TTL", "install"]
