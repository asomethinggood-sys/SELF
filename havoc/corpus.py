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


#: Transliterations and nicknames people actually type, mapped to the name the
#: corpus uses. Anything not here still resolves by substring, in either script.
POET_ALIASES: dict[str, str] = {
    "hafez": "حافظ",
    "hafiz": "حافظ",
    "rumi": "مولوی",
    "mowlana": "مولوی",
    "molana": "مولوی",
    "mevlana": "مولوی",
    "saadi": "سعدی",
    "sadi": "سعدی",
    "khayyam": "خیام",
    "khayam": "خیام",
    "omar": "خیام",
    "attar": "عطار",
    "ferdowsi": "فردوسی",
    "ferdosi": "فردوسی",
    "nezami": "نظامی",
    "saeb": "صائب تبریزی",
    "bidel": "بیدل دهلوی",
    "bedil": "بیدل دهلوی",
    "iraj": "ایرج میرزا",
    "irej": "ایرج میرزا",
    "parvin": "پروین اعتصامی",
    "bahar": "ملک‌الشعرای بهار",
    "malekolshoara": "ملک‌الشعرای بهار",
    "jami": "جامی",
    "roodaki": "رودکی",
    "rudaki": "رودکی",
    "mahsati": "مهستی گنجوی",
    "shakespeare": "William Shakespeare",
    "housman": "A. E. Housman",
    "tennyson": "Alfred, Lord Tennyson",
    "blake": "William Blake",
    "fitzgerald": "Omar Khayyam (tr. Edward FitzGerald)",
}

#: Topics, because "شعر شراب" should give a poem about wine and not a complaint
#: that no poet is called "شراب". Each topic is a set of words; a poem matches if
#: it contains any of them.
TOPICS: dict[str, tuple[str, ...]] = {
    "شراب": ("شراب", "می ", "می‌", "ساغر", "جام", "باده", "رطل", "خم ", "مستی", "ساقی"),
    "عشق": ("عشق", "دلبر", "یار", "معشوق", "وصال", "فراق"),
    "غم": ("غم", "اندوه", "حزن", "دلتنگی", "غصه"),
    "مرگ": ("مرگ", "اجل", "خاک", "قبر", "فنا"),
    "خدا": ("خدا", "الله", "حق ", "معبود", "پروردگار"),
    "طبیعت": ("گل", "بهار", "باد", "ابر", "چمن", "بلبل", "سرو"),
    "وطن": ("وطن", "ایران", "ملت", "میهن"),
    "زنان": ("زن", "دختر", "مادر", "همسر"),
    "حکمت": ("حکمت", "دانش", "عقل", "خرد", "علم"),
    "نصیحت": ("نصیحت", "پند", "موعظه", "اندرز"),
    "دوستی": ("دوست", "رفیق", "یاران", "همدم"),
    "شب": ("شب", "ماه", "ستاره", "سحر"),
    "wine": ("wine", "cup", "tavern", "drunk"),
    "love": ("love", "beloved", "heart", "kiss"),
    "death": ("death", "grave", "die", "dust"),
    "night": ("night", "moon", "star", "dawn"),
    "god": ("god", "lord", "heaven", "prayer"),
    "spring": ("spring", "flower", "rose", "garden"),
    "time": ("time", "hour", "year", "age"),
}

#: Every character that separates two words in Persian text but carries no
#: meaning for a lookup: ZWNJ, the zero-width joiner, and a plain space.
_NAME_NOISE = str.maketrans({"\u200c": "", "\u200d": "", " ": "", "\u0640": ""})


def _name_key(text: str) -> str:
    return fold(text.strip().lower().translate(_NAME_NOISE))


def topic_terms(query: str) -> tuple[str, ...]:
    """The words a topic stands for, or an empty tuple when it is not a topic."""
    key = query.strip().lower().replace("‌", "")
    for name, terms in TOPICS.items():
        if key == name.lower() or (len(key) >= 3 and key in name.lower()):
            return terms
    return ()


def resolve_poet(query: str) -> str | None:
    """Match a user-typed poet name, in either script.

    Exact match wins; then a case-insensitive substring match ("hafez" → حافظ,
    "شکسپیر" → William Shakespeare is not expected to work, Latin names are).
    """
    needle = query.strip().lower()
    if not needle:
        return None
    table = poets()
    # "ایرج‌میرزا", "ایرج میرزا" and "iraj" are the same request.
    key = _name_key(query)
    for name in table:
        if _name_key(name) == key:
            return name
    for name in table:
        if key and key in _name_key(name):
            return name
    # A Latin query against a Persian name: try the alias table.
    target = POET_ALIASES.get(needle) or POET_ALIASES.get(needle.replace(" ", ""))
    if target and target in table:
        return target
    return None


#: Persian text is written with several interchangeable letters; a search that
#: treats them as different is a search nobody uses twice.
_FOLD = str.maketrans({"ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا"})


def fold(text: str) -> str:
    return text.translate(_FOLD)


def search_poems(
    query: str,
    *,
    language: str | None = None,
    limit: int = 4000,
    terms: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Poems whose text or title contains ``query``, folded for Persian spelling.

    When ``terms`` is given, a poem matches if it contains any of them — that is
    how a topic like "شراب" becomes "wine, cup, tavern, ساغر, باده …".

    A linear scan over a few thousand poems is faster than maintaining an index
    that would have to be rebuilt whenever the data file changes.
    """
    needles = [fold(term.lower()) for term in (terms or (query,)) if term.strip()]
    if not needles:
        return []
    matches: list[dict[str, Any]] = []
    for poem in _poems():
        if language is not None and poem["language"] != language:
            continue
        haystack = fold((poem["text"] + " " + poem["title"]).lower())
        if any(needle in haystack for needle in needles):
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
