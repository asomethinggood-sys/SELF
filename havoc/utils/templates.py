"""Safe template rendering for message templates and scheduled messages.

Only a fixed set of placeholders is allowed. Unknown ``{name}`` tokens raise a
validation error at *creation* time, so a scheduled message can never fail
silently hours later. Literal braces are written as ``{{`` / ``}}``.

Feature areas: 24, 60 (validation), 95.
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_TEMPLATE_LENGTH = 3000

ALLOWED_PLACEHOLDERS: dict[str, str] = {
    "name": "recipient/chat display name",
    "chat": "chat title",
    "me": "your first name",
    "date": "today's date (YYYY-MM-DD)",
    "time": "current time (HH:MM)",
    "weekday": "weekday name",
    "count": "custom counter value",
    "id": "chat id",
}


class TemplateError(ValueError):
    def __init__(self, message: str, code: str = "errors.template_invalid") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class TemplateValidation:
    ok: bool
    placeholders: tuple[str, ...]
    unknown: tuple[str, ...]


def _fields(template: str) -> list[str]:
    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(template))
    except ValueError as exc:
        raise TemplateError(f"malformed braces: {exc}", "errors.template_malformed") from exc
    names: list[str] = []
    for _literal, field, _spec, _conv in parsed:
        if field is None:
            continue
        root = re.split(r"[.\[]", field, maxsplit=1)[0]
        if not root:
            raise TemplateError("positional placeholders are not allowed", "errors.template_positional")
        names.append(root)
    return names


def validate_template(template: str) -> TemplateValidation:
    if not template or not template.strip():
        raise TemplateError("template is empty", "errors.template_empty")
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise TemplateError(f"template is longer than {MAX_TEMPLATE_LENGTH} characters", "errors.template_too_long")
    names = _fields(template)
    unknown = tuple(name for name in names if name not in ALLOWED_PLACEHOLDERS)
    return TemplateValidation(ok=not unknown, placeholders=tuple(dict.fromkeys(names)), unknown=unknown)


def render_template(template: str, values: Mapping[str, Any]) -> str:
    """Render ``template``; unknown placeholders are a hard error."""
    validation = validate_template(template)
    if validation.unknown:
        raise TemplateError(
            "unknown placeholder(s): " + ", ".join(validation.unknown), "template.unknown_placeholder"
        )
    missing = [name for name in validation.placeholders if name not in values]
    if missing:
        # Allowed placeholders always get a value from the caller, but guard
        # anyway so a programming mistake is loud rather than silent.
        raise TemplateError("missing value(s): " + ", ".join(missing), "errors.template_missing_value")
    try:
        return template.format(**{name: values[name] for name in validation.placeholders})
    except (KeyError, IndexError, ValueError) as exc:  # pragma: no cover - guarded above
        raise TemplateError(f"rendering failed: {exc}", "errors.template_render_failed") from exc


def template_values(**overrides: Any) -> dict[str, Any]:
    """Default values so a template can be rendered outside a chat context."""
    from datetime import datetime

    now = datetime.now()
    base = {
        "name": "",
        "chat": "",
        "me": "",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "count": 0,
        "id": "",
    }
    base.update({key: value for key, value in overrides.items() if key in ALLOWED_PLACEHOLDERS})
    return base
