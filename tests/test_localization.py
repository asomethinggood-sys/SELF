"""Localization: parity, interpolation, fallbacks, Persian digits."""

from __future__ import annotations

import pytest

from havoc.localization import (
    DEFAULT_LOCALE_DIR,
    LocaleFormatError,
    LocaleManager,
    MissingTranslationError,
    digits_for,
    normalize_language_code,
)
from havoc.version import VERSION


@pytest.fixture(scope="module")
def locales() -> LocaleManager:
    return LocaleManager(DEFAULT_LOCALE_DIR).load()


def test_english_and_farsi_catalogs_exist(locales: LocaleManager) -> None:
    assert set(locales.languages) >= {"en", "fa"}
    assert len(locales.keys("en")) > 300
    assert len(locales.keys("fa")) > 300


def test_catalog_key_parity(locales: LocaleManager) -> None:
    assert locales.missing_keys("fa") == set()
    assert locales.extra_keys("fa") == set()


def test_validate_reports_no_problems(locales: LocaleManager) -> None:
    assert locales.validate() == []


def test_translation_and_fallback(locales: LocaleManager) -> None:
    assert "Y.U.R.A" in locales.t("about.title", "en", version=VERSION, stage="Open beta")
    farsi = locales.t("about.title", "fa", version=VERSION, stage="Open beta")
    assert "Y.U.R.A" in farsi and VERSION in farsi
    # unknown language falls back to English rather than raising
    assert "Y.U.R.A" in locales.t("about.title", "de", version=VERSION, stage="Open beta")


def test_missing_key_raises_or_defaults(locales: LocaleManager) -> None:
    with pytest.raises(MissingTranslationError):
        locales.t("does.not.exist", "en")
    assert locales.t("does.not.exist", "en", default="fallback") == "fallback"


def test_missing_translation_falls_back_to_english() -> None:
    manager = LocaleManager.from_dict({"en": {"hello": "Hello {name}"}, "fa": {}})
    assert manager.t("hello", "fa", name="Ali") == "Hello Ali"


def test_interpolation_validates_variables(locales: LocaleManager) -> None:
    with pytest.raises(LocaleFormatError):
        locales.t("about.title", "en")  # needs {version}
    assert locales.interpolate("Hi {name}", key="x", name="Ali") == "Hi Ali"


def test_malformed_template_raises() -> None:
    manager = LocaleManager.from_dict({"en": {"bad": "hello {name"}})
    with pytest.raises(LocaleFormatError):
        manager.t("bad", "en", name="x")


def test_persian_digits_localization(locales: LocaleManager) -> None:
    manager = LocaleManager.from_dict({"en": {"n": "count {count}"}})
    assert manager.t("n", "en", count=5) == "count 5"
    assert manager.t("n", "fa", count=5) == "count 5"  # digits only when asked
    assert manager.t("n", "fa", count=5, localize_digits=True) == "count ۵"


def test_pluralization() -> None:
    manager = LocaleManager.from_dict(
        {"en": {"items.one": "{count} item", "items.other": "{count} items"}}
    )
    assert manager.plural("items", 1, "en") == "1 item"
    assert manager.plural("items", 3, "en") == "3 items"


def test_language_normalization() -> None:
    assert normalize_language_code("FA") == "fa"
    assert normalize_language_code("fa-IR") == "fa"
    assert normalize_language_code("فارسی") == "fa"
    assert normalize_language_code("english") == "en"
    assert normalize_language_code("auto") == "auto"
    assert normalize_language_code("klingon", "en") == "en"


def test_digits_for() -> None:
    assert digits_for("fa", 42) == "۴۲"
    assert digits_for("en", "۴۲") == "42"


def test_add_catalog_for_plugins(locales: LocaleManager) -> None:
    manager = LocaleManager.from_dict({"en": {"a": "A"}})
    manager.add_catalog("en", {"b": {"c": "BC"}})
    assert manager.t("b.c", "en") == "BC"


def test_persian_strings_contain_no_arabic_letter_variants(locales: LocaleManager) -> None:
    """Quality gate: Persian catalogs must not use Arabic ي/ك."""
    offenders = [key for key in locales.keys("fa") if "ي" in locales.raw(key, "fa") or "ك" in locales.raw(key, "fa")]
    assert offenders == []


def test_a_translation_that_drops_a_placeholder_is_reported() -> None:
    """The name is not a formatting error when it is simply gone."""
    manager = LocaleManager(DEFAULT_LOCALE_DIR)
    manager.add_catalog("fa", {"greeting": "سلام!"})
    manager.add_catalog("en", {"greeting": "hello {name}"})
    problems = manager.validate()
    assert any("greeting" in problem and "missing name" in problem for problem in problems)


def test_a_translation_with_an_extra_placeholder_is_reported() -> None:
    """Worse than missing: an extra one raises at render time, inside a handler."""
    manager = LocaleManager(DEFAULT_LOCALE_DIR)
    manager.add_catalog("fa", {"bye": "خداحافظ {name}"})
    manager.add_catalog("en", {"bye": "goodbye"})
    problems = manager.validate()
    assert any("bye" in problem and "unexpected name" in problem for problem in problems)


# ---------------------------------------------------------------------------
# A placeholder may be called "language", "key" or "variables"
# ---------------------------------------------------------------------------
# The startup banner, the reply-suggestion header and the AI self-test all say
# the word "language" in their text. Passing it unpacked collided with
# `interpolate`'s own arguments, so the bot printed a raw key — and printed it in
# the one message the owner reads at boot. Reported as a wording bug, it was
# actually a crash in the sugar.
def test_a_catalog_may_use_the_word_language(locales: LocaleManager) -> None:
    assert locales.interpolate("Language: {language}", language="en") == "Language: en"


def test_a_catalog_may_use_the_word_key(locales: LocaleManager) -> None:
    assert locales.interpolate("{key} failed", key="profile", language="en") == "profile failed"


def test_a_catalog_may_use_the_word_variables(locales: LocaleManager) -> None:
    assert locales.interpolate("{variables}", variables="plain") == "plain"


def test_the_startup_banner_is_a_sentence(locales: LocaleManager) -> None:
    """The exact call that was broken, in the exact shape the boot makes it."""
    text = locales.t("startup.language", "en", language=locales.language_name("en", "en"))
    assert text.startswith("Language:")
    assert "{" not in text
    assert text != "startup.language"


def test_missing_placeholder_still_fails_loudly(locales: LocaleManager) -> None:
    with pytest.raises(LocaleFormatError):
        locales.interpolate("Language: {language}")


def test_extra_placeholders_are_accepted(locales: LocaleManager) -> None:
    """Passing more than the template needs must not raise: call sites are shared."""
    assert locales.interpolate("{a}", a="1", b="2") == "1"


def test_a_catalog_may_say_the_word_language(locales: LocaleManager) -> None:
    """``{language}`` is a placeholder, not a reserved word.

    Passing ``language=...`` used to collide with the interpolator's own
    parameter, which is how the startup banner and the AI self-test ended up
    printing ``startup.language`` at the owner instead of "Language: English".
    """
    template = "Language: {language}"
    assert locales.interpolate(template, "probe", "en", language="Persian") == "Language: Persian"


def test_interpolation_reports_a_missing_placeholder(locales: LocaleManager) -> None:
    with pytest.raises(Exception):
        locales.interpolate("Hello {missing}", "probe", "en", {})
