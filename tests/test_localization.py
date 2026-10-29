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
    assert "Yura" in locales.t("about.title", "en", version="2.0.0")
    farsi = locales.t("about.title", "fa", version="2.0.0")
    assert "یورا" in farsi
    # unknown language falls back to English rather than raising
    assert "Yura" in locales.t("about.title", "de", version="2.0.0")


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
