"""Yura: her voice, her memory, and every gate on the ambient reply path.

The ambient path can send messages by itself, so each gate is tested on its
own — a regression here would mean the bot talking in a chat it must never
touch.
"""

from __future__ import annotations

import html
import time

import pytest
from conftest import FakeEvent, FakeMessage, invoke, last_text

from havoc import persona
from havoc.event_engine import EventType, normalize_message
from havoc.identity import NAMES, display, tagline


class StubProvider:
    """Stands in for :class:`AIProvider` with a scripted reply."""

    model = "stub-model"
    available = True

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or ["mrrp. hi. (¬‿¬)"])
        self.calls: list[list[object]] = []

    last_max_tokens = 0

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        self.last_max_tokens = max_tokens
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _event(runtime, text: str, *, chat_id: int = 42, sender_id: int = 42, outgoing: bool = True):
    runtime.permissions.set_self_id(42)
    message = FakeMessage(id=900, chat_id=chat_id, sender_id=sender_id, text=text, out=outgoing)
    return normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)


def _plain(runtime) -> str:
    """The last message as Telegram would render it (HTML unescaped)."""
    return html.unescape(last_text(runtime))


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
# The offline bank
# ---------------------------------------------------------------------------
def test_the_bank_clears_three_thousand_lines_per_language() -> None:
    """The complaint that grew the bank: she kept saying the same thing."""
    from havoc import yura_lines

    assert yura_lines.capacity("en") >= 3000
    assert yura_lines.capacity("fa") >= 3000
    assert persona.capacity() == yura_lines.capacity("en") + yura_lines.capacity("fa")


def test_every_generated_line_is_safe_bilingual_and_real() -> None:
    from havoc import yura_lines

    for language in ("en", "fa"):
        # Draw the way the chat does: each line joins the avoid set before the
        # next one is picked, so repeats are not luck, they are a bug.
        seen: set[str] = set()
        for _ in range(120):
            line = persona.build_line(language, avoid=seen)
            assert line not in seen, f"repeat inside the avoid window: {line}"
            seen.add(line)
            assert persona.is_safe(line), f"unsafe line: {line}"
            assert line == line.strip()
            assert len(line) > 20
            assert persona.is_own_line(line), f"unrecognised own line: {line}"
    # The pool pieces themselves must be clean, not just the combinations.
    for language in yura_lines.BODIES:
        for body in yura_lines.BODIES[language]:
            assert persona.is_safe(body), body
            assert persona.is_own_line(yura_lines.pick(language, choice=0))
        for tail in yura_lines.TAILS[language]:
            assert persona.is_safe(tail), tail


def test_the_voice_is_an_e_kitten() -> None:
    """The requested persona, asserted instead of assumed."""
    from havoc import yura_lines

    assert "daddaaa" in yura_lines.PETS["en"]
    assert "دددااااا" in yura_lines.PETS["fa"]
    faces = " ".join(yura_lines.FACES)
    assert any(face in faces for face in ("(¬‿¬)", ">_<", "ᵕ̈", "ᴗ"))
    lines = [persona.build_line("en") for _ in range(60)]
    assert any("daddaaa" in line for line in lines)


def test_no_line_claims_to_be_a_person() -> None:
    claims = ("i am a real", "i am human", "i'm a real girl", "آدم واقعی هستم")
    for line in persona.all_lines():
        lowered = persona.normalize(line)
        assert not any(claim in lowered for claim in claims), line


def test_no_scenario_machinery_survives() -> None:
    """The old keyword/scenario system is gone; a bare call is random."""
    for attribute in ("SCENARIOS", "SCENARIO_BY_KEY", "detect", "resolve_scenario"):
        assert not hasattr(persona, attribute), attribute


def test_a_line_can_be_chosen_deterministically() -> None:
    first = persona.build_line("en", choice=0)
    assert first == persona.build_line("en", choice=0)
    assert persona.is_own_line(first)
    assert persona.is_own_line(persona.build_line("en", choice=999))


def test_an_unknown_language_falls_back_to_english() -> None:
    assert persona.is_own_line(persona.build_line("de", choice=0))


def test_own_lines_are_recognized() -> None:
    line = persona.build_line("en", choice=0)
    assert persona.is_own_line(line) is True
    assert persona.is_own_line("I am tired") is False
    # Case and spacing do not matter; the guard is on content.
    assert persona.is_own_line("  " + line.upper() + " ") is True


def test_the_prompt_carries_voice_memory_and_the_honesty_rule() -> None:
    prompt = persona.system_prompt(
        "en", memories=("her name is Sara",), owner_name="Sam", local_time="2026-10-29 21:00"
    )
    assert "Yura" in prompt
    assert "daddaaa" in prompt
    assert "her name is Sara" in prompt
    assert "Sam" in prompt
    assert "[[remember" in prompt
    assert "bot" in prompt.lower()
    farsi = persona.system_prompt("fa", memories=("اسمش ساراست",))
    assert "یورا" in farsi and "اسمش ساراست" in farsi


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
async def test_a_bare_call_answers_with_a_random_line(runtime) -> None:
    await invoke(runtime, ".yura")
    assert persona.is_own_line(_plain(runtime))


async def test_the_farsi_name_alone_answers_too(runtime) -> None:
    runtime.client.sent.clear()
    message = FakeMessage(id=501, chat_id=100, sender_id=42, text="یورا", out=True)
    await runtime.handlers._handle_outgoing(FakeEvent(message, chat_id=100, out=True))
    assert runtime.client.sent, "a bare Persian call produced nothing"
    assert persona.is_own_line(html.unescape(runtime.client.sent[-1].text))


async def test_she_answers_in_the_chat_language(runtime) -> None:
    await invoke(runtime, ".yura", language="fa")
    assert persona.is_own_line(_plain(runtime))


def _letters(text: str) -> tuple[int, int]:
    """(latin, persian) letters, so a kaomoji's stray ``و`` cannot fail a test."""
    latin = sum("a" <= char.lower() <= "z" for char in text)
    persian = sum("\u0600" <= char <= "\u06ff" for char in text)
    return latin, persian


async def test_the_words_typed_win_over_the_chat_default(runtime) -> None:
    """A Persian chat must not answer an English sentence in Farsi, or back."""
    await invoke(runtime, ".yura hello there", language="fa")
    english = _plain(runtime)
    latin, persian = _letters(english)
    assert latin > persian, english

    await invoke(runtime, ".yura حالت چطوره", language="en")
    farsi = _plain(runtime)
    latin, persian = _letters(farsi)
    assert persian > latin, farsi


async def test_the_card_reports_state_and_memory(runtime) -> None:
    await invoke(runtime, ".yura status")
    text = last_text(runtime)
    assert "Yura" in text
    assert "off" in text.lower()
    assert "Memories" in text


async def test_yura_switches_on_and_off(runtime) -> None:
    await invoke(runtime, ".yura on")
    assert await runtime.store.feature_enabled("persona") is True
    await invoke(runtime, ".yura off")
    assert await runtime.store.feature_enabled("persona") is False


async def test_freeform_text_goes_to_her(runtime) -> None:
    """``.yura <anything>`` is a message to her, not an argument error."""
    await invoke(runtime, ".yura are you awake")
    assert persona.is_own_line(_plain(runtime))


async def test_a_reply_from_the_model_is_sent(runtime) -> None:
    runtime.ai = StubProvider(["daddaaa you're back ╰(*´︶`*)╯"])
    await invoke(runtime, ".yura hey")
    assert "daddaaa" in last_text(runtime)


async def test_a_model_failure_falls_back_to_an_offline_line(runtime) -> None:
    class Exploding(StubProvider):
        async def converse(self, messages, **kwargs):
            from havoc.services.ai_provider import AIError

            raise AIError("nope", kind="timeout")

    runtime.ai = Exploding()
    await invoke(runtime, ".yura hey")
    assert persona.is_own_line(_plain(runtime))


async def test_the_model_reply_is_html_escaped(runtime) -> None:
    runtime.ai = StubProvider(["<b>not bold</b> & <script>"])
    await invoke(runtime, ".yura say something")
    assert "&lt;b&gt;" in last_text(runtime)
    assert "<script>" not in last_text(runtime)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
async def test_memory_remembers_lists_and_forgets(runtime) -> None:
    await invoke(runtime, ".yura remember my name is Sam")
    assert await runtime.memory.count(100) == 1

    await invoke(runtime, ".yura memory")
    assert "Sam" in last_text(runtime)

    await invoke(runtime, ".yura forget Sam")
    assert await runtime.memory.count(100) == 0


async def test_a_duplicate_fact_is_not_stored_twice(runtime) -> None:
    await invoke(runtime, ".yura remember I hate cilantro")
    await invoke(runtime, ".yura remember   i hate   cilantro")
    assert await runtime.memory.count(100) == 1
    assert "already knew" in last_text(runtime).lower()


async def test_memory_can_be_cleared(runtime) -> None:
    await invoke(runtime, ".yura remember one")
    await invoke(runtime, ".yura remember two")
    await invoke(runtime, ".yura memory clear")
    assert await runtime.memory.count(100) == 0


async def test_the_model_can_write_memory_and_files(runtime) -> None:
    runtime.ai = StubProvider(
        [
            "here you go daddaaa ╰(*´︶`*)╯\n"
            "[[remember: her birthday is 12 May]]\n"
            "[[file: notes.md]]\n- milk\n- cat food\n[[/file]]"
        ]
    )
    await invoke(runtime, ".yura make me a shopping list")
    text = last_text(runtime)
    assert "[[remember" not in text and "[[file" not in text
    memories = await runtime.memory.recall(100)
    assert any("12 May" in memory.content for memory in memories)

    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert files, "the generated file was never sent"
    path = files[-1].kwargs["file"]
    assert path.endswith("notes.md")
    with open(path, encoding="utf-8") as handle:
        assert "cat food" in handle.read()


async def test_dangerous_file_extensions_are_downgraded_to_text(runtime) -> None:
    runtime.ai = StubProvider(["ok\n[[file: evil.sh]]rm -rf /[[/file]]"])
    await invoke(runtime, ".yura write a script")
    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert files and files[-1].kwargs["file"].endswith(".txt")


async def test_history_is_trimmed(runtime) -> None:
    from havoc.services.memory import HISTORY_TURNS

    for index in range(HISTORY_TURNS + 12):
        await runtime.memory.append(100, "user", f"message {index}")
    history = await runtime.memory.history(100)
    assert len(history) == HISTORY_TURNS
    assert history[-1] == ("user", f"message {HISTORY_TURNS + 11}")


async def test_the_log_command_shows_the_conversation(runtime) -> None:
    await invoke(runtime, ".yura hello there")
    await invoke(runtime, ".yura log")
    assert "hello there" in last_text(runtime)


# ---------------------------------------------------------------------------
# The ambient path
# ---------------------------------------------------------------------------
async def test_it_replies_to_the_owners_own_message(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "i had a long day"))
    sent = [html.unescape(message.text) for message in runtime.client.sent]
    assert len(sent) == 1
    assert persona.is_own_line(sent[0])


async def test_it_sends_the_model_reply_when_one_is_configured(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider(["mrrp. tell me everything >_<"])
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "i had a long day"))
    assert "mrrp" in runtime.client.sent[-1].text


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
    await runtime.persona_replies.on_self_message(_event(runtime, persona.build_line("en", choice=0)))
    assert runtime.client.sent == []


async def test_it_ignores_commands(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, ".help"))
    assert runtime.client.sent == []


async def test_it_ignores_her_own_name_as_a_bare_word(runtime) -> None:
    """The dispatcher answers a bare ``yura``; the ambient path must not too."""
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "yura"))
    assert runtime.client.sent == []


async def test_it_respects_quiet_hours(runtime) -> None:
    await _enable(runtime)
    await runtime.store.set_setting(
        "quiet_hours", {"start": "00:00", "end": "23:59", "timezone": "UTC"}
    )
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
    await runtime.persona_replies.on_self_message(_event(runtime, "one"))
    await runtime.persona_replies.on_self_message(_event(runtime, "two"))
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


async def test_the_hourly_valve_is_a_hard_cap(runtime) -> None:
    await _enable(runtime)
    replies = runtime.persona_replies
    replies._window = [time.monotonic()] * 400
    replies._last_at = None
    runtime.client.sent.clear()
    await replies.on_self_message(_event(runtime, "good night"))
    assert runtime.client.sent == []


async def test_it_uses_the_message_language(runtime) -> None:
    await _enable(runtime)
    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "شب بخیر"))
    sent = [html.unescape(message.text) for message in runtime.client.sent]
    assert sent and persona.is_own_line(sent[0])


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


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_the_persona_is_on_by_default_but_media_is_not() -> None:
    """The companion answers in the owner's own chat without a setup step.

    Everything that could touch another chat, or send anything but a message in
    that one chat, stays off until it is switched on.
    """
    from havoc.services.storage import DEFAULT_FEATURES, FEATURE_GROUPS

    assert DEFAULT_FEATURES["persona"] is True
    assert DEFAULT_FEATURES["media"] is False
    assert set(FEATURE_GROUPS["persona"]) == {"persona", "media"}


async def test_features_command_lists_the_persona_group(runtime) -> None:
    await invoke(runtime, ".features")
    text = last_text(runtime)
    assert "persona" in text and "media" in text


@pytest.mark.parametrize("language", ["en", "fa"])
def test_the_call_hint_is_present_in_both_languages(language: str) -> None:
    assert persona.call_prompt(language).strip()


# ---------------------------------------------------------------------------
# The key, stored without publishing it
# ---------------------------------------------------------------------------
def _patch_probe(monkeypatch, outcome: tuple[bool, str]) -> None:
    """Never touch the network from a test. The outcome is scripted instead.

    Both the single-provider class and the rotating router are patched: which one
    ``runtime.ai`` is depends on whether credentials exist, and a test should not
    care.
    """
    from havoc.services.ai_provider import AIProvider
    from havoc.services.ai_router import AIRouter

    async def _probe(self, **kwargs):
        return outcome

    monkeypatch.setattr(AIProvider, "probe", _probe)
    monkeypatch.setattr(AIRouter, "probe", _probe)


@pytest.fixture
def no_network_probe(monkeypatch):
    _patch_probe(monkeypatch, (True, "ok"))
    return True


async def test_the_key_command_stores_the_key_and_deletes_the_message(
    runtime, no_network_probe
) -> None:
    runtime.client.sent.clear()
    await invoke(runtime, ".yura key sk-or-v1-testtesttest")
    stored = await runtime.store.get_setting("ai.api_key", None)
    assert stored == "sk-or-v1-testtesttest"
    assert runtime.ai.available is True
    # The message that carried it is gone, and the confirmation does not repeat it.
    assert runtime.client.deleted, "the key message was left in the chat"
    assert "sk-or-v1-testtesttest" not in last_text(runtime)


async def test_the_key_survives_a_restart(runtime, config, no_network_probe) -> None:
    await invoke(runtime, ".yura key sk-or-v1-persisted")
    from havoc.runtime import Runtime

    restarted = await Runtime.create(config, runtime.logger)
    assert restarted.ai.available is True
    # Whatever holds the credentials, the key the owner pasted is still there.
    keys = [credential.key for credential in getattr(restarted.ai, "credentials", [])]
    assert "sk-or-v1-persisted" in keys
    await restarted.db.close()


async def test_the_key_command_reports_state_without_a_value(runtime) -> None:
    await invoke(runtime, ".yura key")
    assert "off" in last_text(runtime).lower() or "no" in last_text(runtime).lower()


async def test_a_saved_key_is_actually_tried_and_the_answer_is_shown(
    runtime, monkeypatch
) -> None:
    _patch_probe(monkeypatch, (True, "ok"))
    await invoke(runtime, ".yura key sk-or-v1-probed")
    text = html.unescape(last_text(runtime))
    assert "ok" in text
    assert runtime.yura_chat.last_error == ""


async def test_a_saved_key_that_fails_is_reported_as_failing(runtime, monkeypatch) -> None:
    """The whole complaint: the card said "live" while nothing answered."""
    _patch_probe(monkeypatch, (False, "network: [SSL: CERTIFICATE_VERIFY_FAILED]"))
    await invoke(runtime, ".yura key sk-or-v1-deadkey")
    text = html.unescape(last_text(runtime))
    assert "network" in text
    assert runtime.yura_chat.last_error.startswith("network:")

    # And the state view repeats it instead of pretending everything is fine.
    await invoke(runtime, ".yura key")
    assert "network" in html.unescape(last_text(runtime))


# ---------------------------------------------------------------------------
# Files the owner actually asked for
# ---------------------------------------------------------------------------
def _pygame_program(lines: int = 300) -> str:
    """A realistic long program: three hundred lines, ending in a main guard."""
    body = [
        "import sys",
        "import pygame",
        "",
        "WIDTH, HEIGHT = 640, 480",
        "STEP = 20",
        "",
        "def main() -> None:",
        "    pygame.init()",
        "    screen = pygame.display.set_mode((WIDTH, HEIGHT))",
        "    clock = pygame.time.Clock()",
        "    snake = [(320, 240)]",
        "    direction = (STEP, 0)",
    ]
    index = 0
    while len(body) < lines - 5:
        body.append(f"    # step {index}: move the snake and keep it inside the screen")
        body.append(f"    step_{index} = (snake[0][0] + direction[0], snake[0][1] + direction[1])")
        index += 1
    while len(body) < lines - 4:
        body.append(f"    tail_{len(body)} = snake[-1]")
    body += ["    pygame.quit()", "", 'if __name__ == "__main__":', "    main()"]
    return "\n".join(body) + "\n"


async def test_a_three_hundred_line_program_arrives_whole(runtime) -> None:
    program = _pygame_program()
    assert len(program.splitlines()) == 300
    runtime.ai = StubProvider([f"sit. a snake. ╰(*´︶`*)╯\n[[file: snake.py]]{program}[[/file]]"])
    await invoke(runtime, ".yura write me a 300 line pygame snake as a .py file")

    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert files, "the program was never sent"
    path = files[-1].kwargs["file"]
    assert path.endswith("snake.py")
    with open(path, encoding="utf-8") as handle:
        delivered = handle.read()
    assert delivered.splitlines() == program.splitlines(), "the program was truncated"
    assert delivered.rstrip().endswith("main()")
    # The chat bubble stays a chat bubble: the code is in the file, not pasted.
    body = _plain(runtime)
    assert "[[file" not in body and "import pygame" not in body


async def test_asking_for_code_buys_the_model_more_tokens(runtime) -> None:
    provider = StubProvider(["here\n[[file: x.py]]print(1)[[/file]]"])
    runtime.ai = provider
    await invoke(runtime, ".yura write a python script that prints 1")
    budget = provider.calls[-1][0].__class__
    del budget
    assert provider.last_max_tokens == 4000

    plain = StubProvider(["mrrp."])
    runtime.ai = plain
    await invoke(runtime, ".yura how was your day")
    assert plain.last_max_tokens == 700, "ordinary chat must not ask for a program's budget"


async def test_a_truncated_file_block_is_salvaged_not_dropped(runtime) -> None:
    runtime.ai = StubProvider(["one sec\n[[file: half.py]]import pygame\npygame.init()\n# cut off here"])
    await invoke(runtime, ".yura give me the pygame file")
    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert files, "a truncated program was thrown away instead of sent"
    with open(files[-1].kwargs["file"], encoding="utf-8") as handle:
        assert "pygame.init()" in handle.read()
    assert "[[file" not in _plain(runtime)
