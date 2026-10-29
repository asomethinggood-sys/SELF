"""Yura's persona: the content bank, the command, and every automation gate.

The automation can send messages by itself, so each gate is tested on its own —
a regression here would mean the bot talking in a chat it should never touch.
"""

from __future__ import annotations

import time

import pytest
from conftest import FakeMessage, invoke, last_text

from havoc import persona
from havoc.event_engine import EventType, normalize_message
from havoc.identity import NAMES, display, tagline


def _event(runtime, text: str, *, chat_id: int = 42, sender_id: int = 42, outgoing: bool = True):
    runtime.permissions.set_self_id(42)
    message = FakeMessage(id=900, chat_id=chat_id, sender_id=sender_id, text=text, out=outgoing)
    return normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)


async def _enable(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def test_the_bot_is_called_yura_in_both_languages() -> None:
    assert NAMES == {"en": "Yura", "fa": "یورا"}
    assert display("en") == "Yura"
    assert display("fa") == "یورا"
    assert display("de").endswith("Yura")  # unknown falls back to English
    assert tagline("en") and tagline("fa") and tagline("en") != tagline("fa")


async def test_the_name_reaches_user_facing_text(runtime) -> None:
    await invoke(runtime, ".about")
    text = last_text(runtime)
    assert "Yura" in text and "HAVOC" not in text


# ---------------------------------------------------------------------------
# Content bank
# ---------------------------------------------------------------------------
def test_every_line_is_safe_and_bilingual() -> None:
    for scenario in persona.SCENARIOS:
        assert set(scenario.lines) == {"en", "fa"}, scenario.key
        assert set(scenario.keywords) == {"en", "fa"}, scenario.key
        for language, lines in scenario.lines.items():
            assert lines, f"{scenario.key}/{language} is empty"
            for line in lines:
                assert persona.is_safe(line), f"unsafe line in {scenario.key}: {line}"
                assert line == line.strip()
                assert len(line) > 15


def test_no_line_claims_to_be_a_person() -> None:
    """The honesty rule: the bank must not pretend Yura is human."""
    claims = ("i am a real", "i am human", "i'm a real girl", "آدم واقعی هستم")
    for line in persona.all_lines():
        lowered = persona.normalize(line)
        assert not any(claim in lowered for claim in claims), line


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("good night", "night"),
        ("Goodnight!", "night"),
        ("I am so tired today", "tired"),
        ("I'm stressed about the deadline", "stress"),
        ("i did it!!", "proud"),
        ("thanks a lot", "thanks"),
        ("hello", "greet"),
        ("are you a bot?", "real"),
        ("I miss her", "miss"),
        ("I'm hungry", "hungry"),
        ("this is just a normal sentence", None),
    ],
)
def test_scenarios_are_detected_in_english(text: str, expected: str | None) -> None:
    assert persona.detect(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("شب بخیر", "night"),
        ("خسته‌ام", "tired"),
        ("دلم برات تنگ شده", "miss"),
        ("دوستت دارم", "love"),
        ("ممنون", "thanks"),
        ("سلام", "greet"),
        ("یک جمله معمولی", None),
    ],
)
def test_scenarios_are_detected_in_farsi(text: str, expected: str | None) -> None:
    assert persona.detect(text, "fa") == expected


def test_word_boundaries_are_respected() -> None:
    """'hi' must not fire inside 'this' or 'which'."""
    assert persona.detect("this is fine") is None
    assert persona.detect("which one") is None


def test_a_reply_can_be_chosen_deterministically() -> None:
    first = persona.build_line("greet", "en", choice=0)
    assert first == persona.build_line("greet", "en", choice=0)
    assert first in persona.SCENARIO_BY_KEY["greet"].lines["en"]
    assert persona.build_line("greet", "en", choice=99) in persona.SCENARIO_BY_KEY["greet"].lines["en"]
    with pytest.raises(KeyError):
        persona.build_line("not-a-scenario", "en")


def test_aliases_resolve_to_scenarios() -> None:
    assert persona.resolve_scenario("salam").key == "greet"
    assert persona.resolve_scenario("tired").key == "tired"
    assert persona.resolve_scenario("خسته").key == "tired"
    assert persona.resolve_scenario("hello").lines == persona.SCENARIO_BY_KEY["greet"].lines
    assert persona.resolve_scenario("nonsense") is None


def test_own_lines_are_recognized() -> None:
    """The loop guard works from the content, not from remembered state."""
    line = persona.build_line("tired", "en", choice=0)
    assert persona.is_own_line(line) is True
    assert persona.is_own_line("I am tired") is False


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
async def test_yura_card_reports_state(runtime) -> None:
    await invoke(runtime, ".yura")
    text = last_text(runtime)
    assert "Yura" in text
    assert str(len(persona.SCENARIOS)) in text
    assert "off" in text.lower()


async def test_yura_lists_scenarios(runtime) -> None:
    await invoke(runtime, ".yura list")
    text = last_text(runtime)
    for scenario in persona.SCENARIOS:
        assert scenario.key in text


async def test_yura_speaks_a_scenario(runtime) -> None:
    await invoke(runtime, ".yura tired")
    assert last_text(runtime) in persona.SCENARIO_BY_KEY["tired"].lines["en"]


async def test_yura_speaks_farsi_when_asked(runtime) -> None:
    await invoke(runtime, ".yura night", language="fa")
    assert last_text(runtime) in persona.SCENARIO_BY_KEY["night"].lines["fa"]


async def test_yura_drafts_then_sends_from_a_reply(runtime) -> None:
    reply = FakeMessage(id=77, chat_id=100, sender_id=1, text="good night, sleep well")
    await invoke(runtime, ".yura reply", reply=reply)
    draft_text = last_text(runtime)
    assert "night" in draft_text

    await invoke(runtime, ".yura send")
    sent = [message for message in runtime.client.sent if message.text in persona.all_lines()]
    assert sent, "the drafted line was not sent"
    # The draft is consumed, so a second send cannot repeat it.
    await invoke(runtime, ".yura send")
    assert "no draft" in last_text(runtime).lower()


async def test_yura_reply_needs_a_matching_message(runtime) -> None:
    reply = FakeMessage(id=78, chat_id=100, sender_id=1, text="the quarterly numbers")
    await invoke(runtime, ".yura reply", reply=reply)
    assert "scenario" in last_text(runtime).lower()


async def test_yura_rejects_an_unknown_scenario(runtime) -> None:
    await invoke(runtime, ".yura banana")
    text = last_text(runtime)
    assert "banana" not in text  # refused, not echoed as a lucky guess
    assert "tired" in text  # ...and every real choice is offered instead


async def test_yura_switches_on_and_off(runtime) -> None:
    await invoke(runtime, ".the_feature_persona_on_placeholder".replace(".the_feature_persona_on_placeholder", ".yura on"))
    assert await runtime.store.feature_enabled("persona") is True
    await invoke(runtime, ".yura off")
    assert await runtime.store.feature_enabled("persona") is False


# ---------------------------------------------------------------------------
# The automation
# ---------------------------------------------------------------------------
async def test_it_replies_to_the_owners_own_message(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night"))
    sent = [message.text for message in runtime.client.sent]
    assert len(sent) == 1
    assert sent[0] in persona.SCENARIO_BY_KEY["night"].lines["en"]


async def test_it_stays_off_until_enabled(runtime) -> None:
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night"))
    assert runtime.client.sent == []
    assert runtime.persona_replies.skipped >= 1


async def test_it_never_answers_another_chat(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night", chat_id=777))
    assert runtime.client.sent == []


async def test_it_never_answers_incoming_messages(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night", outgoing=False))
    assert runtime.client.sent == []


async def test_it_never_answers_its_own_output(runtime) -> None:
    """Otherwise two Yuras could talk to each other forever."""
    await _enable(runtime)
    runtime.client.sent.clear()
    line = persona.build_line("greet", "en", choice=0)
    await runtime.persona_replies.on_self_message(_event(runtime, line))
    assert runtime.client.sent == []


async def test_it_ignores_commands(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, ".help"))
    assert runtime.client.sent == []


async def test_it_respects_quiet_hours(runtime) -> None:
    await _enable(runtime)
    await runtime.store.set_setting("quiet_hours", {"start": "00:00", "end": "23:59", "timezone": "UTC"})
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night"))
    assert runtime.client.sent == []


async def test_it_respects_the_emergency_stop(runtime) -> None:
    await _enable(runtime)
    await runtime.store.set_emergency_stop(True)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night"))
    assert runtime.client.sent == []


async def test_it_rate_limits_itself(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night"))
    await runtime.persona_replies.on_self_message(_event(runtime, "hello"))
    assert len(runtime.client.sent) == 1  # the cooldown ate the second one


async def test_the_first_reply_is_never_blocked_by_the_clock(runtime) -> None:
    """monotonic() can start near zero on a fresh runner; that must not count."""
    await _enable(runtime)
    replies = runtime.persona_replies
    replies._last_at = None
    replies._window = []
    runtime.client.sent.clear()
    await replies.on_self_message(_event(runtime, "good night"))
    assert len(runtime.client.sent) == 1


async def test_the_hourly_budget_is_a_hard_cap(runtime) -> None:
    await _enable(runtime)
    replies = runtime.persona_replies
    replies._window = [time.monotonic()] * 100  # the budget really is spent
    replies._last_at = None
    runtime.client.sent.clear()
    await replies.on_self_message(_event(runtime, "good night"))
    assert runtime.client.sent == []


async def test_nothing_matching_means_no_reply(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "quarterly revenue grew 4%"))
    assert runtime.client.sent == []


async def test_it_uses_the_message_language(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "شب بخیر"))
    sent = [message.text for message in runtime.client.sent]
    assert sent and sent[0] in persona.SCENARIO_BY_KEY["night"].lines["fa"]


async def test_the_self_id_is_read_after_login(runtime) -> None:
    """The subscriber is created before login, when the id is still unknown."""
    await _enable(runtime)
    runtime.permissions.set_self_id(None)
    runtime.client.sent.clear()
    message = FakeMessage(id=902, chat_id=42, sender_id=42, text="good night", out=True)
    await runtime.persona_replies.on_self_message(
        normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)
    )
    assert runtime.client.sent == []  # no idea who the owner is yet

    runtime.permissions.set_self_id(42)
    runtime.persona_replies._last_at = None
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "good night"))
    assert len(runtime.client.sent) == 1


def test_the_new_features_are_off_by_default() -> None:
    """Anything that speaks or sends has to be switched on deliberately."""
    from havoc.services.storage import DEFAULT_FEATURES, FEATURE_GROUPS

    assert DEFAULT_FEATURES["persona"] is False
    assert DEFAULT_FEATURES["media"] is False
    assert set(FEATURE_GROUPS["persona"]) == {"persona", "media"}


async def test_features_command_lists_the_persona_group(runtime) -> None:
    await invoke(runtime, ".features")
    text = last_text(runtime)
    assert "persona" in text and "media" in text


def test_the_bank_has_playful_variants_without_claiming_to_be_human() -> None:
    """More than one way to say hello: warmth is a tone, not an identity claim."""
    greet = next(scenario for scenario in persona.SCENARIOS if scenario.key == "greet")
    assert len(greet.lines["en"]) >= 4 and len(greet.lines["fa"]) >= 4
    love = next(scenario for scenario in persona.SCENARIOS if scenario.key == "love")
    assert len(love.lines["en"]) >= 4
    # Flirty lines still know what they are.
    for line in (*greet.lines["en"], *love.lines["en"]):
        assert "i am human" not in line.lower()
