"""The ornament box: a thousand glyphs, and a rule about not reusing them.

The owner's complaint was not that she had no sparkles — it was that every
message had the *same* sparkles, and the same cute face at the end. A palette is
easy; a palette plus a memory of what was just used is the fix.
"""

from __future__ import annotations

import unicodedata

import pytest

from havoc import persona
from havoc.utils import glyphs


def test_the_box_holds_at_least_a_thousand_glyphs() -> None:
    assert glyphs.count() >= 1000


def test_every_glyph_is_a_single_character() -> None:
    for glyph in glyphs.ALL:
        assert len(glyph) == 1, repr(glyph)


def test_nothing_in_the_box_is_colour_emoji() -> None:
    """Symbols, not pictures — a green checkmark next to Persian looks like a phone."""
    for glyph in glyphs.ALL:
        assert glyph not in glyphs.EMOJI_PRESENTATION, repr(glyph)


def test_nothing_in_the_box_is_markdown_or_invisible() -> None:
    for glyph in glyphs.ALL:
        assert glyph not in glyphs.UNSAFE, repr(glyph)
        assert unicodedata.category(glyph) not in {"Cc", "Cf", "Mn", "Me"}, repr(glyph)
        assert not glyph.isspace()


def test_the_categories_have_the_shapes_a_message_needs() -> None:
    for name in ("star", "rule", "ornament", "dot", "arrow", "floral", "moon"):
        assert glyphs.GLYPHS.get(name), name


def test_a_suggestion_is_fresh() -> None:
    used = {"✦", "✧", "⋆"}
    picks = glyphs.suggest(used)
    for role, items in picks.items():
        assert items, role
        assert not set(items) & used, role


def test_collect_finds_what_she_wrote() -> None:
    found = glyphs.collect("✦ hello ❦ there 〜")
    assert "✦" in found and "❦" in found and "〜" in found
    assert "a" not in found


def test_a_repeat_is_swapped_for_a_fresh_one() -> None:
    text = "✦ baby ✦ i was waiting"
    out = glyphs.restyle(text, {"✦"})
    assert out != text
    assert "✦" not in out
    assert "baby" in out and "i was waiting" in out


def test_a_glyph_inside_a_kaomoji_is_never_touched() -> None:
    face = "╰(*´︶`*)╯ mrrp"
    assert glyphs.restyle(face, {"╰", "✧", "❦"}) == face


def test_nothing_used_means_nothing_changes() -> None:
    text = "✦ hello"
    assert glyphs.restyle(text, set()) == text


def test_the_brief_is_different_twice_in_a_row_sometimes() -> None:
    """Not a hard guarantee (it is random), but the pool must be big enough to see."""
    first = glyphs.suggest(set())
    second = glyphs.suggest(set(first["open"]))
    assert first != second


def test_the_brief_forbids_what_was_already_used() -> None:
    from havoc import persona

    block = persona.ornament_prompt("en", used={"✦", "❦"})
    assert "out of bounds" in block
    assert "✦" in block and "❦" in block
    assert "three or four" in block


def test_the_brief_speaks_persian_in_persian() -> None:
    from havoc import persona

    block = persona.ornament_prompt("fa", used={"✦"})
    assert "تایپوگرافی" in block
    assert "out of bounds" not in block


@pytest.mark.parametrize("language", ["en", "fa"])
def test_the_brief_always_offers_real_glyphs(language: str) -> None:
    from havoc import persona

    block = persona.ornament_prompt(language, used=set())
    offered = glyphs.collect(block)
    assert len(offered) >= 6, block


class _OfflineProvider:
    """A provider that is not available, so the offline bank answers."""

    available = False


async def test_an_offline_line_is_never_restyled(runtime) -> None:
    """The bank is hand-written. A swapped ornament is an edit to a poem."""

    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.ai = _OfflineProvider()  # type: ignore[assignment] - the offline path
    chat = runtime.yura_chat
    await runtime.memory.append(100, "assistant", "✦ ✧ ❦ ⊹ ⁂ everything is used")

    answer = await chat.answer("yura, say something", chat_id=100, language="en")

    assert answer.source == "offline"
    # It is still one of her bank sentences, not a re-lettered version of one.
    assert persona.sentence_of(answer.text, "en"), answer.text
