"""Persian-aware text utilities."""

from __future__ import annotations

from havoc.utils.text import (
    clean_whitespace,
    cleanup_text,
    dedupe_lines,
    extract_hashtags,
    extract_urls,
    is_rtl,
    localize_digits,
    mixed_direction_safe,
    normalize_persian,
    reverse_text,
    script_counts,
    slugify,
    sort_lines,
    split_message,
    text_stats,
    to_arabic_digits,
    to_latin_digits,
    to_persian_digits,
    truncate,
    word_wrap,
)


def test_persian_and_latin_digit_conversion_roundtrip() -> None:
    assert to_persian_digits("123") == "۱۲۳"
    assert to_arabic_digits("123") == "١٢٣"
    assert to_latin_digits("۱۲۳") == "123"
    assert to_latin_digits("١٢٣") == "123"
    assert to_latin_digits("123") == "123"


def test_normalize_persian_fixes_arabic_variants() -> None:
    assert normalize_persian("كتاب علي") == "کتاب علی"
    assert normalize_persian("۱۲۳") == "123"


def test_script_counts_ignores_urls_digits_and_emoji() -> None:
    fa, latin = script_counts("سلام hello ۱۲۳ 😀")
    assert fa == 4
    assert latin == 5


def test_is_rtl() -> None:
    assert is_rtl("سلام دنیا") is True
    assert is_rtl("hello world") is False
    assert is_rtl("Hello سلام") is False  # majority latin


def test_whitespace_cleanup_keeps_meaning() -> None:
    assert clean_whitespace("  a   b \n\n\n\n c  ") == "a b\n\nc"
    assert clean_whitespace("سلام    دنیا") == "سلام دنیا"


def test_repeated_punctuation_collapse() -> None:
    assert cleanup_text("wow!!!! really???") == "wow! really?"


def test_text_stats_handles_zwnj_and_persian() -> None:
    stats = text_stats("می‌روم به خانه. Hello world!\nخط دوم")
    assert stats.persian_words == 5  # می‌روم، به، خانه، خط، دوم
    assert stats.latin_words == 2
    assert stats.sentences >= 2
    assert stats.lines == 2


def test_truncate_and_snippet() -> None:
    assert truncate("hello", 10) == "hello"
    assert truncate("hello world", 8).endswith("…")
    assert len(truncate("x" * 100, 10)) == 10


def test_reverse_text_preserves_combining_marks() -> None:
    text = "a\u0301b"  # á as combining sequence
    assert reverse_text(text) == "b" + "a\u0301"


def test_sort_and_dedupe_lines() -> None:
    assert sort_lines("b\na\nb") == "a\nb\nb"
    assert dedupe_lines("A\na\nb") == "A\nb"


def test_mixed_direction_safe_strips_controls_and_isolates_urls() -> None:
    messy = "سلام \u202e دنیا https://example.com/x"
    cleaned = mixed_direction_safe(messy)
    assert "\u202e" not in cleaned
    assert "https://example.com/x" in cleaned
    assert "\u2066" in cleaned


def test_extract_helpers() -> None:
    assert extract_urls("see https://a.b/c and www.x.y") == ["https://a.b/c", "www.x.y"]
    assert extract_hashtags("#سلام #test") == ["سلام", "test"]


def test_split_message_respects_limit() -> None:
    text = "\n\n".join(["word " * 200] * 5)
    chunks = split_message(text, limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") != ""


def test_word_wrap() -> None:
    wrapped = word_wrap("one two three four", width=7)
    assert all(len(line) <= 9 for line in wrapped)


def test_slugify_handles_persian() -> None:
    assert slugify("Hello World!") == "hello-world"
    assert slugify("سلام دنیا").startswith("fa-")


def test_localize_digits() -> None:
    assert localize_digits("42", "fa") == "۴۲"
    assert localize_digits("42", "en") == "42"
