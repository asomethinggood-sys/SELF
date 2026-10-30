"""Typography: the shape of her voice.

Small caps, spaced letters, the attention bar, the day bands. Pure functions, so
the tests pin exact strings rather than ranges — a bar that drifts is a bar the
owner stops trusting.
"""

from __future__ import annotations

import pytest

from havoc.utils import typeface
from havoc.utils.mood import LABELS, attention, band_style, greeting_for, mood_line
from havoc.utils.typeface import BARS, rule, small_caps, spaced, time_band


def test_small_caps_turns_latin_into_small_capitals() -> None:
    assert small_caps("noted") == "ɴᴏᴛᴇᴅ"
    assert small_caps("Noted Twice") == "ɴᴏᴛᴇᴅ ᴛᴡɪᴄᴇ"


def test_small_caps_leaves_persian_alone() -> None:
    assert small_caps("یورا") == "یورا"


def test_small_caps_gives_up_on_a_whole_message() -> None:
    """A paragraph in small caps is unreadable; only a head is transformed."""
    long_text = "a" * 200
    out = small_caps(long_text)
    assert out.startswith("ᴀ")
    assert out.endswith("a")


def test_spaced_draws_out_a_word() -> None:
    assert spaced("quiet") == "q u i e t"
    assert spaced("a b") == "a b"


def test_spaced_refuses_a_single_letter() -> None:
    assert spaced("x") == "x"


def test_the_rule_is_bounded() -> None:
    assert rule() == "┄" * 9
    assert len(rule(1)) == 3
    assert len(rule(999)) == 24


def test_the_bar_fills_in_proportion() -> None:
    strip = mood_line(9, kind="attention")
    assert strip == f"{BARS[1] * 4}{BARS[0]} attention · 90%"


def test_the_smallest_bar_is_still_one_cell() -> None:
    assert mood_line(4) == "▰▱▱▱▱ attention · 20%"


def test_the_bar_can_be_read_in_persian() -> None:
    assert LABELS["fa"]["attention"] in mood_line(9, language="fa")


def test_the_hour_decides_how_much_of_her_is_there() -> None:
    assert attention(3) < attention(10)
    assert attention(14) < attention(21)


def test_more_turns_lift_the_bar_but_never_past_the_hour() -> None:
    assert attention(9, replies=5) >= attention(9)
    assert attention(9, replies=10_000) <= 0.99


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(0, "night"), (4, "night"), (5, "morning"), (11, "morning"),
     (12, "afternoon"), (17, "afternoon"), (18, "evening"), (23, "night")],
)
def test_the_band_boundaries(hour: int, expected: str) -> None:
    assert time_band(hour) == expected


def test_the_band_tells_the_prompt_how_to_sound() -> None:
    assert "Lower case" in band_style(3)
    assert "headline" in band_style(9)
    assert "آهسته" in band_style(20, "fa")


def test_the_greeting_follows_the_clock() -> None:
    assert greeting_for(3) == "still up"
    assert greeting_for(9) == "morning"
    assert greeting_for(9, "fa") == "صبح"


def test_an_impossible_hour_is_normalised() -> None:
    assert time_band(24) == "night"
    assert time_band(-1) == "night"


def test_the_module_exports_what_it_claims() -> None:
    for name in typeface.__all__:
        assert hasattr(typeface, name)
