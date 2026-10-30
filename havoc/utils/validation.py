"""Input validation for user-supplied command arguments.

Every validator raises :class:`ValidationError` with a stable ``code`` so the
command layer can localize the message (see ``locales/*.json`` under
``errors.*``) instead of showing English internals in a Farsi interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
PROFILE_NAME_LIMIT = 64
PROFILE_BIO_LIMIT = 70
TEMPLATE_NAME_LIMIT = 32
TEMPLATE_BODY_LIMIT = 3000
NOTE_BODY_LIMIT = 4000
COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ValidationError(ValueError):
    def __init__(self, code: str, **params: Any) -> None:
        self.code = code
        self.params = params
        super().__init__(f"{code}: {params}" if params else code)






def validate_template_name(value: str) -> str:
    name = (value or "").strip().lower().replace(" ", "_")
    if not re.fullmatch(rf"[a-z0-9_\-\u0600-\u06FF]{{1,{TEMPLATE_NAME_LIMIT}}}", name):
        raise ValidationError("errors.invalid_name", value=value)
    return name


def validate_message_body(value: str, *, limit: int = TEMPLATE_BODY_LIMIT) -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationError("errors.empty_value", field="message")
    if len(text) > limit:
        raise ValidationError("errors.too_long", field="message", limit=limit)
    return text


def validate_profile_name(value: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ValidationError("errors.empty_value", field="name")
    if len(name) > PROFILE_NAME_LIMIT:
        raise ValidationError("errors.too_long", field="name", limit=PROFILE_NAME_LIMIT)
    return name


def validate_profile_bio(value: str) -> str:
    bio = (value or "").strip()
    if len(bio) > PROFILE_BIO_LIMIT:
        raise ValidationError("errors.too_long", field="bio", limit=PROFILE_BIO_LIMIT)
    return bio


def validate_int(value: Any, *, minimum: int | None = None, maximum: int | None = None, field: str = "value") -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError("errors.not_a_number", field=field) from exc
    if minimum is not None and number < minimum:
        raise ValidationError("errors.too_small", field=field, minimum=minimum)
    if maximum is not None and number > maximum:
        raise ValidationError("errors.too_large", field=field, maximum=maximum)
    return number






def validate_chat_id(value: Any) -> int:
    try:
        chat_id = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError("errors.invalid_chat_id", value=value) from exc
    if chat_id == 0:
        raise ValidationError("errors.invalid_chat_id", value=value)
    return chat_id


@dataclass(frozen=True)
class TimeRange:
    start_hhmm: str
    end_hhmm: str




#: ``(a+)+`` and friends backtrack catastrophically on ordinary input.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+][^()]*\)\s*[*+{]")


def validate_regex(pattern: str) -> str:
    """Compile-check a pattern and refuse known catastrophic shapes.

    HAVOC runs regexes against every incoming message, so a friendly pattern
    like ``(a+)+$`` would be a self-inflicted denial of service.
    """
    if len(pattern) > 300:
        raise ValidationError("errors.too_long", field="pattern", limit=300)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValidationError("errors.invalid_regex", detail=str(exc)) from exc
    if _NESTED_QUANTIFIER.search(pattern):
        raise ValidationError("errors.invalid_regex", detail="nested quantifiers are too slow")
    return pattern


def validate_emoji(value: str) -> str:
    """Accept 1-4 grapheme-ish characters; Telegram rejects arbitrary text."""
    text = (value or "").strip()
    if not text:
        raise ValidationError("errors.empty_value", field="emoji")
    if len(text) > 8:
        raise ValidationError("errors.too_long", field="emoji", limit=8)
    if re.fullmatch(r"[A-Za-z0-9]+", text):
        raise ValidationError("errors.invalid_emoji", value=text)
    return text


def validate_timezone(value: str) -> str:
    """Accept only IANA timezone names that ``zoneinfo`` can resolve."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = (value or "").strip()
    if not name:
        raise ValidationError("errors.missing_argument", field="timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("errors.invalid_timezone", value=name) from exc
    return name






def validate_url(value: str) -> str:
    text = (value or "").strip()
    if not re.match(r"^https?://[^\s]+$", text, re.IGNORECASE):
        raise ValidationError("errors.invalid_url", value=text)
    return text
