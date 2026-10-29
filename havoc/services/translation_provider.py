"""Optional translation providers with an offline-safe contract.

Supported
---------
``none``            translation disabled; callers show a localized explanation.
``mymemory``        free, keyless MyMemory API (rate limited; good for short text).
``libretranslate``  self-hosted or hosted LibreTranslate instance.

Nothing here is required for HAVOC to start. When a provider fails, the error
carries a ``kind`` so the command layer can explain what to do (spec §10) and
never claims success.

Feature areas: 61, 62 (preview is the command layer's job), 70.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .http import HttpError, get_json, post_json

MAX_CHARS = 4500


class TranslationError(Exception):
    def __init__(self, message: str, kind: str = "translation_error") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class TranslationResult:
    text: str
    source: str
    target: str
    provider: str
    detected_source: str | None = None

    @property
    def changed(self) -> bool:
        return self.text.strip() != ""


_FA_CODES = {"fa", "persian", "farsi"}
_EN_CODES = {"en", "english"}

_LANG_ALIASES = {
    "fa": "fa", "فارسی": "fa", "persian": "fa", "farsi": "fa",
    "en": "en", "english": "en", "انگلیسی": "en",
    "ar": "ar", "arabic": "ar", "عربی": "ar",
    "de": "de", "german": "de", "آلمانی": "de",
    "fr": "fr", "french": "fr", "فرانسوی": "fr",
    "es": "es", "spanish": "es", "اسپانیایی": "es",
    "ru": "ru", "russian": "ru", "روسی": "ru",
    "tr": "tr", "turkish": "tr", "ترکی": "tr",
    "zh": "zh", "chinese": "zh", "چینی": "zh",
}


#: Two-letter codes HAVOC accepts as an explicit translation target.
KNOWN_CODES = frozenset(
    {"en", "fa", "ar", "de", "fr", "es", "it", "pt", "ru", "tr", "zh", "ja", "ko",
     "hi", "ur", "nl", "pl", "sv", "he", "az", "ku"}
)
#: Full words users may type instead of a code.
KNOWN_NAMES = frozenset(set(_LANG_ALIASES))


def is_lang_token(value: str | None) -> bool:
    """Whether ``value`` looks like a language request rather than text."""
    if not value:
        return False
    token = value.strip().lower()
    return token in KNOWN_CODES or token in KNOWN_NAMES


def normalize_lang(value: str | None, default: str = "en") -> str:
    if not value:
        return default
    text = value.strip().lower()
    if text in _LANG_ALIASES:
        return _LANG_ALIASES[text]
    if len(text) == 2 and text.isalpha():
        return text
    return default


class TranslationProvider:
    name = "none"

    def __init__(self, *, enabled: bool = False, timeout: float = 15.0) -> None:
        self.enabled = enabled
        self.timeout = timeout
        self.requests = 0
        self.failures = 0

    @property
    def available(self) -> bool:
        return self.enabled

    def describe(self) -> dict[str, object]:
        return {"provider": self.name if self.enabled else "none", "requests": self.requests, "failures": self.failures}

    async def translate(self, text: str, target: str, source: str = "auto") -> TranslationResult:
        raise NotImplementedError

    def _validate(self, text: str) -> str:
        body = (text or "").strip()
        if not body:
            raise TranslationError("nothing to translate", kind="empty_text")
        if len(body) > MAX_CHARS:
            raise TranslationError(f"text is too long (max {MAX_CHARS} characters)", kind="too_long")
        return body


class DisabledTranslationProvider(TranslationProvider):
    name = "none"

    async def translate(self, text: str, target: str, source: str = "auto") -> TranslationResult:
        raise TranslationError("translation provider is not configured", kind="disabled")


class MyMemoryProvider(TranslationProvider):
    """Free, keyless provider. Documented limitation: short texts only."""

    name = "mymemory"
    ENDPOINT = "https://api.mymemory.translated.net/get"

    def __init__(self, *, timeout: float = 15.0, contact: str = "") -> None:
        super().__init__(enabled=True, timeout=timeout)
        self.contact = contact

    async def translate(self, text: str, target: str, source: str = "auto") -> TranslationResult:
        body = self._validate(text)
        src = source if source and source != "auto" else "en"
        params = {"q": body, "langpair": f"{src}|{target}"}
        if self.contact:
            params["de"] = self.contact
        self.requests += 1
        try:
            data = await get_json(self.ENDPOINT, params=params, timeout=self.timeout)
        except HttpError as exc:
            self.failures += 1
            raise TranslationError(str(exc), kind=exc.kind) from exc

        status = data.get("responseStatus") if isinstance(data, dict) else None
        if isinstance(status, int) and status >= 400:
            self.failures += 1
            message = str(data.get("responseDetails") or status)
            raise TranslationError(message, kind="rate_limited" if status == 429 else "provider_error")
        try:
            translated = data["responseData"]["translatedText"]
        except (KeyError, TypeError) as exc:
            self.failures += 1
            raise TranslationError("unexpected translation response", kind="bad_response") from exc
        translated = _clean(translated)
        if not translated:
            self.failures += 1
            raise TranslationError("provider returned an empty translation", kind="bad_response")
        return TranslationResult(
            text=translated,
            source=src,
            target=target,
            provider=self.name,
            detected_source=None,
        )


class LibreTranslateProvider(TranslationProvider):
    name = "libretranslate"

    def __init__(self, *, url: str, api_key: str = "", timeout: float = 15.0) -> None:
        super().__init__(enabled=bool(url), timeout=timeout)
        self.url = url.rstrip("/")
        self._api_key = api_key

    async def translate(self, text: str, target: str, source: str = "auto") -> TranslationResult:
        body = self._validate(text)
        if not self.enabled:
            raise TranslationError("translation provider is not configured", kind="disabled")
        payload = {"q": body, "source": source or "auto", "target": target, "format": "text"}
        if self._api_key:
            payload["api_key"] = self._api_key
        self.requests += 1
        try:
            data = await post_json(f"{self.url}/translate", payload, timeout=self.timeout)
        except HttpError as exc:
            self.failures += 1
            raise TranslationError(str(exc), kind=exc.kind) from exc
        try:
            translated = data["translatedText"]
        except (KeyError, TypeError) as exc:
            self.failures += 1
            raise TranslationError("unexpected translation response", kind="bad_response") from exc
        detected = data.get("detectedLanguage", {}).get("language") if isinstance(data, dict) else None
        return TranslationResult(
            text=_clean(translated),
            source=source,
            target=target,
            provider=self.name,
            detected_source=detected,
        )


def _clean(text: str) -> str:
    # Some providers HTML-escape input; undo that only for entities.
    text = re.sub(r"<br\s*/?>", "\n", str(text))
    return (
        text.replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .strip()
    )


def build_translation_provider(config) -> TranslationProvider:
    if config.translate_provider == "mymemory":
        return MyMemoryProvider(timeout=config.translate_timeout)
    if config.translate_provider == "libretranslate" and config.libretranslate_url:
        return LibreTranslateProvider(
            url=config.libretranslate_url,
            api_key=config.libretranslate_api_key,
            timeout=config.translate_timeout,
        )
    return DisabledTranslationProvider(timeout=config.translate_timeout)
