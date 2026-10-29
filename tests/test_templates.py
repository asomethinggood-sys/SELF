"""Message templates: rendering, validation and safety."""

from __future__ import annotations

import pytest

from havoc.utils.templates import (
    ALLOWED_PLACEHOLDERS,
    MAX_TEMPLATE_LENGTH,
    TemplateError,
    render_template,
    template_values,
    validate_template,
)


def test_known_placeholders_validate() -> None:
    result = validate_template("Hi {name}, it is {weekday}")
    assert result.ok is True
    assert set(result.placeholders) == {"name", "weekday"}
    assert result.unknown == ()


def test_unknown_placeholder_is_flagged_at_creation() -> None:
    result = validate_template("Hi {nobody}")
    assert result.ok is False
    assert result.unknown == ("nobody",)


def test_render_substitutes_values() -> None:
    values = template_values(name="Ali", chat="Team")
    assert render_template("Hi {name} from {chat}", values) == "Hi Ali from Team"


def test_render_refuses_unknown_placeholder() -> None:
    with pytest.raises(TemplateError) as info:
        render_template("Hi {nobody}", template_values())
    assert info.value.code == "template.unknown_placeholder"


def test_literal_braces_are_allowed() -> None:
    assert render_template("{{literal}}", template_values()) == "{literal}"


def test_empty_and_oversized_bodies_are_rejected() -> None:
    for body in ("", "   "):
        with pytest.raises(TemplateError):
            validate_template(body)
    with pytest.raises(TemplateError):
        validate_template("x" * (MAX_TEMPLATE_LENGTH + 1))


def test_unbalanced_braces_are_rejected() -> None:
    with pytest.raises(TemplateError):
        validate_template("Hi {name")


def test_positional_placeholders_are_rejected() -> None:
    with pytest.raises(TemplateError):
        validate_template("Hi {}")


def test_defaults_cover_every_allowed_placeholder() -> None:
    values = template_values()
    assert set(values) == set(ALLOWED_PLACEHOLDERS)
    assert all(isinstance(value, (str, int)) for value in values.values())


def test_overrides_ignore_unknown_keys() -> None:
    values = template_values(name="Ali", nonsense="x")
    assert values["name"] == "Ali"
    assert "nonsense" not in values


def test_render_keeps_unicode() -> None:
    assert render_template("سلام {name}", template_values(name="علی")) == "سلام علی"


def test_localized_error_keys_exist() -> None:
    from havoc.localization import LocaleManager

    locales = LocaleManager().load()
    for code in ("template.invalid", "template.unknown_placeholder"):
        assert locales.has(code, "en")
        assert locales.has(code, "fa")
