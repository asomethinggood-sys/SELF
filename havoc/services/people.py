"""People: what she knows about anyone she talks to, and how she treats them.

The owner asked for two things at once, and they are the same table:

* **"only nice to me, rude to others — even swears"** — so every person carries
  a ``tone``: ``rude`` by default for anyone who is not the owner, and ``nice``
  when the owner says so (``.yura nice`` while replying to them).
* **"she might be talking to my gf, and she should remember that when I tell
  her"** — so every person carries ``facts`` and a ``relation``: a short list of
  sentences, one of which can be "his girlfriend".

One row per person, created the first time she answers them and never for
someone she merely saw in a group: a companion that quietly builds a file on
every member of every chat is surveillance, and this project does not do that.

Nothing here is sent anywhere on its own. The only thing that leaves the machine
is the prompt built by :mod:`havoc.services.yura_chat`, which reads one person's
row at a time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..utils.time import iso, utc_now

__all__ = ["Person", "PersonBook", "TONES", "DEFAULT_TONE", "detect_relation"]

#: How she treats someone. ``rude`` is the standing default for everyone who is
#: not the owner; ``nice`` is what the owner switches on with one command.
TONES = ("rude", "nice", "neutral")
DEFAULT_TONE = "rude"
#: A fact about a person, not an essay.
MAX_FACT_CHARS = 300
#: Enough to know someone. Beyond this the oldest fact is dropped.
MAX_FACTS = 40

#: Relation words, both languages, mapped to one canonical label. The label is
#: what ends up in the prompt, so the list is deliberately small and specific.
RELATIONS: dict[str, str] = {
    "gf": "his girlfriend",
    "girlfriend": "his girlfriend",
    "girl friend": "his girlfriend",
    "دوست دخترم": "his girlfriend",
    "دوستدخترم": "his girlfriend",
    "دوست‌دخترم": "his girlfriend",
    "bf": "his boyfriend",
    "boyfriend": "his boyfriend",
    "دوست پسرم": "his boyfriend",
    "دوستپسرم": "his boyfriend",
    "wife": "his wife",
    "همسرم": "his wife",
    "زنم": "his wife",
    "husband": "his husband",
    "شوهرم": "his husband",
    "fiance": "his fiancé",
    "fiancée": "his fiancé",
    "نامزدم": "his fiancé",
    "sister": "his sister",
    "خواهرم": "his sister",
    "brother": "his brother",
    "برادرم": "his brother",
    "mother": "his mother",
    "mom": "his mother",
    "mum": "his mother",
    "مادرم": "his mother",
    "مامانم": "his mother",
    "father": "his father",
    "dad": "his father",
    "بابام": "his father",
    "پدرم": "his father",
    "cousin": "his cousin",
    "پسرخالهم": "his cousin",
    "دخترخالهم": "his cousin",
    "friend": "his friend",
    "best friend": "his best friend",
    "دوستش": "his friend",
    "رفیقم": "his best friend",
    "colleague": "his colleague",
    "coworker": "his colleague",
    "co-worker": "his colleague",
    "همکارم": "his colleague",
    "boss": "his boss",
    "رئیسم": "his boss",
    "roommate": "his roommate",
    "همخونم": "his roommate",
    "همخانه‌ای": "his roommate",
    "classmate": "his classmate",
    "همکلاسیم": "his classmate",
    "ex": "his ex",
    "سابقش": "his ex",
}

#: "she is my gf", "this is my sister", "he's my boss" — and the Persian
#: equivalents, which put the relation last: "دوستدخترمه".
_EN_POSSESSIVE = re.compile(
    r"\b(?:is|he's|she's|they're|that's|this is)\s+my\s+(?P<rel>[a-z][a-z \-]{1,24})",
    re.IGNORECASE,
)
_FA_POSSESSIVE = re.compile(
    r"(?:دوست\s?دختر|دوست\s?پسر|همسر|نامزد|خواهر|برادر|مادر|مامان|پدر|بابا|"
    r"همکار|رئیس|رفیق|دوست|همکلاسی|همخونه|همخانه|زن|شوهر|سابق)"
    r"(?:م|مه|ش|شه|شون|شونه)\b"
)


@dataclass(frozen=True)
class Person:
    """One row of :class:`PersonBook`."""

    user_id: int
    name: str = ""
    relation: str = ""
    tone: str = DEFAULT_TONE
    facts: tuple[str, ...] = field(default_factory=tuple)
    #: True when she may answer this person's private messages. Off by default:
    #: a stranger must not be able to start talking to his account.
    dm: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def label(self) -> str:
        """What to call them in a prompt."""
        return self.name or f"the person with id {self.user_id}"


    def line(self) -> str:
        """One line for a list command."""
        bits = [self.label]
        if self.relation:
            bits.append(self.relation)
        bits.append(self.tone)
        if self.dm:
            bits.append("dm ✓")
        if self.facts:
            bits.append(f"{len(self.facts)} facts")
        return " · ".join(bits)


def _normalise(text: str) -> str:
    """Comparison key: no ZWNJ, single spaces, casefolded."""
    cleaned = str(text).translate(str.maketrans({"\u200c": "", "\u200f": "", "\u200e": ""}))
    return re.sub(r"\s+", " ", cleaned).casefold().strip()


def detect_relation(text: str) -> str:
    """The relation this sentence states, or ``""``.

    "she's my gf" → *his girlfriend*. This is deliberately a small, explicit
    matcher: guessing a relation from a paragraph would put words in the owner's
    mouth, and being wrong about who someone is is worse than not knowing.
    """
    if not text:
        return ""
    lowered = _normalise(text)

    # English: "she is my gf", "that's my boss".
    match = _EN_POSSESSIVE.search(lowered)
    if match is not None:
        candidate = match.group("rel").strip().rstrip(".,!?;:")
        for key in sorted(RELATIONS, key=len, reverse=True):
            if _normalise(key) in ("",):
                continue
            if candidate == key or candidate.startswith(key + " "):
                return RELATIONS[key]

    # English without a copula: "my gf", "my best friend".
    for key in sorted(RELATIONS, key=len, reverse=True):
        if key.isascii() and re.search(rf"\bmy {re.escape(key)}\b", lowered):
            return RELATIONS[key]

    # Persian puts the relation last: "دوست‌دخترمه" (she is my girlfriend).
    if _FA_POSSESSIVE.search(lowered):
        for key in sorted(RELATIONS, key=len, reverse=True):
            needle = _normalise(key)
            if needle and not key.isascii() and needle in lowered:
                return RELATIONS[key]
    return ""


def _row(row: Any) -> Person | None:
    if row is None:
        return None
    try:
        facts = json.loads(row["facts"] or "[]")
    except (TypeError, ValueError):
        facts = []
    tone = str(row["tone"] or DEFAULT_TONE)
    return Person(
        user_id=int(row["user_id"]),
        name=str(row["name"] or ""),
        relation=str(row["relation"] or ""),
        tone=tone if tone in TONES else DEFAULT_TONE,
        facts=tuple(str(fact) for fact in facts if str(fact).strip()),
        dm=bool(int(row["dm"] or 0)) if "dm" in row.keys() else False,
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


class PersonBook:
    """Read/write access to ``yura_people``."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def get(self, user_id: int | None) -> Person | None:
        if user_id is None:
            return None
        row = await self.db.fetch_one("SELECT * FROM yura_people WHERE user_id = ?", (int(user_id),))
        return _row(row)

    async def touch(self, user_id: int | None, name: str = "") -> Person | None:
        """Create the row for someone she answered, and keep their name current.

        ``name`` is only written when it is new or changed, so a rename in
        Telegram shows up without a write on every message.
        """
        if user_id is None:
            return None
        person = await self.get(user_id)
        now = iso(utc_now())
        clean = " ".join(str(name or "").split())[:80]
        if person is None:
            await self.db.execute(
                "INSERT INTO yura_people(user_id, name, relation, tone, facts, created_at, updated_at)"
                " VALUES(?, ?, '', ?, '[]', ?, ?)",
                (int(user_id), clean, DEFAULT_TONE, now, now),
            )
            return await self.get(user_id)
        if clean and clean != person.name:
            await self.db.execute(
                "UPDATE yura_people SET name = ?, updated_at = ? WHERE user_id = ?",
                (clean, now, int(user_id)),
            )
            return await self.get(user_id)
        return person

    async def note(
        self,
        user_id: int | None,
        fact: str,
        *,
        relation: str = "",
        name: str = "",
    ) -> bool:
        """Keep one sentence about a person. Returns ``False`` for a repeat."""
        if user_id is None:
            return False
        text = " ".join(str(fact or "").split())[:MAX_FACT_CHARS].strip()
        if not text:
            return False
        person = await self.touch(user_id, name)
        if person is None:
            return False
        if any(_normalise(existing) == _normalise(text) for existing in person.facts):
            return False
        facts = list(person.facts) + [text]
        if len(facts) > MAX_FACTS:
            facts = facts[-MAX_FACTS:]
        updates = ["facts = ?", "updated_at = ?"]
        values: list[Any] = [json.dumps(facts, ensure_ascii=False), iso(utc_now())]
        derived = relation or detect_relation(text)
        if derived and derived != person.relation:
            updates.append("relation = ?")
            values.append(derived)
        values.append(int(user_id))
        await self.db.execute(
            f"UPDATE yura_people SET {', '.join(updates)} WHERE user_id = ?", tuple(values)
        )
        return True


    async def set_tone(self, user_id: int | None, tone: str) -> bool:
        if user_id is None or tone not in TONES:
            return False
        await self.touch(user_id)
        await self.db.execute(
            "UPDATE yura_people SET tone = ?, updated_at = ? WHERE user_id = ?",
            (tone, iso(utc_now()), int(user_id)),
        )
        return True

    async def set_dm(self, user_id: int | None, enabled: bool) -> bool:
        """Allow (or stop) her answering this person's private messages."""
        if user_id is None:
            return False
        await self.touch(user_id)
        await self.db.execute(
            "UPDATE yura_people SET dm = ?, updated_at = ? WHERE user_id = ?",
            (1 if enabled else 0, iso(utc_now()), int(user_id)),
        )
        return True

    async def forget(self, user_id: int | None, query: str = "") -> int:
        """Forget one sentence, or every sentence, about a person."""
        person = await self.get(user_id)
        if person is None:
            return 0
        needle = _normalise(query)
        if not needle:
            removed = len(person.facts)
            await self.db.execute(
                "UPDATE yura_people SET facts = '[]', updated_at = ? WHERE user_id = ?",
                (iso(utc_now()), int(user_id)),
            )
            return removed
        kept = [fact for fact in person.facts if needle not in _normalise(fact)]
        removed = len(person.facts) - len(kept)
        if removed:
            await self.db.execute(
                "UPDATE yura_people SET facts = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(kept, ensure_ascii=False), iso(utc_now()), int(user_id)),
            )
        return removed

    async def prune(self) -> int:
        """Delete the rows that hold nothing.

        A row is created when she answers someone, and it is only worth keeping
        once something is in it: a name is not a memory. This sweeps the empty
        ones — including any created by an older build that looked at a stranger's
        message without answering it — so the table means "people I know", not
        "people who have ever written to this account".
        """
        removed = await self.db.execute(
            "DELETE FROM yura_people WHERE facts = '[]' AND relation = '' AND dm = 0"
            " AND tone = ?",
            (DEFAULT_TONE,),
        )
        return int(removed or 0)

    async def all(self, limit: int = 40) -> list[Person]:
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_people ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),)
        )
        return [person for person in (_row(row) for row in rows) if person is not None]

    async def count(self) -> int:
        return int(
            await self.db.scalar("SELECT COUNT(*) FROM yura_people", default=0) or 0
        )

    async def search(self, query: str, limit: int = 5) -> list[Person]:
        """Find a person by name, so ``.yura nice Sara`` works without a reply."""
        needle = _normalise(query)
        if not needle:
            return []
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_people WHERE lower(name) LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{needle}%", max(1, int(limit))),
        )
        people = [person for person in (_row(row) for row in rows) if person is not None]
        return people
