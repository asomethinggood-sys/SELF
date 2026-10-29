"""Automatic language detection for English, Farsi and mixed text.

Design notes
------------
* No network or AI dependency: script statistics plus a handful of
  high-signal function words.
* Every message containing *one* Persian character must not be classified as
  Farsi. A confidence score and a minimum amount of usable letters are
  required; otherwise the estimate is ``unknown`` and callers use the
  configured fallback.
* Persian and Arabic share a code block, so Arabic-script text is labelled
  ``fa`` only when Persian-specific letters, Persian digits, or Persian
  function words are present; otherwise it is reported as ``ar`` (still
  RTL) so we do not mislead the user.

Feature areas: 3, 37 (message language detection), plus inputs for 28/73.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .utils.text import (
    PERSIAN_LETTERS,
    script_counts,
    strip_code,
    strip_emojis,
    strip_mentions,
    strip_urls,
)

#: Minimum number of script letters before we trust a classification.
MIN_LETTERS = 3
#: Below this dominance ratio we consider text mixed.
MIXED_LOW = 0.20
#: Ratio at/above which text is considered confidently single-script.
DOMINANT = 0.82

PERSIAN_SPECIFIC = set("پچژگکیۀ")
PERSIAN_HINTS = {
    "و", "از", "به", "که", "این", "را", "با", "برای", "است", "می", "نمی", "تو", "من",
    "شما", "او", "آن", "هم", "شد", "کرد", "چه", "چرا", "کجا", "سلام", "ممنون", "خوب",
}
LATIN_STOPWORDS = {
    "the", "and", "you", "for", "are", "with", "this", "that", "have", "not", "but",
    "from", "what", "your", "can", "will", "how", "why", "hello", "thanks", "please",
}


@dataclass(frozen=True)
class LanguageEstimate:
    """Result of language detection.

    Attributes
    ----------
    language:
        ``en``, ``fa``, ``ar``, ``mixed``, or ``unknown``.
    primary:
        Best-guess single language (``en``/``fa``), used for fallback logic.
    confidence:
        0.0-1.0. Values under :data:`CONFIDENCE_FLOOR` should be treated as
        uncertain by callers.
    persian_ratio:
        Share of Persian-script letters among Persian + Latin letters.
    mixed:
        True when both scripts are meaningfully present.
    """

    language: str
    primary: str
    confidence: float
    persian_ratio: float
    persian_letters: int
    latin_letters: int
    mixed: bool = False
    reason: str = ""

    @property
    def is_confident(self) -> bool:
        return self.confidence >= CONFIDENCE_FLOOR and self.language != "unknown"

    def resolve(self, fallback: str = "en") -> str:
        """Return a usable language code, applying ``fallback`` when unsure."""
        if self.mixed:
            return self.primary or fallback
        if self.language in ("en", "fa"):
            return self.language
        return fallback


#: Estimates below this confidence are not trusted for UI decisions.
CONFIDENCE_FLOOR = 0.55


def _sanitize(text: str) -> str:
    text = strip_code(text)
    text = strip_urls(text)
    text = strip_mentions(text)
    text = strip_emojis(text)
    # keep letters and digits; drop punctuation noise
    return text


def _function_word_score(text: str) -> tuple[float, float]:
    tokens = [t for t in re.split(r"[^\w\u0600-\u06FF']+", text.lower()) if t]
    if not tokens:
        return 0.0, 0.0
    fa = sum(1 for t in tokens if t in PERSIAN_HINTS) / len(tokens)
    en = sum(1 for t in tokens if t in LATIN_STOPWORDS) / len(tokens)
    return fa, en


def detect_language(text: str | None) -> LanguageEstimate:
    """Classify a message as English, Farsi, Arabic-script, mixed, or unknown."""
    if not text or not text.strip():
        return LanguageEstimate("unknown", "en", 0.0, 0.0, 0, 0, reason="empty")

    cleaned = _sanitize(text)
    fa_letters, lat_letters = script_counts(cleaned)
    total = fa_letters + lat_letters

    if total == 0:
        # Only emojis, digits, punctuation or URLs: nothing to classify.
        return LanguageEstimate("unknown", "en", 0.0, 0.0, 0, 0, reason="no letters")

    ratio = fa_letters / total

    if total < MIN_LETTERS:
        # Very short messages ("ok", "بله") are guessable but not reliable.
        guess = "fa" if ratio >= 0.8 else "en" if ratio <= 0.2 else "mixed"
        primary = "fa" if ratio >= 0.5 else "en"
        return LanguageEstimate(
            guess if guess != "mixed" else "mixed",
            primary,
            confidence=0.35,
            persian_ratio=ratio,
            persian_letters=fa_letters,
            latin_letters=lat_letters,
            mixed=guess == "mixed",
            reason="too short to classify reliably",
        )

    fa_hint, en_hint = _function_word_score(cleaned)
    mixed = MIXED_LOW <= ratio <= (1 - MIXED_LOW) and fa_letters >= 2 and lat_letters >= 2

    # Dominant script
    if ratio >= DOMINANT:
        language = "fa"
        confidence = min(0.99, 0.62 + 0.3 * ratio + 0.2 * fa_hint)
        primary = "fa"
    elif ratio <= (1 - DOMINANT):
        language = "en"
        confidence = min(0.99, 0.62 + 0.3 * (1 - ratio) + 0.2 * en_hint)
        primary = "en"
    else:
        language = "mixed"
        primary = "fa" if ratio >= 0.5 else "en"
        confidence = 0.6 + 0.2 * abs(ratio - 0.5) * 2
        mixed = True

    # Persian vs. other Arabic-script languages
    if language in {"fa", "mixed"}:
        persian_specific = any(ch in PERSIAN_SPECIFIC for ch in cleaned)
        digits_fa = bool(re.search(r"[\u06F0-\u06F9]", cleaned))
        if not persian_specific and not digits_fa and fa_hint == 0.0:
            language = "ar" if language == "fa" else "mixed"
            confidence *= 0.8
            primary = "fa"

    # Latin stopword evidence can promote a low-ratio mixed message to English
    if language == "mixed" and ratio < 0.5 and en_hint >= 0.2:
        primary = "en"
    if language == "mixed" and ratio >= 0.5 and fa_hint >= 0.2:
        primary = "fa"

    return LanguageEstimate(
        language=language,
        primary=primary,
        confidence=round(confidence, 3),
        persian_ratio=round(ratio, 3),
        persian_letters=fa_letters,
        latin_letters=lat_letters,
        mixed=mixed,
        reason="script statistics" if not mixed else "mixed scripts",
    )


def resolve_language(
    text: str | None,
    *,
    preference: str = "auto",
    fallback: str = "en",
) -> str:
    """Resolve the language to answer in.

    ``preference`` may be ``en``, ``fa`` (forced) or ``auto`` (detect).
    """
    if preference in ("en", "fa"):
        return preference
    estimate = detect_language(text)
    if estimate.mixed:
        return estimate.primary or fallback
    return estimate.resolve(fallback)


#: Recognized script letters for validation in tests/other modules.
PERSIAN_LETTER_PATTERN = re.compile(f"[{PERSIAN_LETTERS}]")
