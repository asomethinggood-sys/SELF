"""Who is talking, and what was being talked about.

Two failures this fixes, both reported by the owner:

* **"it might talk to my friend and think it's me"** — in a group the account is
  one of many voices, and a model given only "a message arrived" will happily
  address the owner while answering a stranger. Every turn now carries the
  speaker's name, whether that speaker *is* the owner, and the chat's name.
* **"someone sends a news with pictures and i reply 'what do you think'"** — a
  reply to a news post is meaningless without the news. Every group turn now
  carries a short transcript of the last few messages, with the author on each
  line, so "this" and "it" have something to point at.

The transcript is built from the chat the bot is already in, capped hard, and
only ever includes text (a picture is a placeholder, never a download). It is
assembled per turn and never stored: a group's conversation does not need to be
kept to answer one question about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ChatContext", "build"]

#: How far back a group turn looks. Twelve lines is enough for a thread and
#: short enough that the prompt stays about the current question.
TRANSCRIPT_LINES = 12
#: A transcript is a summary of the room, not a transcript of the day.
MAX_TRANSCRIPT_CHARS = 1400
#: Long single messages are cut rather than dropped.
MAX_LINE_CHARS = 180


@dataclass
class ChatContext:
    """The room, the speaker, and the last few things said in it."""

    chat_title: str = ""
    chat_id: int = 0
    speaker: str = ""
    speaker_id: int | None = None
    is_owner: bool = False
    is_group: bool = False
    transcript: list[str] = field(default_factory=list)
    #: How she is supposed to treat this speaker (see ``havoc.services.people``).
    tone: str = ""
    #: "his girlfriend", when the owner said so.
    relation: str = ""
    #: What she was told about them, oldest first.
    known: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.transcript or self.speaker or self.known)


def _display(message: Any) -> str:
    """A name for whoever wrote ``message``, without a network call if possible."""
    sender = getattr(message, "sender", None)
    for attribute in ("first_name", "title", "username"):
        value = getattr(sender, attribute, None) if sender is not None else None
        if value:
            return str(value)
    name = getattr(message, "post_author", None)
    if name:
        return str(name)
    sender_id = getattr(message, "sender_id", None)
    return f"id {sender_id}" if sender_id else "someone"


def _body(message: Any) -> str:
    text = " ".join(str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "").split())
    if text:
        return text[:MAX_LINE_CHARS]
    media = getattr(message, "media", None)
    if media is None:
        return ""
    for kind in ("photo", "video", "document", "voice", "audio", "sticker"):
        if getattr(media, kind, None) is not None:
            return f"[{kind}]"
    return "[media]"


async def build(
    runtime: Any,
    event: Any,
    *,
    limit: int = TRANSCRIPT_LINES,
    own_ids: set[int] | None = None,
) -> ChatContext:
    """The room around ``event``. Never raises; an empty context is still usable."""
    owner_id = getattr(runtime.permissions, "self_id", None)
    context = ChatContext(
        chat_id=int(getattr(event, "chat_id", 0) or 0),
        is_group=bool(getattr(event, "is_group", False)),
    )
    message = getattr(event, "raw", None)

    # -- who is speaking --------------------------------------------------
    sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
    context.speaker_id = int(sender_id) if sender_id else None
    # This account only writes as the owner or as her, and hers is filtered before
    # the room is built — so an outgoing message is his even when the id is
    # missing from the update.
    context.is_owner = bool(getattr(event, "is_outgoing", False)) or bool(
        owner_id and sender_id == owner_id
    )
    if message is not None:
        getter = getattr(message, "get_sender", None)
        if getter is not None:
            try:
                sender = await getter()
                if sender is not None:
                    message.sender = sender
            except Exception:  # noqa: BLE001 - a missing sender is not an error
                pass
        context.speaker = "the owner" if context.is_owner else _display(message)

    # -- what she knows about them ----------------------------------------
    # Only people she actually answers get a row: a companion that keeps a file
    # on every member of every group is surveillance, which this is not.
    book = getattr(runtime, "people", None)
    if context.is_group and book is not None and context.speaker_id and not context.is_owner:
        try:
            person = await book.touch(context.speaker_id, context.speaker)
        except Exception:  # noqa: BLE001 - the room must work without the book
            person = None
        if person is not None:
            context.tone = person.tone
            context.relation = person.relation
            context.known = tuple(person.facts)
            if person.name:
                context.speaker = person.name

    # -- a name for the room ----------------------------------------------
    chat = getattr(message, "chat", None)
    title = getattr(chat, "title", None) or getattr(chat, "username", None)
    if title:
        context.chat_title = str(title)

    # -- what has been said -----------------------------------------------
    if not context.is_group:
        return context
    client = getattr(runtime, "client", None)
    if client is None:
        return context
    try:
        raw_messages = await client.get_messages(context.chat_id, limit=max(2, int(limit)))
    except Exception:  # noqa: BLE001 - context is a bonus, not a dependency
        return context
    messages = list(reversed(list(raw_messages or [])))
    if not messages:
        return context
    ids = own_ids or set()
    total = 0
    for item in messages:
        body = _body(item)
        if not body:
            continue
        item_id = getattr(item, "id", None)
        sender = getattr(item, "sender_id", None)
        if item_id is not None and int(item_id) in ids:
            who = "yura (you)"
        elif owner_id and sender == owner_id:
            who = "the owner"
        elif getattr(item, "out", False):
            who = "yura (you)" if item_id in ids else "the owner"
        else:
            who = _display(item)
        line = f"- {who}: {body}"
        total += len(line)
        if total > MAX_TRANSCRIPT_CHARS:
            break
        context.transcript.append(line)
    return context
