"""`.translate` asks the model she already talks to — not a service nobody set up.

The command used to answer "you have no API" on a bot with five keys, because
translation was a separate provider that defaulted to *off*. These tests pin the
new contract: without a configured provider the AI engine does the work, it is
asked for Gemma 31B first, and its answer arrives as a translation rather than as
a model's opinion about translating.
"""

from __future__ import annotations

import pytest

from havoc.config import Config
from havoc.services import translation_provider as tp
from havoc.services.ai_provider import AIError, AIMessage
from havoc.services.translation_provider import (
    TRANSLATE_MODEL,
    AITranslationProvider,
    DisabledTranslationProvider,
    TranslationError,
    build_translation_provider,
)


class RoutedEngine:
    """Duck-types the router: remembers what it was asked and answers."""

    def __init__(self, answer: str = "سلام، حال شما چطور است؟", *, available: bool = True) -> None:
        self.answer = answer
        self.available = available
        self.threads: list[list[AIMessage]] = []
        self.only: list[str | None] = []
        self.budgets: list[int] = []

    async def converse(self, messages, *, max_tokens=400, temperature=0.85, only=None):
        self.threads.append(list(messages))
        self.only.append(only)
        self.budgets.append(max_tokens)
        return self.answer


class OldEngine:
    """An engine that predates the pin: no ``only`` keyword."""

    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    async def converse(self, messages, *, max_tokens=400, temperature=0.85):
        self.calls += 1
        return "hello there"


class BrokenEngine:
    available = True

    async def converse(self, messages, **kwargs):
        raise AIError("quota is gone", kind="rate_limited")


def test_the_default_provider_is_the_ai_engine() -> None:
    engine = RoutedEngine()
    translator = build_translation_provider(Config(), engine)
    assert isinstance(translator, AITranslationProvider)
    assert translator.available is True


def test_gemma_31b_is_the_model_it_names() -> None:
    translator = build_translation_provider(Config(), RoutedEngine())
    assert isinstance(translator, AITranslationProvider)
    assert translator.model == TRANSLATE_MODEL == "gemma-4-31b-it"
    assert translator.describe()["model"] == "gemma-4-31b-it"


def test_without_an_engine_it_is_honestly_off() -> None:
    assert isinstance(build_translation_provider(Config(), None), DisabledTranslationProvider)


def test_a_chosen_provider_still_wins() -> None:
    """Somebody who configured LibreTranslate should not be overruled."""
    config = Config(translate_provider="libretranslate", libretranslate_url="http://localhost:5000")
    translator = build_translation_provider(config, RoutedEngine())
    assert translator.name == "libretranslate"
    assert build_translation_provider(Config(translate_provider="mymemory")).name == "mymemory"


async def test_the_translation_is_asked_of_gemma() -> None:
    engine = RoutedEngine("good morning")
    translator = AITranslationProvider(engine)
    result = await translator.translate("صبح بخیر", "en")

    assert result.text == "good morning"
    assert result.provider == "gemma-4-31b-it"
    assert result.target == "en"
    assert engine.only == ["gemma-4-31b-it"], "the pin is what makes it *gemma*"
    system, user = engine.threads[0]
    assert system.role == "system" and "only the translation" in system.content
    assert "English" in user.content
    assert "صبح بخیر" in user.content


async def test_a_named_source_language_reaches_the_prompt() -> None:
    engine = RoutedEngine("привет")
    translator = AITranslationProvider(engine)
    await translator.translate("hello", "ru", "en")
    assert "from English to Russian" in engine.threads[0][1].content


async def test_the_budget_grows_with_the_text() -> None:
    engine = RoutedEngine("x")
    translator = AITranslationProvider(engine)
    await translator.translate("a" * 20, "en")
    await translator.translate("a" * 900, "en")
    assert engine.budgets[0] < engine.budgets[1]
    assert engine.budgets[1] <= 1600


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("```\nسلام\n```", "سلام"),
        ("Translation: سلام", "سلام"),
        ('"سلام"', "سلام"),
        ("«سلام»", "سلام"),
    ],
)
async def test_the_model_is_not_allowed_to_wrap_it(raw: str, clean: str) -> None:
    translator = AITranslationProvider(RoutedEngine(raw))
    assert (await translator.translate("hello", "fa")).text == clean


async def test_an_engine_without_the_pin_still_translates() -> None:
    engine = OldEngine()
    translator = AITranslationProvider(engine)
    assert (await translator.translate("سلام", "en")).text == "hello there"
    assert engine.calls == 1


async def test_a_spent_key_becomes_a_kind_not_a_crash() -> None:
    translator = AITranslationProvider(BrokenEngine())
    with pytest.raises(TranslationError) as caught:
        await translator.translate("hello", "fa")
    assert caught.value.kind == "rate_limited"


async def test_an_empty_answer_is_a_failure_not_a_blank_message() -> None:
    translator = AITranslationProvider(RoutedEngine("   "))
    with pytest.raises(TranslationError) as caught:
        await translator.translate("hello", "fa")
    assert caught.value.kind == "bad_response"


async def test_nothing_to_translate_is_refused_before_a_request() -> None:
    engine = RoutedEngine()
    translator = AITranslationProvider(engine)
    with pytest.raises(TranslationError) as caught:
        await translator.translate("   ", "fa")
    assert caught.value.kind == "empty_text"
    assert engine.threads == []


def test_config_accepts_ai_as_a_name_for_it() -> None:
    from havoc.config import validate

    config = Config(translate_provider="ai")
    assert [issue for issue in validate(config) if issue.field == "TRANSLATE_PROVIDER"] == []


def test_a_key_alone_makes_translation_available() -> None:
    """The command's own gate reads this property, so it has to agree."""
    assert Config(translate_provider="none", ai_provider="openai_compatible", ai_api_key="k").translation_available
    assert Config().translation_available is False


def test_the_names_a_model_understands_are_words() -> None:
    assert tp.LANG_NAMES["fa"].startswith("Persian")
    assert tp.LANG_NAMES["ja"] == "Japanese"
    assert tp.LANG_NAMES["ru"] == "Russian"


async def test_the_command_answers_with_a_translation(runtime) -> None:
    """End to end: the command a person types, with the engine swapped."""
    from conftest import invoke, sent_text

    runtime.translator = build_translation_provider(Config(), RoutedEngine("good morning"))
    await invoke(runtime, ".translate en صبح بخیر")
    said = sent_text(runtime)
    assert "good morning" in said
    assert "gemma-4-31b-it" in said, said
