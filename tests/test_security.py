"""Security helpers: redaction, path safety, HTML escaping, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from havoc.utils import security
from havoc.utils.formatting import code, escape
from havoc.utils.safe_math import CalcError, evaluate
from havoc.utils.security import REDACTED
from havoc.utils.validation import (
    ValidationError,
    validate_chat_id,
    validate_emoji,
    validate_message_body,
    validate_profile_bio,
    validate_regex,
    validate_timezone,
)


def test_redact_text_hides_secrets() -> None:
    raw = "token 123456:abcdefghijklmnop api_hash=0123456789abcdef0123456789abcdef phone +491234567"
    clean = security.redact_text(raw)
    assert "abcdefghijklmnop" not in clean
    assert "0123456789abcdef0123456789abcdef" not in clean
    assert REDACTED in clean


def test_safe_filename_strips_traversal() -> None:
    name = security.safe_filename("../../etc/passwd")
    assert "/" not in name and ".." not in name
    assert name.endswith("passwd")


def test_ensure_within_blocks_escape(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(ValueError):
        security.ensure_within(base, tmp_path / "outside.txt")
    inside = security.ensure_within(base, base / "ok.txt")
    assert inside.parent == base


def test_escape_neutralises_html() -> None:
    assert escape("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert code("<b>") == "<code>&lt;b&gt;</code>"


def test_calculator_evaluates_safely() -> None:
    assert evaluate("2+2*3") == 8
    assert evaluate("2**10") == 1024
    assert evaluate("sqrt(16)") == 4
    assert evaluate("max(1, 5, 3)") == 5
    with pytest.raises(CalcError):
        evaluate("__import__('os').system('ls')")
    with pytest.raises(CalcError):
        evaluate("1/0")
    with pytest.raises(CalcError):
        evaluate("x + 1")


def test_regex_validation_rejects_catastrophic_patterns() -> None:
    validate_regex(r"^hello$")
    with pytest.raises(ValidationError):
        validate_regex("(")
    with pytest.raises(ValidationError):
        validate_regex("(a+)+$")


def test_chat_id_validation() -> None:
    assert validate_chat_id("100") == 100
    assert validate_chat_id("-1001234567890") == -1001234567890
    with pytest.raises(ValidationError):
        validate_chat_id("not-a-number")


def test_timezone_validation() -> None:
    assert validate_timezone("Asia/Tehran") == "Asia/Tehran"
    with pytest.raises(ValidationError):
        validate_timezone("Mars/Olympus")


def test_emoji_validation() -> None:
    assert validate_emoji("👍") == "👍"
    with pytest.raises(ValidationError):
        validate_emoji("not-an-emoji")


def test_message_body_limits() -> None:
    validate_message_body("hello")
    with pytest.raises(ValidationError):
        validate_message_body("")


def test_profile_bio_truncation_and_limit() -> None:
    assert validate_profile_bio("  spaced  ") == "spaced"
    with pytest.raises(ValidationError):
        validate_profile_bio("x" * 200)
