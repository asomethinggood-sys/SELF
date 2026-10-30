"""Localization system for HAVOC (English + Farsi).

Public surface
--------------
``LocaleManager``
    Loads JSON catalogs, resolves dotted keys, interpolates variables,
    validates that every key exists in every language, and formats numbers/
    digits per language.

Rules enforced here
-------------------
* One implementation, many locales. Feature code never branches on language;
  it asks for a key.
* Missing translations fall back to ``en`` and are reported (never silently
  empty).
* Interpolation is validated: a missing variable raises
  :class:`LocaleFormatError` instead of producing a broken message.
* Persian output uses Persian digits only when the caller asks
  (``localize_digits=True``), so IDs and usernames stay copy-pasteable.

Feature areas: 4 (complete localization), 63 (Persian formatting), 93.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils.text import to_latin_digits, to_persian_digits

DEFAULT_LOCALE_DIR = Path(__file__).with_name("locales")
FALLBACK_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "fa")

LANGUAGE_NAMES = {
    "en": {"en": "English", "fa": "انگلیسی"},
    "fa": {"en": "Farsi", "fa": "فارسی"},
}


class LocalizationError(Exception):
    """Base class for localization failures."""


class MissingTranslationError(LocalizationError):
    def __init__(self, key: str, language: str) -> None:
        self.key = key
        self.language = language
        super().__init__(f"missing translation key {key!r} for language {language!r}")


class LocaleFormatError(LocalizationError):
    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"{key}: {message}")


def _flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            flat.update(_flatten(value, full))
        elif isinstance(value, list):
            flat[full] = "\n".join(str(v) for v in value)
        else:
            flat[full] = str(value)
    return flat


@dataclass
class LocaleCatalog:
    language: str
    strings: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.strings.get(key)


class LocaleManager:
    """Thread-safe-enough (read-only after load) localization registry."""

    def __init__(self, locale_dir: Path | str = DEFAULT_LOCALE_DIR) -> None:
        self.locale_dir = Path(locale_dir)
        self._catalogs: dict[str, LocaleCatalog] = {}
        self._missing: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self) -> LocaleManager:
        """Load every ``*.json`` catalog from the locale directory."""
        if not self.locale_dir.exists():
            raise LocalizationError(f"locale directory not found: {self.locale_dir}")
        for path in sorted(self.locale_dir.glob("*.json")):
            language = path.stem
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:  # pragma: no cover - guarded by tests
                raise LocalizationError(f"invalid JSON in {path.name}: {exc}") from exc
            if not isinstance(raw, Mapping):
                raise LocalizationError(f"{path.name} must contain a JSON object")
            self._catalogs[language] = LocaleCatalog(language, _flatten(raw))
        if FALLBACK_LANGUAGE not in self._catalogs:
            raise LocalizationError("the fallback language catalog 'en' is missing")
        return self

    @classmethod
    def from_dict(cls, catalogs: Mapping[str, Mapping[str, Any]]) -> LocaleManager:
        """Build a manager from in-memory catalogs (used by tests/plugins)."""
        manager = cls()
        for language, data in catalogs.items():
            manager._catalogs[language] = LocaleCatalog(language, _flatten(data))
        return manager

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalogs))

    def keys(self, language: str | None = None) -> set[str]:
        catalog = self._catalogs.get(language or FALLBACK_LANGUAGE)
        return set(catalog.strings) if catalog else set()

    def has(self, key: str, language: str = FALLBACK_LANGUAGE) -> bool:
        catalog = self._catalogs.get(language)
        return bool(catalog and key in catalog.strings)

    #: Feature names currently enabled; used by help rendering to mark commands.
    enabled_features: set[str] = set()

    def missing_keys(self, language: str) -> set[str]:
        return self.keys(FALLBACK_LANGUAGE) - self.keys(language)

    def extra_keys(self, language: str) -> set[str]:
        return self.keys(language) - self.keys(FALLBACK_LANGUAGE)


    def add_catalog(self, language: str, data: Mapping[str, Any]) -> None:
        """Register additional strings (used by the plugin API)."""
        catalog = self._catalogs.setdefault(language, LocaleCatalog(language))
        catalog.strings.update(_flatten(data))

    # ------------------------------------------------------------------
    # Lookup & interpolation
    # ------------------------------------------------------------------
    def raw(self, key: str, language: str = FALLBACK_LANGUAGE) -> str:
        catalog = self._catalogs.get(language)
        if catalog:
            value = catalog.get(key)
            if value is not None:
                return value
        fallback = self._catalogs.get(FALLBACK_LANGUAGE)
        if fallback:
            value = fallback.get(key)
            if value is not None:
                self._missing.add((language, key))
                return value
        raise MissingTranslationError(key, language)

    def t(
        self,
        key: str,
        language: str = FALLBACK_LANGUAGE,
        /,
        *,
        localize_digits: bool = False,
        default: str | None = None,
        **variables: Any,
    ) -> str:
        """Translate ``key`` and interpolate ``variables``.

        Parameters
        ----------
        localize_digits:
            Convert ASCII digits in the *final* string to Persian digits when
            ``language == "fa"``. Off by default for IDs/usernames.
        default:
            Value returned when the key is missing entirely (instead of raising).
        """
        requested = language
        if language not in self._catalogs:
            language = FALLBACK_LANGUAGE
        try:
            template = self.raw(key, language)
        except MissingTranslationError:
            if default is not None:
                return default
            raise
        # Always interpolate so a template with placeholders and no supplied
        # variables fails loudly instead of leaking "{version}" to the user.
        # Passed as a mapping, not as ``**variables``: a catalog may legitimately
        # want the word {language} in its own text, and unpacking made that a
        # TypeError the moment the caller said ``language=...`` — which is how the
        # startup banner, the reply-suggestion header and the AI self-test all
        # ended up printing a raw key instead of a sentence.
        template = self.interpolate(template, key, language, variables)
        if localize_digits and requested == "fa":
            template = to_persian_digits(template)
        return template

    def interpolate(
        self,
        template: str,
        key: str = "<inline>",
        language: str = "en",
        variables: Mapping[str, Any] | None = None,
        /,
        **extra: Any,
    ) -> str:
        """Safely format ``template`` and validate that all fields are supplied.

        Everything after ``template`` that this method needs for its own
        bookkeeping is *positional-only*, so a catalog is free to use
        ``{language}``, ``{key}`` or ``{variables}`` as placeholders. Supplying
        them as keywords therefore reaches the template, not this signature —
        which is what makes ``"Language: {language}"`` work.
        """
        values: dict[str, Any] = dict(variables or {})
        values.update(extra)
        return self._interpolate(template, key=key, language=language, variables=values)

    def _interpolate(self, template: str, *, key: str, language: str, variables: Mapping[str, Any]) -> str:
        formatter = string.Formatter()
        try:
            parsed = list(formatter.parse(template))
        except ValueError as exc:
            raise LocaleFormatError(key, f"malformed template: {exc}") from exc

        required: set[str] = set()
        for _, field_name, _, _ in parsed:
            if field_name is None:
                continue
            # strip attribute/index access: {user.name} -> user
            root = re.split(r"[.\[]", field_name, maxsplit=1)[0]
            if root:
                required.add(root)

        missing = required - set(variables)
        if missing:
            raise LocaleFormatError(
                key, f"missing variable(s): {', '.join(sorted(missing))} (language={language})"
            )
        try:
            return template.format(**variables)
        except (KeyError, IndexError, ValueError) as exc:
            raise LocaleFormatError(key, f"interpolation failed: {exc}") from exc

    def plural(
        self,
        key: str,
        count: int,
        language: str = FALLBACK_LANGUAGE,
        /,
        **variables: Any,
    ) -> str:
        """Translate a pluralized key.

        Convention: ``key.one`` and ``key.other`` exist in the catalogs.
        Persian uses ``other`` for every count > 1, which is correct for
        Persian grammar (singular noun after numbers).
        """
        form = "one" if count == 1 else "other"
        candidate = f"{key}.{form}"
        if not self.has(candidate, language) and not self.has(candidate, FALLBACK_LANGUAGE):
            candidate = key
        return self.t(candidate, language, count=count, localize_digits=False, **variables)

    def language_name(self, code: str, in_language: str = "en") -> str:
        return LANGUAGE_NAMES.get(code, {}).get(in_language, code)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        """Return human-readable validation problems (empty means healthy)."""
        problems: list[str] = []
        base_keys = self.keys(FALLBACK_LANGUAGE)
        if not base_keys:
            problems.append("English catalog is empty")
        for language in self.languages:
            if language == FALLBACK_LANGUAGE:
                continue
            missing = sorted(self.missing_keys(language))
            if missing:
                preview = ", ".join(missing[:5])
                problems.append(f"{language}: {len(missing)} missing key(s) e.g. {preview}")
        for language in self.languages:
            for key in sorted(self.keys(language)):
                template = self.raw(key, language)
                try:
                    self._check_template(template, key, language)
                except LocaleFormatError as exc:
                    problems.append(str(exc))
        # A translation that drops a placeholder is not a formatting error — it
        # renders, just without the name, the count, the time. The only way to see
        # it is to compare the sets, so that is what happens here. An *extra*
        # placeholder is worse: it raises at render time, inside a handler.
        for language in self.languages:
            if language == FALLBACK_LANGUAGE:
                continue
            for key in sorted(self.keys(language) & self.keys(FALLBACK_LANGUAGE)):
                expected = self.find_placeholders(key, FALLBACK_LANGUAGE)
                actual = self.find_placeholders(key, language)
                if expected == actual:
                    continue
                parts = []
                if expected - actual:
                    parts.append("missing " + ", ".join(sorted(expected - actual)))
                if actual - expected:
                    parts.append("unexpected " + ", ".join(sorted(actual - expected)))
                problems.append(f"{language}: {key} placeholders differ ({'; '.join(parts)})")
        return problems

    def _check_template(self, template: str, key: str, language: str) -> None:
        formatter = string.Formatter()
        try:
            parsed = list(formatter.parse(template))
        except ValueError as exc:
            raise LocaleFormatError(key, f"malformed template in {language}: {exc}") from exc
        names = {f[1].split(".")[0].split("[")[0] for f in parsed if f[1]}
        if "" in names:
            raise LocaleFormatError(key, f"positional placeholder in {language} template")

    def find_placeholders(self, key: str, language: str = FALLBACK_LANGUAGE) -> set[str]:
        """Placeholders used by ``key`` in ``language``."""
        template = self.raw(key, language)
        return {f[1].split(".")[0].split("[")[0] for f in string.Formatter().parse(template) if f[1]}


def normalize_language_code(value: str | None, fallback: str = "en") -> str:
    """Normalize user input such as ``FA``, ``fa-IR``, ``farsi``, ``فارسی``."""
    if not value:
        return fallback
    text = value.strip().lower()
    aliases = {
        "fa": "fa", "fa-ir": "fa", "farsi": "fa", "persian": "fa", "فارسی": "fa", "پارسی": "fa",
        "en": "en", "en-us": "en", "en-gb": "en", "english": "en", "انگلیسی": "en",
        "auto": "auto", "خودکار": "auto",
    }
    if text in aliases:
        return aliases[text]
    if text.startswith("fa"):
        return "fa"
    if text.startswith("en"):
        return "en"
    return fallback


def digits_for(language: str, value: str | int) -> str:
    """Render a number using the digits conventional for ``language``."""
    text = to_latin_digits(str(value))
    return to_persian_digits(text) if language == "fa" else text
