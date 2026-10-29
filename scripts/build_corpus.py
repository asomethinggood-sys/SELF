#!/usr/bin/env python3
"""Build the poetry corpus that ships with Yura.

Two public-domain sources:

* **Persian — Ganjoor** (``ganjoor/ganjoor-db``, a MySQL dump of ganjoor.net):
  ~100k couplets from the classical canon. Downloaded once as a tarball, so
  the build does not hammer the API.
* **English — Project Gutenberg** (through the GITenberg mirror on GitHub):
  sonnet sequences and numbered poems, where the section boundaries are
  unambiguous (Roman numerals or ALL-CAPS titles).

Output: ``havoc/data/poems.json.gz``::

    {"built_at": "...", "sources": [...],
     "poems": [{"title", "poet", "language", "text"}, ...]}

Usage::

    python scripts/build_corpus.py --check            # verify the shipped file
    python scripts/build_corpus.py                    # full rebuild
    python scripts/build_corpus.py --persian 3000 --english 500

Downloads are cached under ``.cache/corpus`` (git-ignored), so a failed run
resumes instead of refetching.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "havoc" / "data" / "poems.json.gz"
CACHE = ROOT / ".cache" / "corpus"
API = "https://api.github.com"

#: Ganjoor poets we keep, with a sampling cap each. Only authors whose work is
#: long out of copyright: the classical canon, no modern or living poets.
#: The cap is a ceiling, not a promise: sampling spreads across each book.
PERSIAN_POETS: dict[str, int] = {
    "حافظ": 420,
    "مولوی": 420,
    "سعدی": 340,
    "صائب تبریزی": 260,
    "عطار": 300,
    "خیام": 200,
    "فردوسی": 130,
    "نظامی": 130,
    "سنایی": 200,
    "انوری": 160,
    "خاقانی": 160,
    "بیدل دهلوی": 200,
    "شاه نعمت‌الله ولی": 140,
    "ابوسعید ابوالخیر": 130,
    "باباطاهر": 110,
    "امیرخسرو دهلوی": 110,
    "اوحدی": 100,
    "فیض کاشانی": 100,
    "محتشم کاشانی": 100,
    "خواجوی کرمانی": 100,
    "سلمان ساوجی": 90,
    "وحشی": 90,
    "عراقی": 90,
    "ناصرخسرو": 90,
    "رودکی": 80,
    "قاآنی": 80,
    "فروغی بسطامی": 60,
    "عبید زاکانی": 60,
    "هلالی جغتایی": 60,
    "عرفی": 60,
}

#: Long masnavi passages do not fit a chat message, and short ruba'is are the
#: most shareable thing in the canon. The same window applies to every language,
#: so ``.poem`` always yields something that reads in one bubble.
MAX_POEM_CHARS = 900
MIN_POEM_CHARS = 80

#: Gutenberg books whose poems are delimited by a heading line.
#: ``mode``: "roman" = Roman numeral heading, "titled" = ALL-CAPS title heading.
GUTENBERG_BOOKS: tuple[dict[str, object], ...] = (
    {
        "repo": "GITenberg/Shakespeare-s-Sonnets_1041",
        "file": "1041.txt",
        "poet": "William Shakespeare",
        "work": "Sonnet",
        "mode": "roman",
        "limit": 154,
        "number": True,
    },
    {
        "repo": "GITenberg/A-Shropshire-Lad_5720",
        "file": "5720.txt",
        "poet": "A. E. Housman",
        "work": "A Shropshire Lad",
        "mode": "roman",
        "limit": 63,
        "number": True,
    },
    {
        "repo": "GITenberg/In-memoriam_70950",
        "file": "70950-0.txt",
        "poet": "Alfred, Lord Tennyson",
        "work": "In Memoriam A. H. H.",
        "mode": "roman",
        "limit": 131,
        "number": True,
    },
    {
        "repo": "GITenberg/The-Rubaiyat-of-Omar-Khayyam_246",
        "file": "246.txt",
        "poet": "Omar Khayyam (tr. Edward FitzGerald)",
        "work": "The Rubáiyát",
        "mode": "roman",
        "limit": 101,
        "number": True,
    },
    {
        "repo": "GITenberg/Songs-of-Innocence-and-Songs-of-Experience_1934",
        "file": "1934-0.txt",
        "poet": "William Blake",
        "work": "Songs of Innocence and of Experience",
        "mode": "titled",
        "limit": 60,
        "number": False,
    },
)

ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
ROMAN_RE = re.compile(r"^([IVXLCDM]{1,8})\.?$")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or _gh_token()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "yura-corpus-builder"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_token() -> str:
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=20, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _get(url: str, *, retries: int = 4) -> bytes:
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt + 1 < retries:
                time.sleep(5 + attempt * 5)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 < retries:
                time.sleep(2 + attempt * 2)
                continue
            raise
    raise RuntimeError("unreachable")


def _cached(url: str, name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists() and path.stat().st_size:
        return path.read_bytes()
    data = _get(url)
    path.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# Persian — Ganjoor SQL dump
# ---------------------------------------------------------------------------
def _ganjoor_dump() -> Path:
    """Return the path to the decompressed ``dump.sql``."""
    sql_path = CACHE / "ganjoor.sql"
    if sql_path.exists() and sql_path.stat().st_size > 1_000_000:
        return sql_path
    CACHE.mkdir(parents=True, exist_ok=True)
    tarball = CACHE / "ganjoor-db.tar.gz"
    if not (tarball.exists() and tarball.stat().st_size > 1_000_000):
        # codeload serves a whole-repo tarball: one request instead of thousands.
        url = "https://codeload.github.com/ganjoor/ganjoor-db/tar.gz/master"
        print("  downloading ganjoor dump (~30 MB)", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=300) as response, tarball.open("wb") as handle:
            while chunk := response.read(1 << 20):
                handle.write(chunk)
    with tarfile.open(tarball, "r:gz") as archive:
        member = next(name for name in archive.getnames() if name.endswith("dump.sql.gz"))
        extracted = archive.extractfile(member)
        assert extracted is not None
        with gzip.open(extracted, "rb") as source, sql_path.open("wb") as out:
            out.write(source.read())
    return sql_path


_UNESCAPE = {
    "0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b",
    "'": "'", '"': '"', "\\": "\\", "%": "\\%", "_": "\\_",
}


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(_UNESCAPE.get(value[index + 1], value[index + 1]))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


_ROW_SPLIT = re.compile(r"\),\(")


def _rows(line: str) -> list[list[str | None]]:
    """Parse one ``INSERT INTO ... VALUES (...),(...)`` statement."""
    start = line.find("VALUES")
    if start < 0:
        return []
    body = line[start + 6:].strip()
    if body.endswith(";"):
        body = body[:-1]
    body = body.strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    rows: list[list[str | None]] = []
    for raw in _ROW_SPLIT.split(body):
        fields: list[str | None] = []
        index = 0
        length = len(raw)
        field: list[str] = []
        while index < length:
            char = raw[index]
            if char == "'":
                index += 1
                while index < length:
                    current = raw[index]
                    if current == "\\" and index + 1 < length:
                        field.append(raw[index:index + 2])
                        index += 2
                        continue
                    if current == "'":
                        if index + 1 < length and raw[index + 1] == "'":
                            field.append("'")
                            index += 2
                            continue
                        break
                    field.append(current)
                    index += 1
                fields.append(_unescape("".join(field)))
                field = []
                index += 2  # skip the quote and the comma
            elif char == ",":
                if field:
                    fields.append("".join(field).strip() or None)
                    field = []
                index += 1
            else:
                field.append(char)
                index += 1
        if field:
            fields.append("".join(field).strip() or None)
        if fields:
            rows.append(fields)
    return rows


def _clean_persian(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def build_persian(limit: int) -> list[dict]:
    sql_path = _ganjoor_dump()
    poets: dict[str, str] = {}
    categories: dict[str, str] = {}  # category id -> poet id
    poems: dict[str, tuple[str, str]] = {}  # poem id -> (category id, title)
    verses: dict[str, list[tuple[int, int, str]]] = {}

    with sql_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith("INSERT INTO `"):
                continue
            if line.startswith("INSERT INTO `poets`"):
                for row in _rows(line):
                    if len(row) >= 2 and row[0] and row[1]:
                        poets[row[0]] = row[1]
            elif line.startswith("INSERT INTO `categories`"):
                for row in _rows(line):
                    if len(row) >= 4 and row[0] and row[1]:
                        categories[row[0]] = row[1]
            elif line.startswith("INSERT INTO `poems`"):
                for row in _rows(line):
                    if len(row) >= 3 and row[0]:
                        poems[row[0]] = (row[1] or "", row[2] or "")
            elif line.startswith("INSERT INTO `verses`"):
                for row in _rows(line):
                    if len(row) >= 5 and row[1] and row[4]:
                        try:
                            order = int(row[2] or 0)
                            position = int(row[3] or 0)
                        except ValueError:
                            continue
                        verses.setdefault(row[1], []).append((order, position, row[4]))

    wanted = {poet: cap for poet, cap in PERSIAN_POETS.items()}
    counts: dict[str, int] = {poet: 0 for poet in wanted}
    collected: list[dict] = []

    # Deterministic interleave across poets so a truncated run still has variety.
    by_poet: dict[str, list[tuple[str, str]]] = {poet: [] for poet in wanted}
    for poem_id, (category_id, title) in poems.items():
        poet_id = categories.get(category_id)
        poet = poets.get(poet_id or "")
        if poet in by_poet:
            by_poet[poet].append((poem_id, title))

    for poet in sorted(by_poet, key=lambda name: -wanted[name]):
        rows = sorted(by_poet[poet], key=lambda item: int(item[0]) if item[0].isdigit() else 0)
        # Spread the sample over the whole book instead of taking one chapter.
        step = max(1, len(rows) // max(1, wanted[poet]))
        for poem_id, title in rows[::step]:
            if counts[poet] >= wanted[poet] or len(collected) >= limit:
                break
            chunks = sorted(verses.get(poem_id, []))
            if len(chunks) < 4:
                continue
            # Ganjoor stores one hemistich per row: position 0 opens a line,
            # position 1 fills it. Pair them into bayts with a wide gap, the
            # way a divan prints them.
            lines: list[str] = []
            pending = ""
            for _order, position, text in chunks:
                if position:
                    lines.append(f"{pending}    {text}".rstrip() if pending else text)
                    pending = ""
                else:
                    if pending:
                        lines.append(pending)
                    pending = text
            if pending:
                lines.append(pending)
            text = _clean_persian("\n".join(lines))
            if not MIN_POEM_CHARS <= len(text) <= MAX_POEM_CHARS:
                continue
            collected.append(
                {
                    "title": (title or "").strip() or f"{poet} — شعر",
                    "poet": poet,
                    "language": "fa",
                    "text": text,
                }
            )
            counts[poet] += 1
        print(f"  {poet}: {counts[poet]}", file=sys.stderr)
    return collected


# ---------------------------------------------------------------------------
# English — Project Gutenberg
# ---------------------------------------------------------------------------
def _strip_gutenberg(text: str) -> str:
    start = re.search(r"\*\*\* ?START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", text)
    end = re.search(r"\*\*\* ?END OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", text)
    if start:
        text = text[start.end():]
    if end:
        text = text[: end.start()]
    return text


def _roman_to_int(token: str) -> int | None:
    token = token.upper().rstrip(".")
    total = 0
    previous = 0
    for char in reversed(token):
        value = ROMAN_VALUES.get(char)
        if value is None:
            return None
        total = total - value if value < previous else total + value
        previous = max(previous, value)
    return total or None


def parse_roman_sections(text: str, *, work: str, number: bool) -> list[tuple[str, str]]:
    """Split a book whose poems are headed by a Roman numeral."""
    lines = _strip_gutenberg(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    current_value = 0
    last_value = 0
    for raw in lines:
        stripped = raw.strip()
        value = _roman_to_int(stripped) if ROMAN_RE.match(stripped) else None
        # Poetry books number their sections upward; anything that breaks the
        # sequence is prose (a stray pronoun "I", a page number in Roman).
        if value is not None and value > last_value:
            if current is not None:
                sections.append((current_value, current))
            current = []
            current_value = value
            last_value = value
            continue
        if current is not None:
            current.append(raw)
    if current is not None:
        sections.append((current_value, current))

    out: list[tuple[str, str]] = []
    for value, body in sections:
        cleaned = [line.strip() for line in body if line.strip()]
        if len(cleaned) < 3:
            continue
        # Books carry a year or a dedication between heading and first stanza.
        while cleaned and (cleaned[0].isdigit() or len(cleaned[0]) <= 2):
            cleaned.pop(0)
        if len(cleaned) < 3:
            continue
        title = f"{work} {value}" if number else work
        out.append((title, "\n".join(cleaned)))
    return out


def parse_titled_sections(text: str, *, work: str) -> list[tuple[str, str]]:
    """Split a book whose poems are headed by a short ALL-CAPS title line."""
    lines = _strip_gutenberg(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[tuple[str, str]] = []
    title = ""
    body: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        looks_like_title = (
            bool(stripped)
            and stripped.isupper()
            and 2 <= len(stripped.split()) <= 8
            and any(char.isalpha() for char in stripped)
            and not stripped.startswith("*")
            and not stripped.endswith((".", ",", "-"))
        )
        if looks_like_title:
            cleaned = [line for line in body if line.strip()]
            if title and len(cleaned) >= 3:
                out.append((f"{work} — {title.title()}", "\n".join(line.strip() for line in cleaned)))
            title = stripped.title()
            body = []
            continue
        if title:
            body.append(raw)
    cleaned = [line for line in body if line.strip()]
    if title and len(cleaned) >= 3:
        out.append((f"{work} — {title.title()}", "\n".join(line.strip() for line in cleaned)))
    return out


def build_english(limit: int) -> list[dict]:
    poems: list[dict] = []
    for book in GUTENBERG_BOOKS:
        if len(poems) >= limit:
            break
        repo = str(book["repo"])
        name = repo.split("/")[-1]
        try:
            raw = _cached(f"{API}/repos/{repo}/contents/{book['file']}", f"gut-{name}.json")
            payload = json.loads(raw)
            if isinstance(payload, list):  # a directory, be forgiving
                payload = payload[0]
            text = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - a dead book must not kill the build
            print(f"  ! {name}: {exc}", file=sys.stderr)
            continue
        if book["mode"] == "roman":
            found = parse_roman_sections(text, work=str(book["work"]), number=bool(book["number"]))
        else:
            found = parse_titled_sections(text, work=str(book["work"]))
        taken = 0
        for title, body in found:
            if taken >= int(book["limit"]) or len(poems) >= limit:
                break
            if not MIN_POEM_CHARS <= len(body) <= MAX_POEM_CHARS:
                continue
            if "PROJECT GUTENBERG" in body.upper():
                continue
            poems.append({"title": title, "poet": book["poet"], "language": "en", "text": body})
            taken += 1
        print(f"  {book['poet']}: +{taken} ({len(poems)} total)", file=sys.stderr)
    return poems


# ---------------------------------------------------------------------------
def write_corpus(poems: list[dict]) -> Path:
    poems = sorted(poems, key=lambda item: (item["language"], item["poet"], item["title"]))
    payload = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [
            "ganjoor/ganjoor-db (ganjoor.net classical Persian canon, public domain)",
            "Project Gutenberg via GITenberg (public domain)",
        ],
        "poems": poems,
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(TARGET, "wb", compresslevel=9) as handle:
        handle.write(raw)
    return TARGET


def check() -> int:
    if not TARGET.exists():
        print(f"missing {TARGET}", file=sys.stderr)
        return 1
    with gzip.open(TARGET, "rb") as handle:
        payload = json.loads(handle.read())
    poems = payload["poems"]
    by_lang: dict[str, int] = {}
    by_poet: dict[str, int] = {}
    for poem in poems:
        by_lang[poem["language"]] = by_lang.get(poem["language"], 0) + 1
        by_poet[poem["poet"]] = by_poet.get(poem["poet"], 0) + 1
    print(f"{len(poems)} poems, {len(by_poet)} poets, {by_lang}, {TARGET.stat().st_size // 1024} KB")
    for poet, count in sorted(by_poet.items(), key=lambda pair: -pair[1]):
        print(f"  {poet}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persian", type=int, default=3200, help="max Persian poems")
    parser.add_argument("--english", type=int, default=500, help="max English poems")
    parser.add_argument("--persian-only", action="store_true")
    parser.add_argument("--english-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check()

    poems: list[dict] = []
    if not args.english_only:
        print("Persian — Ganjoor", file=sys.stderr)
        poems.extend(build_persian(args.persian))
    if not args.persian_only:
        print("English — Project Gutenberg", file=sys.stderr)
        poems.extend(build_english(args.english))
    if not poems:
        print("no poems collected", file=sys.stderr)
        return 1
    target = write_corpus(poems)
    print(f"wrote {len(poems)} poems to {target} ({target.stat().st_size // 1024} KB)", file=sys.stderr)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
