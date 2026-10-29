"""The offline corpora Yura reads from.

Two data files ship inside the package (``havoc/data``), both built by
``scripts/build_*.py`` from public-domain sources and both loaded lazily:

``poems.json.gz``
    ~3,700 poems from the classical Persian canon (Ganjoor) and from public
    domain English poets (Project Gutenberg). Fields: ``title``, ``poet``,
    ``language``, ``text``.
``fals.json.gz``
    495 Hafez readings: the couplets and the traditional interpretation.
    Fields: ``id``, ``verses``, ``interpretation``.

Nothing here talks to the network and nothing is imported at start-up: a bot
that never runs ``.poem`` never pays for the ~1 MB of text.
"""

from __future__ import annotations

import gzip
import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
POEMS_PATH = DATA_DIR / "poems.json.gz"
FALS_PATH = DATA_DIR / "fals.json.gz"


class CorpusError(Exception):
    """The shipped data file is missing or unreadable."""


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CorpusError(f"missing corpus file: {path.name}")
    try:
        with gzip.open(path, "rb") as handle:
            return json.loads(handle.read())
    except (OSError, ValueError) as exc:  # corrupt download, truncated file
        raise CorpusError(f"unreadable corpus file: {path.name}") from exc


@lru_cache(maxsize=1)
def _poems() -> tuple[dict[str, Any], ...]:
    return tuple(_load(POEMS_PATH).get("poems", ()))


@lru_cache(maxsize=1)
def _fals() -> tuple[dict[str, Any], ...]:
    return tuple(_load(FALS_PATH).get("readings", ()))


def poem_count() -> int:
    return len(_poems())


def fal_count() -> int:
    return len(_fals())


def poets() -> dict[str, int]:
    """Poet name → number of poems, most prolific first."""
    counts: dict[str, int] = {}
    for poem in _poems():
        counts[poem["poet"]] = counts.get(poem["poet"], 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def languages() -> dict[str, int]:
    counts: dict[str, int] = {}
    for poem in _poems():
        counts[poem["language"]] = counts.get(poem["language"], 0) + 1
    return counts


def resolve_poet(query: str) -> str | None:
    """Match a user-typed poet name, in either script.

    Exact match wins; then a case-insensitive substring match ("hafez" → حافظ,
    "شکسپیر" → William Shakespeare is not expected to work, Latin names are).
    """
    needle = query.strip().lower()
    if not needle:
        return None
    table = poets()
    for name in table:
        if name.lower() == needle:
            return name
    for name in table:
        if needle in name.lower():
            return name
    # A Latin query against a Persian name: try a small, honest alias table.
    aliases = {
        "hafez": "حافظ",
        "rumi": "مولوی",
        "mowlana": "مولوی",
        "saadi": "سعدی",
        "khayyam": "خیام",
        "attar": "عطار",
        "ferdowsi": "فردوسی",
        "nevisi": "نظامی",
        "nezami": "نظامی",
        "shakespeare": "William Shakespeare",
        "dickinson": "Emily Dickinson",
        "housman": "A. E. Housman",
        "tennyson": "Alfred, Lord Tennyson",
        "blake": "William Blake",
        "tagore": "Rabindranath Tagore",
    }
    target = aliases.get(needle)
    if target and target in table:
        return target
    return None


#: Persian text is written with several interchangeable letters; a search that
#: treats them as different is a search nobody uses twice.
_FOLD = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا"})


def fold(text: str) -> str:
    return text.translate(_FOLD)


def search_poems(query: str, *, language: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Poems whose text or title contains ``query``, folded for Persian spelling.

    The corpus is small enough (a few MB) that a linear scan beats any index, and
    an index would have to be rebuilt every time the data file changes.
    """
    needle = fold(query.strip().lower())
    if not needle:
        return []
    matches: list[dict[str, Any]] = []
    for poem in _poems():
        if language is not None and poem["language"] != language:
            continue
        haystack = fold((poem["text"] + " " + poem["title"]).lower())
        if needle in haystack:
            matches.append(poem)
            if len(matches) >= limit:
                break
    return matches


def sample_poem(
    *, language: str | None = None, poet: str | None = None, rng: random.Random | None = None
) -> dict[str, Any]:
    """One poem, filtered however the caller asked. Raises when nothing matches."""
    pool = [poem for poem in _poems() if (language is None or poem["language"] == language)]
    if poet is not None:
        pool = [poem for poem in pool if poem["poet"] == poet]
    if not pool:
        raise CorpusError("no poem matches that filter")
    picker = rng or random.SystemRandom()
    return picker.choice(pool)


def sample_fal(*, reading_id: int | None = None, rng: random.Random | None = None) -> dict[str, Any]:
    """One Hafez reading — the whole point of a fal is that it is not chosen."""
    readings = _fals()
    if not readings:
        raise CorpusError("no readings available")
    picker = rng or random.SystemRandom()
    if reading_id is None:
        return picker.choice(readings)
    for reading in readings:
        if int(reading["id"]) == int(reading_id):
            return reading
    raise CorpusError(f"no reading #{reading_id}")
