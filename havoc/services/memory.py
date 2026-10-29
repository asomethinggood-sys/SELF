"""Yura's memory: what she keeps about the owner, and the last few turns.

Two stores, both in the same SQLite file as everything else:

``yura_memory``
    Durable facts, one row each: "نامش سارا است", "works night shifts", "hates
    cilantro". Written either by the owner explicitly (``.yura remember …``) or
    by the model through a ``[[remember: …]]`` directive. Recall is a plain
    "most recent, heaviest first" read: with a few dozen rows, ranking with a
    vector database would be pretence.

``yura_history``
    A rolling window of the conversation, so a reply has context instead of
    amnesia. Trimmed to :data:`HISTORY_TURNS` messages per chat on every write.

Nothing here talks to the network and nothing here is ever sent to a third
party on its own: recall is only read when the owner addresses Yura, and the
only thing that leaves the machine is the prompt built by
:mod:`havoc.services.yura_chat`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..utils.time import iso, utc_now

#: How many past messages are replayed to the model per chat.
HISTORY_TURNS = 20
#: Upper bound on a single memory: a fact, not an essay.
MAX_MEMORY_CHARS = 500
#: How many facts are injected into a prompt.
RECALL_LIMIT = 40

KINDS = ("fact", "preference", "note", "person")


@dataclass(frozen=True)
class Memory:
    id: int
    chat_id: int
    kind: str
    content: str
    weight: int
    created_at: str

    def line(self) -> str:
        """One line for a prompt or the ``.yura memory`` list."""
        return self.content.replace("\n", " ").strip()


class MemoryStore:
    """Read/write access to the two Yura tables."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- memories ------------------------------------------------------
    async def remember(
        self, chat_id: int, content: str, *, kind: str = "fact", weight: int = 1
    ) -> int | None:
        """Store ``content`` unless an equivalent fact is already known.

        Returns the new row id, or ``None`` when it was a duplicate — the same
        fact tends to come up repeatedly in conversation, and a memory that
        repeats itself is worse than one that is short.
        """
        text = " ".join(str(content).split())[:MAX_MEMORY_CHARS].strip()
        if not text:
            return None
        kind = kind if kind in KINDS else "fact"
        existing = await self.db.fetch_all(
            "SELECT id, content FROM yura_memory WHERE chat_id = ? ORDER BY id DESC LIMIT 500",
            (chat_id,),
        )
        needle = _key(text)
        for row in existing:
            if _key(str(row["content"])) == needle:
                return None
        return await self.db.insert(
            "INSERT INTO yura_memory(chat_id, kind, content, weight, created_at) VALUES(?, ?, ?, ?, ?)",
            (chat_id, kind, text, max(1, min(int(weight), 9)), iso(utc_now())),
        )

    async def recall(self, chat_id: int, *, limit: int = RECALL_LIMIT) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_memory WHERE chat_id = ? ORDER BY weight DESC, id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [_memory(row) for row in rows]

    async def list(self, chat_id: int, *, limit: int = 50) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_memory WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [_memory(row) for row in rows]

    async def forget(self, chat_id: int, memory_id: int) -> bool:
        removed = await self.db.execute(
            "DELETE FROM yura_memory WHERE chat_id = ? AND id = ?", (chat_id, memory_id)
        )
        return bool(removed)

    async def forget_matching(self, chat_id: int, query: str) -> int:
        """Forget every fact that mentions ``query`` (case/ZWNJ-insensitive)."""
        needle = _key(query)
        if not needle:
            return 0
        rows = await self.db.fetch_all(
            "SELECT id, content FROM yura_memory WHERE chat_id = ?", (chat_id,)
        )
        doomed = [int(row["id"]) for row in rows if needle in _key(str(row["content"]))]
        for memory_id in doomed:
            await self.db.execute("DELETE FROM yura_memory WHERE id = ?", (memory_id,))
        return len(doomed)

    async def clear(self, chat_id: int) -> int:
        return await self.db.execute("DELETE FROM yura_memory WHERE chat_id = ?", (chat_id,))

    async def count(self, chat_id: int) -> int:
        return int(
            await self.db.scalar(
                "SELECT COUNT(*) FROM yura_memory WHERE chat_id = ?", (chat_id,), default=0
            )
            or 0
        )

    # -- history -------------------------------------------------------
    async def append(self, chat_id: int, role: str, content: str) -> None:
        text = str(content).strip()
        if not text or role not in {"user", "assistant"}:
            return
        await self.db.execute(
            "INSERT INTO yura_history(chat_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            (chat_id, role, text[:4000], iso(utc_now())),
        )
        # Trim everything older than the window in one statement: a growing
        # table is the classic way a "small" feature eats the disk.
        await self.db.execute(
            """
            DELETE FROM yura_history
             WHERE chat_id = ?
               AND id NOT IN (
                   SELECT id FROM yura_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?
               )
            """,
            (chat_id, chat_id, HISTORY_TURNS),
        )

    async def history(self, chat_id: int, *, limit: int = HISTORY_TURNS) -> list[tuple[str, str]]:
        rows = await self.db.fetch_all(
            "SELECT role, content FROM yura_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [(str(row["role"]), str(row["content"])) for row in reversed(rows)]

    async def clear_history(self, chat_id: int) -> int:
        return await self.db.execute("DELETE FROM yura_history WHERE chat_id = ?", (chat_id,))

    # -- helpers -------------------------------------------------------
    async def transcript(self, chat_id: int, messages: Sequence[tuple[str, str]]) -> str:
        """A readable transcript, used by ``.yura log`` and by the prompt."""
        lines = []
        for role, content in messages:
            who = "me" if role == "user" else "yura"
            lines.append(f"{who}: {content}")
        return "\n".join(lines)


def _memory(row: Any) -> Memory:
    return Memory(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        kind=str(row["kind"]),
        content=str(row["content"]),
        weight=int(row["weight"]),
        created_at=str(row["created_at"]),
    )


_ZWNJ = str.maketrans({"\u200c": "", "\u200d": "", "\u200f": "", "\u200e": ""})


def _key(text: str) -> str:
    """Comparison key: casefolded, punctuation-light, ZWNJ-free Persian."""
    cleaned = text.translate(_ZWNJ).casefold()
    return " ".join(cleaned.replace("،", " ").replace(".", " ").split())
