"""Text utilities with first-class Persian (Farsi) support.

The functions here are pure and dependency-free so they can be unit-tested
without Telegram. They are careful never to reverse or corrupt RTL text:
Unicode's bidi algorithm handles ordering, we only supply isolation where a
renderer might otherwise mis-order mixed runs.

Feature areas covered: 63 (Persian text utilities), 64 (text cleanup),
65 (text statistics), 66 (text transformation), plus helpers used by the
language detector and the localization layer.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Unicode ranges
# ---------------------------------------------------------------------------
PERSIAN_LETTERS = (
    "\u0621-\u063A"  # Arabic letters (shared with Persian)
    "\u0641-\u064A"  # Arabic letters (shared with Persian)
    "\u067E\u0686\u0698\u06A9\u06AF\u06CC"  # Persian-specific letters
    "\u06BE\u06C1"  # Heh variants used in Urdu/Persian text
)
PERSIAN_RANGE = "\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF"
ARABIC_INDIC_DIGITS = "\u0660-\u0669"
PERSIAN_DIGITS = "\u06F0-\u06F9"
BIDI_MARKS = "\u200E\u200F\u061C\u202A-\u202E\u2066-\u2069"

_RE_PERSIAN_CHAR = re.compile(f"[{PERSIAN_RANGE}]")
_RE_PERSIAN_LETTER = re.compile(f"[{PERSIAN_LETTERS}]")
_RE_LATIN_LETTER = re.compile(r"[A-Za-z\u00C0-\u024F]")
_RE_DIGITS = re.compile(r"[0-9\u06F0-\u06F9\u0660-\u0669]")
_RE_URL = re.compile(r"(?:https?://|www\.|t\.me/|tg://)\S+", re.IGNORECASE)
_RE_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,}")
_RE_HASHTAG = re.compile(r"(?<!\w)#[^\s#,.!?]+")
_RE_CODE_SPAN = re.compile(r"`[^`]*`|```.*?```", re.DOTALL)
_RE_INVISIBLE = re.compile(f"[{BIDI_MARKS}\u200B\uFEFF\u00AD]")
_RE_MULTISPACE = re.compile(r"[ \t\u00A0]{2,}")
_RE_MULTINEWLINE = re.compile(r"\n{3,}")
_RE_REPEATED_PUNCT = re.compile(r"([!?.,،؛…])\1{1,}")
_RE_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "\uFE0F\u2764" "]"
)

_DIGIT_MAP_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_DIGIT_MAP_AR = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_FROM_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Letters that Persian and Arabic share but that are usually mistyped.
_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ٱ": "ا", "ۀ": "ه"})


def strip_urls(text: str) -> str:
    return _RE_URL.sub(" ", text)


def strip_mentions(text: str) -> str:
    return _RE_MENTION.sub(" ", text)


def strip_emojis(text: str) -> str:
    return _RE_EMOJI.sub("", text)


def strip_code(text: str) -> str:
    return _RE_CODE_SPAN.sub(" ", text)


def strip_invisible(text: str) -> str:
    """Remove zero-width and bidi control characters (used for normalization)."""
    return _RE_INVISIBLE.sub("", text)








def script_counts(text: str) -> tuple[int, int]:
    """Count Persian-script and Latin letters. Digits/emoji/punct ignored."""
    fa = len(_RE_PERSIAN_LETTER.findall(text))
    lat = len(_RE_LATIN_LETTER.findall(text))
    return fa, lat


def is_rtl(text: str, threshold: float = 0.6) -> bool:
    """True when the majority of letters are RTL."""
    fa, lat = script_counts(text)
    total = fa + lat
    if total == 0:
        return False
    return fa / total >= threshold


def to_persian_digits(text: str) -> str:
    return text.translate(_DIGIT_MAP_FA)


def to_arabic_digits(text: str) -> str:
    return text.translate(_DIGIT_MAP_AR)


def to_latin_digits(text: str) -> str:
    """Convert Persian/Arabic-Indic digits to ASCII digits (idempotent)."""
    return text.translate(_FROM_FA)


def normalize_persian(text: str) -> str:
    """Normalize Arabic letter variants to their Persian counterparts.

    It does **not** touch digits or punctuation, so meaning is preserved.
    """
    return to_latin_digits(text).translate(_ARABIC_TO_PERSIAN).replace("\u064A", "ی")


def localize_digits(text: str, lang: str) -> str:
    """Render digits in the script of ``lang`` (fa -> Persian digits)."""
    if lang == "fa":
        return to_persian_digits(text)
    return text




def mixed_direction_safe(text: str) -> str:
    """Make mixed Persian/English text render in the intended order.

    Strategy: leave the text untouched (clients apply bidi) but replace
    invisible control characters that commonly corrupt display, and place
    LTR runs (URLs, @mentions, code) inside LRI isolates.
    """
    text = strip_invisible(text)

    def _isolate(match: re.Match[str]) -> str:
        return f"\u2066{match.group(0)}\u2069"

    text = re.sub(r"(https?://\S+|www\.\S+|\b[\w.+-]+@[\w-]+\.[\w.]+\b)", _isolate, text)
    return text


def clean_whitespace(text: str) -> str:
    """Collapse spacing noise while keeping deliberate indentation.

    Runs of spaces become one space and 3+ newlines become one blank line.
    A line is right-stripped; its leading whitespace is removed unless it
    looks like intentional indentation (4+ spaces or a tab), so pasted code
    blocks survive.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RE_MULTISPACE.sub(" ", text)
    text = _RE_MULTINEWLINE.sub("\n\n", text)
    cleaned: list[str] = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        keeps_indent = stripped.startswith("    ") or stripped.startswith("\t")
        cleaned.append(stripped if keeps_indent else stripped.lstrip())
    return "\n".join(cleaned).strip()


def clean_repeated_punctuation(text: str, max_repeat: int = 1) -> str:
    """Collapse runs like ``!!!!`` / ``!!!!!!`` respecting ``max_repeat``."""

    def _collapse(match: re.Match[str]) -> str:
        return match.group(1) * max_repeat

    return _RE_REPEATED_PUNCT.sub(_collapse, text)


def cleanup_text(text: str, *, punctuation: bool = True, normalize_letters: bool = True) -> str:
    out = clean_whitespace(text)
    if normalize_letters:
        out = normalize_persian(out)
    if punctuation:
        out = clean_repeated_punctuation(out)
    return out


def truncate(text: str, limit: int = 120, ellipsis: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))].rstrip() + ellipsis


def snippet(text: str, limit: int = 120) -> str:
    """Single-line, whitespace-collapsed snippet safe for logs/diagnostics."""
    return truncate(" ".join(text.split()), limit)


@dataclass(frozen=True)
class TextStats:
    characters: int
    characters_no_spaces: int
    words: int
    lines: int
    sentences: int
    persian_words: int
    latin_words: int
    emojis: int
    urls: int


def text_stats(text: str) -> TextStats:
    """Count words/sentences/lines handling Persian punctuation and ZWNJ.

    ZWNJ (U+200C) is a word-internal marker in Persian (e.g. ``میروم``);
    treating it as a separator would double-count words, so it is kept.
    """
    characters = len(text)
    no_spaces = len(re.sub(r"\s", "", text))
    words_list = [w for w in re.split(r"[\s\u200B\uFEFF]+", text) if w.strip()]
    persian_words = sum(1 for w in words_list if _RE_PERSIAN_LETTER.search(w))
    latin_words = sum(1 for w in words_list if _RE_LATIN_LETTER.search(w))
    sentences = len([s for s in re.split(r"[.!?؟…\n]+", text) if s.strip()])
    return TextStats(
        characters=characters,
        characters_no_spaces=no_spaces,
        words=len(words_list),
        lines=len(text.splitlines()) or (1 if text else 0),
        sentences=sentences,
        persian_words=persian_words,
        latin_words=latin_words,
        emojis=len(_RE_EMOJI.findall(text)),
        urls=len(_RE_URL.findall(text)),
    )


def reverse_text(text: str) -> str:
    """Reverse a string without corrupting combining marks or emoji sequences.

    Grapheme clusters are approximated by keeping combining characters with
    their base character and leaving ZWJ emoji sequences intact.
    """
    clusters: list[str] = []
    for ch in text:
        if clusters and (unicodedata.combining(ch) or ch in "\u200D\uFE0F" or clusters[-1].endswith("\u200D")):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return "".join(reversed(clusters))


def sort_lines(text: str, reverse: bool = False) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(sorted(lines, reverse=reverse))


def dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        key = line.strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def slugify(text: str, limit: int = 60) -> str:
    """ASCII-ish slug used for filenames. Persian is transliterated to a hash."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()
    if not slug:
        import hashlib

        slug = "fa-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return slug[:limit]


def word_wrap(text: str, width: int = 60) -> str:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width and current:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split long text into Telegram-sized chunks on natural boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks






def extract_urls(text: str) -> list[str]:
    return _RE_URL.findall(text)


def extract_hashtags(text: str) -> list[str]:
    return [tag[1:] for tag in _RE_HASHTAG.findall(text)]


def extract_mentions(text: str) -> list[str]:
    return [m[1:] for m in _RE_MENTION.findall(text)]




