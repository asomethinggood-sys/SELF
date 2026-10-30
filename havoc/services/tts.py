"""Her voice: a text-to-speech path that works with a free key, or no key at all.

Two providers, in the order the owner asked for:

``edge`` — Microsoft Edge's read-aloud engine through the ``edge-tts`` package.
**No account and no key.** It is the default and the fallback, so ``.yura say``
works on a fresh clone with nothing configured.

``openai`` — anything that speaks the OpenAI ``/audio/speech`` shape. That is
**Groq** (free tier: a key from console.groq.com, ``playai-tts``), OpenAI itself,
or a local server. A key is stored the same base64 way the AI keys are, so it is
never in the diff in clear text.

Nothing here speaks unless asked: a voice that arrives uninvited is a different
product. The audio is returned as bytes plus the mime the bytes are, so the
sender decides how to ship it.
"""

from __future__ import annotations

import base64
import json

from ..utils.security import redact_text

__all__ = [
    "EDGE_VOICES",
    "GROQ_BASE_URL",
    "GROQ_MODEL",
    "TTSError",
    "TTSResult",
    "TextToSpeech",
]

#: Free-tier, no card: https://console.groq.com/keys
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "playai-tts"
#: Edge voices that fit her, per language, best first. A *list* because a voice
#: name can disappear from the service: the next one is tried instead of the whole
#: command failing. All of them are young and light, none is a newsreader.
EDGE_VOICES: dict[str, tuple[str, ...]] = {
    "en": ("en-US-AnaNeural", "en-GB-MaisieNeural", "en-US-MichelleNeural"),
    "fa": ("fa-IR-DilaraNeural", "fa-IR-FaridNeural"),
    # ru-RU-DariyaNeural is a child voice — the closest thing the free engine has
    # to the way she actually sounds; Svetlana is the grown-up fallback.
    "ru": ("ru-RU-DariyaNeural", "ru-RU-SvetlanaNeural"),
    # Nanami is the light, soft one; Keita only exists so a broken name still
    # produces speech instead of an error message.
    "ja": ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"),
}
#: How each language is nudged to sound like *her* rather than like a reader.
#: ``rate`` speeds the sentence up a touch, ``pitch`` lifts it into kitten range.
EDGE_TUNING: dict[str, dict[str, str]] = {
    "en": {"rate": "+6%", "pitch": "+22Hz"},
    "fa": {"rate": "+4%", "pitch": "+18Hz"},
    "ru": {"rate": "+6%", "pitch": "+24Hz"},
    "ja": {"rate": "+8%", "pitch": "+26Hz"},
}
#: What a language sounds like when it is only written down.
_SCRIPTS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("fa", (0x0600, 0x06FF)),
    ("ru", (0x0400, 0x04FF)),
    ("ja", (0x3040, 0x30FF)),
)
#: Groq's voices, per language it can actually speak. The free tier has no
#: Persian, Russian or Japanese voice, so those fall back to edge rather than
#: reading the text with an English mouth.
GROQ_VOICES = {
    "en": "Celeste-PlayAI",
    "fa": "Celeste-PlayAI",
    "ru": "Celeste-PlayAI",
    "ja": "Celeste-PlayAI",
}
#: Languages the edge engine is tuned for here.
SPEECH_LANGUAGES = tuple(EDGE_VOICES)

#: Telegram's voice notes are Opus; these two mime types are accepted and
#: re-encoded by the client, so mp3 is fine.
MAX_SPEECH_CHARS = 600
PROVIDER_SETTING = "tts.provider"
KEY_SETTING = "tts.key"
VOICE_SETTING = "tts.voice"
MODEL_SETTING = "tts.model"
BASE_URL_SETTING = "tts.base_url"


class TTSError(Exception):
    """Speech could not be produced, with a machine-readable ``kind``."""

    def __init__(self, message: str, *, kind: str = "tts_error") -> None:
        self.kind = kind
        super().__init__(message)


class TTSResult:
    """Audio, and enough about it to send it."""

    __slots__ = ("audio", "mime", "provider", "voice")

    def __init__(self, audio: bytes, *, mime: str, provider: str, voice: str = "") -> None:
        self.audio = audio
        self.mime = mime
        self.provider = provider
        self.voice = voice

    @property
    def size(self) -> int:
        return len(self.audio)

    def suffix(self) -> str:
        return ".mp3" if "mpeg" in self.mime or "mp3" in self.mime else ".ogg"

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"TTSResult({self.provider}, {self.mime}, {self.size} bytes)"


def detect_speech_language(text: str) -> str:
    """Which voice a *piece of text* should be read in.

    The chat language is not enough: ``.yura say привет`` in an English chat has
    to come out of a Russian mouth. Detection is by script, which is honest for
    the four languages tuned here, and falls back to English.
    """
    body = str(text or "")
    for code, (low, high) in _SCRIPTS:
        if any(low <= ord(character) <= high for character in body):
            return code
    return "en"


def _edge_voices(language: str) -> tuple[str, ...]:
    return EDGE_VOICES.get(language) or EDGE_VOICES["en"]


def _encode_key(raw: str) -> str:
    return base64.b64encode(raw.strip().encode("utf-8")).decode("ascii")


def _decode_key(stored: str) -> str:
    try:
        return base64.b64decode(stored.encode("ascii")).decode("utf-8").strip()
    except Exception:  # noqa: BLE001 - a corrupt value is an empty key, not a crash
        return ""


class TextToSpeech:
    """Speaks a line, through whichever provider is configured and working."""

    def __init__(self, runtime, *, post=None, edge_speak=None) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        self._post = post
        self._edge_speak = edge_speak
        self.last_error = ""
        self.spoken = 0

    # -- configuration ---------------------------------------------------
    async def provider(self) -> str:
        value = await self.runtime.store.get_setting(PROVIDER_SETTING, None)
        return str(value or "auto").strip().lower()

    async def key(self) -> str:
        stored = await self.runtime.store.get_setting(KEY_SETTING, None)
        return _decode_key(str(stored)) if stored else ""

    async def set_key(self, raw: str) -> None:
        await self.runtime.store.set_setting(KEY_SETTING, _encode_key(raw))

    async def voice(self, language: str = "en") -> str:
        stored = await self.runtime.store.get_setting(VOICE_SETTING, None)
        if stored:
            return str(stored)
        if await self.provider() == "openai":
            return GROQ_VOICES.get(language, GROQ_VOICES["en"])
        return _edge_voices(language)[0]

    async def base_url(self) -> str:
        stored = await self.runtime.store.get_setting(BASE_URL_SETTING, None)
        return str(stored or GROQ_BASE_URL).rstrip("/")

    async def model(self) -> str:
        stored = await self.runtime.store.get_setting(MODEL_SETTING, None)
        return str(stored or GROQ_MODEL)

    async def describe(self) -> str:
        """One line for `.yura voice`, naming what is actually usable."""
        chosen = await self.provider()
        has_key = bool(await self.key())
        edge = self._edge_available()
        if chosen == "openai":
            state = "configured" if has_key else "no key"
        elif chosen == "edge":
            state = "ready" if edge else "edge-tts not installed"
        else:
            state = "ready (edge)" if edge else ("key set" if has_key else "no provider")
        return f"{chosen} · {state}"

    async def has_engine(self) -> bool:
        """True when *something* here could actually speak right now."""
        if await self.provider() == "openai" and await self.key():
            return True
        return self._edge_available()

    # -- providers -------------------------------------------------------
    def _edge_available(self) -> bool:
        if self._edge_speak is not None:
            return True
        try:
            import edge_tts  # noqa: F401
        except Exception:  # noqa: BLE001 - optional dependency
            return False
        return True

    async def _edge(self, text: str, voice: str) -> TTSResult:
        if self._edge_speak is not None:
            audio = await self._edge_speak(text, voice)
            return TTSResult(audio, mime="audio/mpeg", provider="edge", voice=voice)
        try:
            import edge_tts
        except Exception as exc:  # noqa: BLE001
            raise TTSError("edge-tts is not installed", kind="unavailable") from exc
        buffer = bytearray()
        tuning = EDGE_TUNING.get(detect_speech_language(text), {})
        try:
            try:
                communicate = edge_tts.Communicate(text, voice, **tuning)
            except TypeError:  # pragma: no cover - an older edge-tts without tuning
                communicate = edge_tts.Communicate(text, voice)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    buffer.extend(chunk["data"])
        except Exception as exc:  # noqa: BLE001 - network or voice name
            raise TTSError(f"edge-tts failed: {redact_text(str(exc))[:120]}", kind="network") from exc
        if not buffer:
            raise TTSError("edge-tts returned no audio", kind="empty")
        return TTSResult(bytes(buffer), mime="audio/mpeg", provider="edge", voice=voice)

    async def _openai(self, text: str, voice: str) -> TTSResult:
        key = await self.key()
        if not key:
            raise TTSError("no TTS key stored", kind="unauthorized")
        url = f"{await self.base_url()}/audio/speech"
        body = {"model": await self.model(), "input": text, "voice": voice, "response_format": "mp3"}
        post = self._post
        if post is None:
            from .http import request

            async def post(target, payload, headers):
                return await request("POST", target, json_body=payload, headers=headers, timeout=45.0)

        try:
            response = await post(url, body, {"Authorization": f"Bearer {key}"})
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise TTSError(f"TTS request failed: {redact_text(str(exc))[:120]}", kind="network") from exc
        status = getattr(response, "status", 200)
        audio = getattr(response, "body", b"") or b""
        if status >= 400 or not audio:
            detail = ""
            try:
                detail = json.loads(audio.decode("utf-8", "replace")).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001 - not JSON, no detail
                detail = audio[:120].decode("utf-8", "replace")
            kind = "rate_limited" if status == 429 else "unauthorized" if status in (401, 403) else "http_error"
            raise TTSError(f"HTTP {status}: {redact_text(detail)[:160]}", kind=kind)
        return TTSResult(audio, mime="audio/mpeg", provider="openai", voice=voice)

    # -- the one public call ---------------------------------------------
    async def say(self, text: str, language: str = "en") -> TTSResult | None:
        """Speak ``text``. ``None`` when no provider could do it — never raises.

        The language is taken from the text itself when the script says more than
        the chat setting does: Russian and Japanese were added for their voices,
        and a Russian sentence in an English chat must not be read by Ana.
        """
        spoken = " ".join(str(text or "").split())[:MAX_SPEECH_CHARS]
        if not spoken:
            return None
        if detect_speech_language(spoken) != "en":
            language = detect_speech_language(spoken)
        chosen = await self.provider()
        # An explicitly chosen voice wins over the per-language default, so
        # ``.yura voice set …`` still means what it says.
        stored = await self.runtime.store.get_setting(VOICE_SETTING, None)
        candidates = [str(stored)] if stored else list(_edge_voices(language))
        order = [chosen] if chosen in {"edge", "openai"} else ["edge", "openai"]
        if chosen == "openai" and not await self.key():
            order = ["edge"]
        # A configured provider that fails falls through to the keyless one: the
        # owner asked for a voice note, not for a lecture about the API key.
        if "openai" in order:
            order.append("edge")
        errors: list[str] = []
        for provider in order:
            voices = candidates if provider == "edge" else [GROQ_VOICES.get(language, GROQ_VOICES["en"])]
            for voice in voices:
                try:
                    if provider == "edge":
                        result = await self._edge(spoken, voice)
                    else:
                        result = await self._openai(spoken, voice)
                except TTSError as exc:
                    errors.append(f"{provider}/{voice}: {exc.kind}")
                    continue
                self.spoken += 1
                self.last_error = ""
                return result
        self.last_error = "; ".join(errors) or "no provider"
        self.logger.warning("tts: %s", self.last_error)
        return None
