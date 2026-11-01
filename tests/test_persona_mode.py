"""Two voices, one creature: the switch has to actually change how she answers.

The owner's words: "right now it's not a very helpful assistant because of how it
responds". A setting that is stored but does not reach the prompt is a setting
that changed nothing, so every test here checks the *instruction the model gets*
or the *words that come out*, never the presence of a key in a table.
"""

from __future__ import annotations

import html

from conftest import invoke, last_text, sent_text

from havoc import persona
from havoc.event_engine import EventType, normalize_message


class Recorder:
    """A provider that keeps the messages it was given."""

    model = "stub-model"
    available = True
    last_max_tokens = 0

    def __init__(self, reply: str = "here is the answer.") -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        self.last_max_tokens = max_tokens
        return self.reply

    @property
    def system(self) -> str:
        assert self.calls, "the model was never asked"
        return str(getattr(self.calls[-1][0], "content", self.calls[-1][0]))


def test_the_default_voice_is_the_one_she_already_had() -> None:
    assert persona.DEFAULT_MODE == "egirl"
    assert set(persona.MODES) == {"egirl", "assistant"}
    assert persona.normalize_mode("ASSISTANT") == "assistant"
    assert persona.normalize_mode("دستیار") == "assistant"
    assert persona.normalize_mode("nonsense") == ""


def test_the_two_prompts_are_actually_different_instructions() -> None:
    egirl = persona.system_prompt("en", hour=2)
    assistant = persona.system_prompt("en", hour=2, mode="assistant")

    assert egirl != assistant
    # The egirl voice is the one with pet names and kaomoji; the assistant one
    # must not be able to write them by accident.
    assert '- Those names are for him only: "daddaaa"' in egirl
    assert "- Those names are for him only" not in assistant
    assert "e-kitten" in egirl
    assert "assistant mode" in assistant
    assert "no pet names" in assistant.lower()
    # The night register is an egirl flourish: an assistant that goes lowercase
    # at midnight is a costume, not a personality.
    assert "lowercase" in egirl.lower()
    # ...while the parts that must survive every mode do survive.
    for shared in ("The hands you have", "Never insult", "Never claim to have done"):
        assert shared in assistant, shared


def test_the_assistant_never_calls_a_guest_daddy() -> None:
    assistant = persona.system_prompt("en", is_owner=False, mode="assistant")
    assert "this turn is not his" in assistant
    # The rule *names* the pet words in order to forbid them; the instruction
    # that tells her to use them is what must not be there.
    assert "- Those names are for him only" not in assistant


async def test_the_command_switches_the_voice_and_remembers_it(runtime) -> None:
    await invoke(runtime, ".yura mode assistant")
    assert await runtime.store.get_setting(persona.MODE_SETTING, "") == "assistant"
    assert await persona.current_mode(runtime.store) == "assistant"
    text = html.unescape(sent_text(runtime))
    assert "assistant" in text

    await invoke(runtime, ".yura mode egirl")
    assert await persona.current_mode(runtime.store) == "egirl"

    # The short form is the one he will actually type.
    await invoke(runtime, ".yura assistant")
    assert await persona.current_mode(runtime.store) == "assistant"


async def test_the_card_shows_which_voice_is_on(runtime) -> None:
    await persona.set_mode(runtime.store, "assistant")
    await invoke(runtime, ".yura status")
    text = html.unescape(last_text(runtime))
    assert "voice" in text.lower()
    assert "assistant" in text


async def test_a_nonsense_mode_is_refused_and_changes_nothing(runtime) -> None:
    await persona.set_mode(runtime.store, "egirl")
    await invoke(runtime, ".yura mode banana")
    assert await persona.current_mode(runtime.store) == "egirl"
    text = html.unescape(last_text(runtime))
    assert "egirl" in text and "assistant" in text


async def test_the_voice_reaches_the_model(runtime) -> None:
    """The point of the whole switch: the question is answered differently."""
    from conftest import FakeMessage

    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.permissions.set_self_id(42)
    provider = Recorder("fine.")
    runtime.ai = provider

    fake = FakeMessage(
        id=901, chat_id=42, sender_id=42, text="how do i center a div", out=True, is_private=True
    )
    event = normalize_message(fake, self_id=42, prefix=".", event_type=EventType.OUTGOING)

    await persona.set_mode(runtime.store, "assistant")
    await runtime.persona_replies.on_self_message(event)
    assert provider.calls, f"she did not answer in assistant mode: {dict(runtime.persona_replies.skips)}"
    assert "assistant mode" in provider.system
    assert 'a little feral for him' not in provider.system

    provider.calls.clear()
    # The two-second valve between unsolicited answers is not what this test is
    # about; the voice is.
    runtime.persona_replies._last_at = None
    await persona.set_mode(runtime.store, "egirl")
    await runtime.persona_replies.on_self_message(event)
    assert provider.calls, "she did not answer in egirl mode"
    assert "e-kitten" in provider.system
    assert "assistant mode" not in provider.system


def test_the_offline_fallback_keeps_the_voice_too() -> None:
    """A model that is down must not turn the assistant back into a kitten."""
    plain = persona.assistant_line("en")
    # Every line says *something* about why it is short; none of them pretend to
    # be the answer the model would have given.
    assert plain and len(plain) > 15
    assert not any(glyph in plain for glyph in ("ᵕ", "◕", "╰", "♡", ">_<"))
    assert persona.assistant_line("fa")
    # The bank is asked to avoid what was already said.
    avoid = {persona.assistant_line("en", choice=0)}
    assert persona.assistant_line("en", choice=2, avoid=avoid) != next(iter(avoid))
