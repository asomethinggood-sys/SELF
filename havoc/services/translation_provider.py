"""Optional translation providers with an offline-safe contract.

Supported
---------
``ai``              the model she already talks with — by default Gemma 4 31B on
                    the free Google keys. This is the one that is *on* without
                    anybody configuring anything: if she can speak, she can
                    translate, which is how ``.translate`` stopped answering
                    "you have no API".
``mymemory``        free, keyless MyMemory API (rate limited; good for short text).
``libretranslate``  self-hosted or hosted LibreTranslate instance.

A translation engine is not a translator in the dictionary sense: it is asked the
same way a person would be, told to answer with the translation and nothing else,
and its answer is cleaned up (code fences, "Translation:" prefixes, quotes)
before it is shown. Nothing but the text the user typed is sent.

Nothing here is required for HAVOC to start. When a provider fails, the error
carries a ``kind`` so the command layer can explain what to do (spec §10) and
never claims success.

Feature areas: 61, 62 (preview is the command layer's job), 70.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ai_provider import AIError, AIMessage
from .http import HttpError, get_json, post_json

MAX_CHARS = 4500
#: The model a translation is asked of, by default. It is in the router's
#: catalogue under this name and on the free Google tier, so the command works
#: on a clone with nothing but the owner's keys.
TRANSLATE_MODEL = "gemma-4-31b-it"
#: Codes to the names a model understands in a sentence.
LANG_NAMES = {
    "en": "English", "fa": "Persian (Farsi)", "ar": "Arabic", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "ru": "Russian", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean", "hi": "Hindi", "ur": "Urdu", "nl": "Dutch", "pl": "Polish",
    "sv": "Swedish", "he": "Hebrew", "az": "Azerbaijani", "ku": "Kurdish",
}
#: What the model is told. Short on purpose: a long instruction invites a long
#: answer, and a translation with a preamble is a translation that failed.
_TRANSLATE_SYSTEM = (
    "you are a translator. you return only the translation of the text you are "
    "given, in the requested language, keeping the meaning, tone and line breaks. "
    "no preamble, no quotes, no notes, no code fences, no explanation. if the text "
    "is already in the target language, return it unchanged."
)


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


class AITranslationProvider(TranslationProvider):
    """Translates with the model she already talks to (Gemma 31B by default).

    The router is duck-typed: anything with ``converse`` works, so a single-key
    :class:`AIProvider` is a valid engine too. ``only`` is passed when the engine
    supports pinning and quietly skipped when it does not, because the point is a
    working translation, not a particular HTTP shape.
    """

    name = "ai"

    def __init__(self, ai: object, *, model: str = TRANSLATE_MODEL, timeout: float = 30.0) -> None:
        super().__init__(enabled=bool(getattr(ai, "available", False)), timeout=timeout)
        self.ai = ai
        self.model = model

    def describe(self) -> dict[str, object]:
        return {
            "provider": self.name if self.enabled else "none",
            "model": self.model,
            "requests": self.requests,
            "failures": self.failures,
        }

    async def translate(self, text: str, target: str, source: str = "auto") -> TranslationResult:
        body = self._validate(text)
        if not self.enabled:
            raise TranslationError("translation provider is not configured", kind="disabled")
        wanted = LANG_NAMES.get(target, target)
        ask = f"translate to {wanted}:\n\n{body}"
        if source and source != "auto":
            ask = f"translate from {LANG_NAMES.get(source, source)} to {wanted}:\n\n{body}"
        thread = [AIMessage("system", _TRANSLATE_SYSTEM), AIMessage("user", ask)]
        self.requests += 1
        try:
            answer = await self._ask(thread, body)
        except AIError as exc:
            self.failures += 1
            raise TranslationError(f"{exc.kind}: {exc}", kind=exc.kind) from exc
        except Exception as exc:  # noqa: BLE001 - a stuck engine is not a crash
            self.failures += 1
            raise TranslationError(f"{type(exc).__name__}: {exc}", kind="provider_error") from exc
        translated = _tidy(_clean(str(answer)))
        if not translated:
            self.failures += 1
            raise TranslationError("the model returned nothing", kind="bad_response")
        return TranslationResult(
            text=translated,
            source=source,
            target=target,
            provider=self.model,
            detected_source=None,
        )

    async def _ask(self, thread: list[AIMessage], body: str) -> str:
        # A translation is longer than the sentence it came from, and a truncated
        # one ends mid-word; the budget scales with the text, within reason.
        budget = max(200, min(1600, len(body) * 3))
        converse = getattr(self.ai, "converse", None)
        if converse is None:
            raise AIError("the AI engine has no converse()", kind="disabled")
        try:
            return await converse(thread, max_tokens=budget, temperature=0.2, only=self.model)
        except TypeError:
            # An engine without the pin: still a translator, just not a pinned one.
            return await converse(thread, max_tokens=budget, temperature=0.2)


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


#: Labels a model likes to put in front of an answer it was told not to decorate.
_LABEL_RE = re.compile(
    r"^\s*(?:translation|translated text|here is the translation|ترجمه|ترجمه\s*:|译文)\s*[:：]\s*",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^```[\w-]*\s*\n?(.*?)\n?```$", re.DOTALL)


def _tidy(text: str) -> str:
    """Undo the wrapping a model adds even when it was told not to.

    Only shapes that cannot be part of a translation: a whole answer inside a
    code fence, a leading "Translation:" label, and quotes around the *entire*
    answer. Anything else — including a quote inside a sentence — is left alone,
    because mangling her text is worse than a stray backtick.
    """
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    body = _LABEL_RE.sub("", body, count=1).strip()
    for left, right in (('"', '"'), ("'", "'"), ("“", "”"), ("«", "»")):
        if len(body) > 1 and body.startswith(left) and body.endswith(right):
            body = body[1:-1].strip()
            break
    return body


def build_translation_provider(config, ai: object | None = None) -> TranslationProvider:
    """The engine ``.translate`` will actually use.

    ``TRANSLATE_PROVIDER`` is honoured when it names one, but the default no
    longer means "off": if there is an AI engine, translation is done by it. That
    is the whole fix for a command answering "you have no API" on a bot with five
    keys — she can speak, so she can translate, and there is nothing to configure.
    """
    choice = (getattr(config, "translate_provider", "none") or "none").lower()
    if choice == "mymemory":
        return MyMemoryProvider(timeout=config.translate_timeout)
    if choice == "libretranslate" and config.libretranslate_url:
        return LibreTranslateProvider(
            url=config.libretranslate_url,
            api_key=config.libretranslate_api_key,
            timeout=config.translate_timeout,
        )
    if choice in {"ai", "gemma", "gemma-4-31b", "gemma-4-31b-it", "none"}:
        engine = AITranslationProvider(
            ai, model=TRANSLATE_MODEL, timeout=config.translate_timeout
        )
        if engine.available:
            return engine
    return DisabledTranslationProvider(timeout=config.translate_timeout)
