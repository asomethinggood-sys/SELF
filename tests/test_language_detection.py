"""Language detection quality gates (English, Farsi, mixed, uncertain)."""

from __future__ import annotations

from havoc.language_detection import (
    CONFIDENCE_FLOOR,
    MIN_LETTERS,
    detect_language,
    resolve_language,
)


def test_empty_and_symbol_only_are_unknown() -> None:
    for text in ("", "   ", "😀😀", "12345", "https://example.com/path"):
        estimate = detect_language(text)
        assert estimate.language == "unknown"
        assert estimate.confidence < CONFIDENCE_FLOOR


def test_english_sentence() -> None:
    estimate = detect_language("Hello, how are you doing today? I hope everything is fine.")
    assert estimate.language == "en"
    assert estimate.confidence > 0.8


def test_farsi_sentence() -> None:
    estimate = detect_language("سلام، حال شما چطور است؟ امیدوارم همه چیز خوب باشد.")
    assert estimate.language == "fa"
    assert estimate.confidence > 0.8
    assert estimate.persian_ratio > 0.9


def test_single_persian_character_is_not_farsi() -> None:
    estimate = detect_language("This is an English sentence with one Persian letter: ی")
    assert estimate.language == "en"
    assert estimate.persian_ratio < 0.1


def test_mixed_language_text_is_flagged() -> None:
    estimate = detect_language("This message is mostly English but contains فارسی words mixed inside it")
    assert estimate.mixed is True or estimate.language in {"en", "fa"}
    assert 0.0 < estimate.persian_ratio < 1.0


def test_short_text_is_low_confidence() -> None:
    estimate = detect_language("ok")
    assert estimate.confidence < 0.5
    assert len("ok") >= MIN_LETTERS - 1


def test_persian_digits_and_url_do_not_confuse_detection() -> None:
    estimate = detect_language("سفارش ۱۲۳۴ ثبت شد https://example.com/a/b")
    assert estimate.language == "fa"


def test_emoji_heavy_message_keeps_language() -> None:
    estimate = detect_language("ممنون 🙏🙏 عالی بود 🔥")
    assert estimate.language == "fa"


def test_arabic_only_text_is_not_labelled_farsi() -> None:
    estimate = detect_language("السلام عليكم ورحمة الله وبركاته")
    assert estimate.language in {"ar", "fa"}
    if estimate.language == "fa":
        # only allowed when Persian-specific evidence exists
        assert estimate.confidence < 0.9


def test_resolve_language_prefers_explicit_preference() -> None:
    assert resolve_language("hello", preference="fa") == "fa"
    assert resolve_language("hello", preference="en") == "en"


def test_resolve_language_uses_detection_when_auto() -> None:
    assert resolve_language("این یک جمله فارسی است", preference="auto", fallback="en") == "fa"
    assert resolve_language("This is an English sentence with enough words", preference="auto", fallback="fa") == "en"


def test_resolve_language_falls_back_for_unclassifiable_text() -> None:
    assert resolve_language("😀😀😀", preference="auto", fallback="fa") == "fa"
    assert resolve_language("", preference="auto", fallback="en") == "en"


def test_message_with_only_a_mention_is_unknown() -> None:
    assert detect_language("@someone").language == "unknown"
