"""Everything this account sent *as the bot*, by id.

Why this exists
---------------
In a group, two kinds of message come from the same account: the owner's, typed
in his own client, and hers, sent by this process. A Telegram reply to one of her
messages is an ordinary update that says "outgoing" for both, so the only honest
way to tell them apart is to remember what this process sent.

That memory used to live inside the persona feature and was filled by hand at the
end of one send path — which meant an answer sent by any other path (the by-name
command, a file, a voice note) was invisible, and replying to it looked like
replying to the owner. The ledger is filled by :class:`~havoc.services.sender.Sender`
itself, so **every** message the bot sends is recorded, whatever feature sent it.

It is also persisted: the bot is restarted often, and a reply that arrives after
a restart must still be recognised. The ids are kept per chat, newest last, and
capped — this is a conversation memory, not an archive.
"""

from __future__ import annotations

from collections import deque
from typing import Any

#: How many ids per chat. A reply arrives seconds or minutes after the message it
#: answers, so a few hundred is generous; unbounded would grow forever.
LEDGER_LIMIT = 200
#: Key in the settings table, one row per chat.
LEDGER_SETTING = "yura.sent_ids"


class SentLedger:
    """The ids of the messages the bot sent, per chat."""

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self._ids: dict[int, deque[int]] = {}

    async def _bucket(self, chat_id: Any) -> deque[int]:
        try:
            key = int(chat_id)
        except (TypeError, ValueError):
            key = 0
        bucket = self._ids.get(key)
        if bucket is not None:
            return bucket
        stored: Any = None
        if self.store is not None:
            try:
                stored = await self.store.get_setting(f"{LEDGER_SETTING}.{key}", None)
            except Exception:  # noqa: BLE001 - a broken settings row must not stop a reply
                stored = None
        ids = [int(value) for value in (stored or []) if str(value).lstrip("-").isdigit()]
        bucket = deque(ids[-LEDGER_LIMIT:], maxlen=LEDGER_LIMIT)
        self._ids[key] = bucket
        return bucket

    async def note(self, chat_id: Any, message_id: Any) -> None:
        """Record one message this process sent."""
        if message_id is None:
            return
        try:
            bucket = await self._bucket(chat_id)
        except Exception:  # noqa: BLE001 - bookkeeping is never worth an exception
            return
        try:
            value = int(message_id)
        except (TypeError, ValueError):
            return
        if value in bucket:
            return
        bucket.append(value)
        if self.store is not None:
            try:
                await self.store.set_setting(
                    f"{LEDGER_SETTING}.{int(chat_id)}", list(bucket)
                )
            except Exception:  # noqa: BLE001 - see above
                pass

    async def has(self, chat_id: Any, message_id: Any) -> bool:
        """True when ``message_id`` in ``chat_id`` is a message the bot sent."""
        if message_id is None:
            return False
        try:
            bucket = await self._bucket(chat_id)
        except Exception:  # noqa: BLE001
            return False
        try:
            return int(message_id) in bucket
        except (TypeError, ValueError):
            return False

    async def recent(self, chat_id: Any) -> tuple[int, ...]:
        try:
            return tuple(await self._bucket(chat_id))
        except Exception:  # noqa: BLE001
            return ()
