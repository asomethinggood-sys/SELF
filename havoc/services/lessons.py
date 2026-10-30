"""Corrections: how he teaches her, and how she keeps it.

There is no filter in front of her mouth. The owner's instruction was explicit:
she may swear, she may be nasty, and when she gets it wrong he says so — and
*that* is the mechanism. A blocklist would have made her a token eater that
never changes; a lesson makes her someone who was told once.

So a correction is stored as a sentence she agreed to, in ``yura_lessons``:

* ``scope='global'`` — about her, in every chat (“stop swearing at me”);
* ``scope='person'`` — about one person (“don't call her that”), so the lesson
  travels with them the way the facts do.

The rule is injected into the prompt as something that is *hers* now, not as a
rule imposed by a machine. When a later answer walks over one, the lesson is not
suppressed: it is counted, and the next turn tells her — so she can own it in her
own voice ("i know, i know, you told me") and stop doing it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..utils.time import iso, utc_now

__all__ = ["Lesson", "LessonBook", "extract_correction", "forbidden_tokens"]

MAX_RULE_CHARS = 280
#: A person’s lessons are few: this is a personality, not a policy document.
MAX_LESSONS = 60


@dataclass(frozen=True)
class Lesson:
    id: int
    scope: str
    subject_id: int
    rule: str
    tokens: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""
    times_broken: int = 0
    last_broken_at: str = ""
    created_at: str = ""

    def line(self) -> str:
        return self.rule

    @property
    def reminder(self) -> str:
        """What she is told about a rule she keeps walking over."""
        return f"{self.rule} (you forgot this {self.times_broken} time(s))"


def _normalise(text: str) -> str:
    cleaned = str(text).translate(str.maketrans({"\u200c": "", "\u200f": "", "\u200e": ""}))
    return re.sub(r"\s+", " ", cleaned).casefold().strip()


#: Ways he corrects her. The payload is kept as *his* sentence, because a rule
#: she remembers in her own words is one she can argue with.
_CORRECTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:please\s+)?(?:don'?t|do not|never|stop)\s+"
        r"(?:say|saying|use|using|write|writing|call(?:ing)?(?:\s+me)?)\s+(?P<rule>.+)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*(?:please\s+)?(?:don'?t|do not|never|stop)\s+(?P<rule>(?:swear|swearing|curse|"
        r"cursing|insult|insulting|threaten|threatening|shout|shouting|yell|yelling|be rude|"
        r"being rude|be mean|being mean|talk to me like|treat me like).*)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*(?:don'?t|do not|never|stop)\s+(?:do|doing)\s+that\b(?P<rule>.*)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*(?:that was|that'?s|thats)\s+(?:too much|not ok|not okay|rude|mean|out of line)\b"
        r"(?P<rule>.*)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*be\s+(?P<rule>(?:nicer|kinder|more polite|less rude|softer|sweeter).*)$",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"^\s*(?:این\s*(?:کارو|کار\s*را)\s*نکن|دیگه\s*(?:نگو|فحش\s*نده)|فحش\s*نده|"
        r"بی\s*ادب\s*نباش|ادب\s*داشته\s*باش|مودب\s*باش|ناسزا\s*نگو)\s*(?P<rule>.*)$",
        re.DOTALL,
    ),
)

#: A correction is a sentence, not a paragraph; and a question is never one.
MAX_CORRECTION_CHARS = 160
#: ...and neither is a compliment wearing the same coat ("don't say that, you're
#: sweet"). Storing those would fill her prompt with rules he never meant.
_NOT_A_CORRECTION = (
    "you're sweet",
    "youre sweet",
    "you are sweet",
    "you're too kind",
    "love you",
    "i love",
    "thank you",
    "thanks",
    "just kidding",
    "jk",
    "خوشم میاد",
    "دوستت دارم",
    "شوخی کردم",
)

#: Where the banned thing sits inside "don't say X": quoted, or after a verb.
_QUOTED = re.compile(r"[\"«“]([^\"»”]{2,40})[\"»”]")
_AFTER_VERB = re.compile(
    r"(?:say|saying|use|using|write|writing|call(?:ing)?(?:\s+me)?)\s+(?P<what>[^\s,.;!?]{2,40})",
    re.IGNORECASE,
)


def forbidden_tokens(rule: str) -> tuple[str, ...]:
    """The words a rule forbids, or ``()`` when it is a behaviour, not a word.

    "don't say 'bro'" has a token; "be nicer" does not — the second kind is
    honoured by reading it, not by matching against it.
    """
    tokens: list[str] = []
    for match in _QUOTED.finditer(rule or ""):
        tokens.append(match.group(1).strip())
    if not tokens:
        for match in _AFTER_VERB.finditer(rule or ""):
            word = match.group("what").strip().strip(".,!?;:،\"'«»“”")
            if word and word.lower() not in {"that", "this", "it", "at", "me", "like"}:
                tokens.append(word)
    seen: list[str] = []
    for token in tokens:
        if token and token.lower() not in {item.lower() for item in seen}:
            seen.append(token)
    return tuple(seen)


def extract_correction(text: str) -> str:
    """The correction he just gave her, or ``""``.

    Returns his sentence, lightly cleaned — not a normalised command. She should
    remember being told, not a rule number.
    """
    if not text:
        return ""
    # "yura stop swearing at me" is the same correction as "stop swearing at me";
    # the name in front of it is how he addresses her, not part of the lesson.
    stripped = " ".join(text.split())
    stripped = re.sub(
        r"^(?:hey|ok|okay|yo)?\s*(?:yura|یورا)[\s,:،-]*", "", stripped, flags=re.IGNORECASE
    )
    if len(stripped) > MAX_CORRECTION_CHARS or stripped.endswith("?"):
        return ""
    lowered = stripped.casefold()
    if any(soft in lowered for soft in _NOT_A_CORRECTION):
        return ""
    for pattern in _CORRECTIONS:
        if pattern.match(stripped) is not None:
            return stripped.strip(" .,:،;")[:MAX_RULE_CHARS]
    return ""


def _row(row: Any) -> Lesson:
    try:
        tokens = json.loads(row["tokens"] or "[]")
    except (TypeError, ValueError):
        tokens = []
    return Lesson(
        id=int(row["id"]),
        scope=str(row["scope"] or "global"),
        subject_id=int(row["subject_id"] or 0),
        rule=str(row["rule"] or ""),
        tokens=tuple(str(token) for token in tokens),
        source=str(row["source"] or ""),
        times_broken=int(row["times_broken"] or 0),
        last_broken_at=str(row["last_broken_at"] or ""),
        created_at=str(row["created_at"] or ""),
    )


class LessonBook:
    """Read/write access to ``yura_lessons``."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def add(
        self,
        rule: str,
        *,
        scope: str = "global",
        subject_id: int | None = None,
        source: str = "",
    ) -> Lesson | None:
        """Keep a correction. Returns ``None`` when she had already learned it."""
        text = " ".join(str(rule or "").split())[:MAX_RULE_CHARS].strip(" .,:،;")
        if not text:
            return None
        scope = scope if scope in {"global", "person"} else "global"
        subject = int(subject_id or 0) if scope == "person" else 0
        existing = await self.db.fetch_all(
            "SELECT * FROM yura_lessons WHERE scope = ? AND subject_id = ?",
            (scope, subject),
        )
        needle = _normalise(text)
        for row in existing:
            if _normalise(_row(row).rule) == needle:
                return None
        tokens = forbidden_tokens(text)
        lesson_id = await self.db.insert(
            "INSERT INTO yura_lessons(scope, subject_id, rule, tokens, source, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (scope, subject, text, json.dumps(list(tokens), ensure_ascii=False), str(source)[:400], iso(utc_now())),
        )
        return await self.get(lesson_id)

    async def get(self, lesson_id: int | None) -> Lesson | None:
        if lesson_id is None:
            return None
        row = await self.db.fetch_one("SELECT * FROM yura_lessons WHERE id = ?", (int(lesson_id),))
        return _row(row) if row is not None else None

    async def for_person(self, subject_id: int | None, limit: int = 30) -> list[Lesson]:
        """Everything she has been taught, plus what is specifically about them."""
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_lessons WHERE scope = 'global' OR"
            " (scope = 'person' AND subject_id = ?) ORDER BY id ASC LIMIT ?",
            (int(subject_id or 0), max(1, int(limit))),
        )
        return [_row(row) for row in rows]

    async def all(self, limit: int = MAX_LESSONS) -> list[Lesson]:
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_lessons ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        )
        return [_row(row) for row in rows]

    async def count(self) -> int:
        return int(await self.db.scalar("SELECT COUNT(*) FROM yura_lessons", default=0) or 0)

    async def remove(self, which: str) -> int:
        """Forget a lesson by id (``#3``) or by a phrase from it."""
        which = str(which or "").strip()
        if not which:
            return 0
        if which.lstrip("#").isdigit():
            return int(
                await self.db.execute(
                    "DELETE FROM yura_lessons WHERE id = ?", (int(which.lstrip("#")),)
                )
            )
        needle = _normalise(which)
        rows = await self.db.fetch_all("SELECT * FROM yura_lessons")
        doomed = [int(row["id"]) for row in rows if needle in _normalise(str(row["rule"] or ""))]
        for lesson_id in doomed:
            await self.db.execute("DELETE FROM yura_lessons WHERE id = ?", (lesson_id,))
        return len(doomed)

    async def note_break(self, lesson: Lesson) -> None:
        """She walked over one of her own rules. Count it; never suppress it."""
        await self.db.execute(
            "UPDATE yura_lessons SET times_broken = times_broken + 1, last_broken_at = ?"
            " WHERE id = ?",
            (iso(utc_now()), lesson.id),
        )

    async def violations(self, text: str, lessons: list[Lesson]) -> list[Lesson]:
        """Which of her own rules this answer walked over."""
        if not text:
            return []
        lowered = _normalise(text)
        hits: list[Lesson] = []
        for lesson in lessons:
            for token in lesson.tokens:
                needle = _normalise(token)
                if needle and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", lowered):
                    hits.append(lesson)
                    break
        return hits

    async def recent_break(self, subject_id: int | None) -> Lesson | None:
        """The rule she last forgot, so the next turn can own it."""
        rows = await self.db.fetch_all(
            "SELECT * FROM yura_lessons WHERE last_broken_at IS NOT NULL AND"
            " (scope = 'global' OR (scope = 'person' AND subject_id = ?))"
            " ORDER BY last_broken_at DESC LIMIT 1",
            (int(subject_id or 0),),
        )
        return _row(rows[0]) if rows else None
