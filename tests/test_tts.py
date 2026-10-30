"""Her voice, without a network.

The two providers are injected (a fake ``post`` and a fake ``edge_speak``), so
what is tested is the *policy*: which provider is chosen, what happens when the
key is missing, and that nothing here raises into the reply path.
"""

from __future__ import annotations

import base64
import json

import pytest

from havoc.services.tts import (
    EDGE_VOICES,
    GROQ_BASE_URL,
    GROQ_MODEL,
    TextToSpeech,
    TTSError,
    TTSResult,
)


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"ID3fake-mp3") -> None:
        self.status = status
        self.body = body


class _Edge:
    def __init__(self, audio: bytes = b"fake-ogg", *, fail: bool = False) -> None:
        self.audio = audio
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, text: str, voice: str) -> bytes:
        self.calls.append((text, voice))
        if self.fail:
            raise TTSError("no route", kind="network")
        return self.audio


class _Post:
    def __init__(self, response: _Response | None = None) -> None:
        self.response = response or _Response()
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, url: str, body: dict, headers: dict):
        self.calls.append((url, body, headers))
        return self.response


async def _engine(runtime, *, edge=None, post=None) -> TextToSpeech:
    return TextToSpeech(runtime, post=post, edge_speak=edge or _Edge())


# ---------------------------------------------------------------------------
# With no key at all
# ---------------------------------------------------------------------------
async def test_edge_needs_no_key_and_still_speaks(runtime) -> None:
    edge = _Edge()
    engine = await _engine(runtime, edge=edge)
    assert await engine.has_engine() is True
    result = await engine.say("hello there")
    assert result is not None
    assert result.provider == "edge"
    assert result.audio == b"fake-ogg"
    assert edge.calls == [("hello there", EDGE_VOICES["en"])]


async def test_persian_gets_a_persian_voice(runtime) -> None:
    edge = _Edge()
    engine = await _engine(runtime, edge=edge)
    await engine.say("سلام", "fa")
    assert edge.calls[-1][1] == EDGE_VOICES["fa"]


async def test_a_blank_line_is_not_spoken(runtime) -> None:
    engine = await _engine(runtime)
    assert await engine.say("   ") is None
    assert engine.spoken == 0


async def test_a_failure_returns_none_instead_of_raising(runtime) -> None:
    engine = await _engine(runtime, edge=_Edge(fail=True))
    assert await engine.say("hi") is None
    assert engine.last_error


def test_the_audio_suffix_matches_what_it_is() -> None:
    assert TTSResult(b"x", mime="audio/mpeg", provider="edge").suffix() == ".mp3"
    assert TTSResult(b"x", mime="audio/ogg", provider="edge").suffix() == ".ogg"


# ---------------------------------------------------------------------------
# With a free Groq key
# ---------------------------------------------------------------------------
async def test_a_stored_key_is_used_and_the_groq_endpoint_is_called(runtime) -> None:
    post = _Post()
    engine = await _engine(runtime, post=post)
    await engine.set_key("gsk_free_tier_key")
    await runtime.store.set_setting("tts.provider", "openai")

    result = await engine.say("say it again")

    assert result is not None and result.provider == "openai"
    url, body, headers = post.calls[0]
    assert url == f"{GROQ_BASE_URL}/audio/speech"
    assert body["model"] == GROQ_MODEL
    assert body["input"] == "say it again"
    assert headers["Authorization"] == "Bearer gsk_free_tier_key"


async def test_the_key_is_stored_base64_and_never_in_clear(runtime) -> None:
    engine = await _engine(runtime)
    await engine.set_key("gsk_free_tier_key")
    stored = await runtime.store.get_setting("tts.key", None)
    assert stored and "gsk_free_tier_key" not in stored
    assert base64.b64decode(stored).decode() == "gsk_free_tier_key"
    # ...and reading it back gives the key, not the blob.
    assert await engine.key() == "gsk_free_tier_key"


async def test_a_401_is_reported_as_a_key_problem_and_falls_back_to_edge(runtime) -> None:
    post = _Post(_Response(status=401, body=json.dumps({"error": {"message": "bad key"}}).encode()))
    edge = _Edge()
    engine = await _engine(runtime, edge=edge, post=post)
    await engine.set_key("wrong")
    await runtime.store.set_setting("tts.provider", "openai")

    result = await engine.say("hi")

    assert result is not None and result.provider == "edge"
    assert edge.calls, "the keyless voice must take over"


async def test_a_429_is_named_not_swallowed(runtime) -> None:
    post = _Post(_Response(status=429, body=b'{"error":{"message":"slow down"}}'))
    engine = await _engine(runtime, edge=_Edge(fail=True), post=post)
    await engine.set_key("k")
    await runtime.store.set_setting("tts.provider", "openai")
    assert await engine.say("hi") is None
    assert "rate_limited" in engine.last_error


async def test_openai_without_a_key_never_calls_the_network(runtime) -> None:
    post = _Post()
    engine = await _engine(runtime, post=post)
    await runtime.store.set_setting("tts.provider", "openai")
    result = await engine.say("hi")
    assert result is not None and result.provider == "edge"
    assert post.calls == []


async def test_the_status_line_says_what_is_missing(runtime) -> None:
    engine = await _engine(runtime)
    await runtime.store.set_setting("tts.provider", "openai")
    assert "no key" in await engine.describe()
    await engine.set_key("k")
    assert "configured" in await engine.describe()


async def test_a_long_line_is_cut_before_it_costs_money_or_breaks(runtime) -> None:
    edge = _Edge()
    engine = await _engine(runtime, edge=edge)
    await engine.say("x" * 5000)
    assert len(edge.calls[0][0]) == 600


@pytest.mark.parametrize("language", ["en", "fa"])
def test_every_language_has_a_voice(language: str) -> None:
    assert EDGE_VOICES[language]
