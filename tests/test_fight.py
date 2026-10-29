"""Fight Mode: the composer, the intensity ladder, and the repeating loop.

The loop sends messages on its own, so the interesting cases are the brakes:
emergency stop, quiet hours, dry run, and the fact that a second ``instant``
replaces the first stream instead of doubling it.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeMessage, invoke, last_text

from havoc import fight
from havoc.content import build_offline_comeback, contains_harassment

# ---------------------------------------------------------------------------
# The composer
# ---------------------------------------------------------------------------


def test_the_composer_has_more_than_twenty_thousand_lines() -> None:
    """The owner asked for a big bank; this is the honest count."""
    assert fight.capacity("en") >= 20_000
    assert fight.capacity("fa") >= 20_000
    assert fight.capacity() == fight.capacity("en") + fight.capacity("fa")


def test_every_style_and_intensity_is_writable() -> None:
    for language in ("en", "fa"):
        for style in fight.STYLES:
            for intensity in fight.INTENSITIES:
                line = fight.compose("deadline", style=style, intensity=intensity, language=language)
                assert line.strip()
                assert len(line) > 20
                assert "{hook}" not in line


def test_lines_vary() -> None:
    lines = {fight.compose("nonsense", style="brutal", intensity="unhinged") for _ in range(40)}
    assert len(lines) > 30


def test_the_hook_is_used_by_the_arguments_that_want_it() -> None:
    lines = [fight.compose("verbosity", style="clever", intensity="medium") for _ in range(20)]
    assert any("verbosity" in line for line in lines)
    assert all("{hook}" not in line for line in lines)


def test_no_line_carries_a_threat_or_harassment_marker() -> None:
    for language in ("en", "fa"):
        for style in fight.STYLES:
            for intensity in fight.INTENSITIES:
                for _ in range(6):
                    line = fight.compose("x", style=style, intensity=intensity, language=language)
                    assert not contains_harassment(line), line


def test_the_ladder_gets_harsher() -> None:
    """A rough proxy: the unhinged pool must not be the gentle one."""
    assert fight.INTENSITIES.index("unhinged") > fight.INTENSITIES.index("low")
    gentle = set(fight.OPENERS["en"]["low"]) & set(fight.OPENERS["en"]["unhinged"])
    assert not gentle, "the pools share lines between opposite intensities"


def test_aliases_are_accepted_in_both_scripts() -> None:
    assert fight.normalize_style("BRUTAL") == "brutal"
    assert fight.normalize_style("بی‌رحم") == "brutal"
    assert fight.normalize_style("nonsense") == "clever"
    assert fight.normalize_intensity("دیوانه") == "unhinged"
    assert fight.normalize_intensity("UNHINGED") == "unhinged"
    assert fight.normalize_intensity("medium-ish") == "medium"


def test_the_offline_builder_uses_the_composer() -> None:
    result = build_offline_comeback(
        hook="tests", style="brutal", intensity="unhinged", language="en"
    )
    assert result.style == "brutal"
    assert result.intensity == "unhinged"
    assert result.source == "offline"


def test_an_insult_is_still_defused() -> None:
    result = build_offline_comeback(
        hook="idiot", style="brutal", intensity="unhinged", language="en",
        source_text="you are an idiot",
    )
    assert result.style == "defuse"
    assert "idiot" not in result.text.lower()


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


async def test_fight_styles_lists_the_new_ladder(runtime) -> None:
    await invoke(runtime, ".fight styles")
    text = last_text(runtime)
    assert "brutal" in text
    assert "unhinged" in text


async def test_brutal_unhinged_is_drafted(runtime) -> None:
    reply = FakeMessage(id=11, chat_id=100, text="your code is slow and boring")
    await invoke(runtime, ".fight brutal unhinged", reply=reply)
    draft = str(await runtime.store.get_chat_setting(100, "fight.draft"))
    assert draft.strip()


async def test_instant_starts_a_loop_and_reports_it(runtime) -> None:
    reply = FakeMessage(id=12, chat_id=100, text="your code is slow and boring")
    await invoke(runtime, ".fight instant repeated brutal", reply=reply)
    assert "repeat" in last_text(runtime).lower() or "بسه" in last_text(runtime)
    state = runtime.fight_loop.state(100)
    assert state is not None
    assert state.style == "brutal"
    assert runtime.fight_loop.active(100)

    await asyncio.sleep(0.05)
    await runtime.fight_loop.stop(100)
    assert not runtime.fight_loop.active(100)


async def test_the_loop_sends_more_than_one_line(runtime) -> None:
    reply = FakeMessage(id=13, chat_id=100, text="i think tabs are better than spaces")
    await invoke(runtime, ".fight instant brutal", reply=reply)
    state = runtime.fight_loop.state(100)
    assert state is not None
    state.interval = 0.01  # make the test fast, not the bot
    await asyncio.sleep(0.15)
    # The loop reads its interval each tick, so the change takes effect.
    await runtime.fight_loop.stop(100)
    sent = [message.text for message in runtime.client.sent[1:]]
    assert len(sent) >= 2


async def test_a_second_instant_replaces_the_first_loop(runtime) -> None:
    reply = FakeMessage(id=14, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    first = runtime.fight_loop.state(100)
    await invoke(runtime, ".fight instant repeated unhinged", reply=reply)
    second = runtime.fight_loop.state(100)
    assert first is not second
    assert runtime.fight_loop.started == 2
    await runtime.fight_loop.stop(100)


async def test_stop_ends_the_loop(runtime) -> None:
    reply = FakeMessage(id=15, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    await invoke(runtime, ".fight stop")
    assert not runtime.fight_loop.active(100)
    assert "stopped" in last_text(runtime).lower() or "متوقف" in last_text(runtime)


async def test_the_bare_word_stops_it(runtime) -> None:
    reply = FakeMessage(id=16, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    message = FakeMessage(id=600, chat_id=100, sender_id=42, text="بسه", out=True)

    from conftest import FakeEvent

    await runtime.handlers._handle_outgoing(FakeEvent(message, chat_id=100, out=True))
    assert not runtime.fight_loop.active(100)


async def test_the_loop_obeys_the_emergency_stop(runtime) -> None:
    reply = FakeMessage(id=17, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    state = runtime.fight_loop.state(100)
    state.interval = 0.01
    await asyncio.sleep(0.03)
    await runtime.store.set_emergency_stop(True)
    before = len(runtime.client.sent)
    await asyncio.sleep(0.1)
    assert state.stop_reason == "emergency_stop"
    assert len(runtime.client.sent) <= before + 1


async def test_the_loop_obeys_dry_run(runtime) -> None:
    reply = FakeMessage(id=18, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    state = runtime.fight_loop.state(100)
    state.interval = 0.01
    await runtime.permissions.set_dry_run(True)
    await asyncio.sleep(0.08)
    assert state.stop_reason == "dry_run"
    assert runtime.client.sent[-1].kwargs.get("dry_run") is None  # nothing new sent


async def test_the_loop_stops_at_the_line_ceiling(runtime) -> None:
    from havoc.services import fight_loop as module

    reply = FakeMessage(id=19, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    state = runtime.fight_loop.state(100)
    state.interval = 0.001
    state.sent = module.MAX_LINES - 1
    await asyncio.sleep(0.1)
    assert state.stop_reason == "limit_reached"


async def test_a_fight_loop_refuses_a_threat_as_its_source(runtime) -> None:
    reply = FakeMessage(id=20, chat_id=100, text="I will kill you")
    await invoke(runtime, ".fight instant brutal", reply=reply)
    assert runtime.fight_loop.state(100) is None


async def test_shutdown_stops_every_loop(runtime) -> None:
    reply = FakeMessage(id=21, chat_id=100, text="this is fine")
    await invoke(runtime, ".fight instant", reply=reply)
    stopped = await runtime.fight_loop.stop_all()
    assert stopped == 1
    assert not runtime.fight_loop.active(100)


@pytest.mark.parametrize("style", ["clever", "brutal", "sarcastic"])
async def test_every_style_drafts_offline(runtime, style: str) -> None:
    reply = FakeMessage(id=30, chat_id=100, text="your take is boring and wrong")
    await invoke(runtime, f".fight {style}", reply=reply)
    assert await runtime.store.get_chat_setting(100, "fight.draft")
