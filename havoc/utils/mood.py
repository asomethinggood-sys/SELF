"""Her body clock, expressed as a bar.

A companion that answers the same way at 04:00 and at 22:00 is a program. The
clock was already in the prompt as text; this module turns it into something the
owner can *see* — a five-cell attention bar under some messages, and the band the
system prompt uses to change register (night = lower case, no bold; morning =
headline and bullets).

Everything here is a pure function of the hour and the number of turns, so a test
can pin it and two runs at the same minute agree.
"""

from __future__ import annotations

from .typeface import mood_strip, time_band

__all__ = ["LABELS", "attention", "band_style", "greeting_for", "mood_line"]

#: What the bar is measuring, per language.
LABELS = {
    "en": {"attention": "attention", "awake": "awake", "battery": "battery"},
    "fa": {"attention": "حواس", "awake": "بیدار", "battery": "شارژ"},
}

#: Attention by hour: dozy before dawn, sharp mid-morning and late at night.
_CURVE = {
    0: 0.55, 1: 0.4, 2: 0.3, 3: 0.25, 4: 0.2, 5: 0.3,
    6: 0.45, 7: 0.6, 8: 0.75, 9: 0.9, 10: 0.95, 11: 0.9,
    12: 0.7, 13: 0.55, 14: 0.6, 15: 0.7, 16: 0.8, 17: 0.85,
    18: 0.9, 19: 0.85, 20: 0.9, 21: 0.95, 22: 0.9, 23: 0.75,
}


def attention(hour: int, replies: int = 0) -> float:
    """0..1 — how much of her is in the room.

    ``replies`` nudges it up: a conversation she has been part of all day is a
    conversation she is still leaning into. The nudge is capped so the bar never
    claims an attention the hour does not support.
    """
    level = _CURVE[int(hour) % 24]
    bonus = min(0.15, max(0, replies) * 0.01)
    return round(min(0.99, level + bonus), 2)


def mood_line(hour: int, *, language: str = "en", replies: int = 0, kind: str = "attention") -> str:
    """The bar itself, ready to be a line of a message."""
    labels = LABELS.get(language) or LABELS["en"]
    label = labels.get(kind, labels["attention"])
    return mood_strip(attention(hour, replies), label)


#: How the band changes the writing, as a line for the system prompt.
_BAND_STYLE = {
    "en": {
        "night": "It is the middle of their night. Lower case, no headlines, no bold — "
                 "short lines, softer words, and one symbol at most.",
        "morning": "It is their morning. Bright, quick, a headline and bullets are fine.",
        "afternoon": "It is their afternoon. Normal register: one bold line at most.",
        "evening": "It is their evening. Slower, a little closer, italics more than bold.",
    },
    "fa": {
        "night": "نصف‌شب اوست. حروف کوچک، بی‌تیتر، بی‌بولد — خط‌های کوتاه، حرف‌های نرم‌تر، حداکثر یک نماد.",
        "morning": "صبح اوست. سرحال و سریع؛ یک تیتر و فهرست اشکالی ندارد.",
        "afternoon": "بعدازظهر اوست. لحن معمولی: حداکثر یک خط بولد.",
        "evening": "شب اوست. آهسته‌تر و نزدیک‌تر؛ ایتالیک بیشتر از بولد.",
    },
}

#: A one-word greeting for the band, used where a clock line would be noise.
_GREETING = {
    "en": {"night": "still up", "morning": "morning", "afternoon": "afternoon", "evening": "evening"},
    "fa": {"night": "هنوز بیدار", "morning": "صبح", "afternoon": "بعدازظهر", "evening": "شب"},
}


def band_style(hour: int, language: str = "en") -> str:
    """How this hour should sound, as one instruction line."""
    styles = _BAND_STYLE.get(language) or _BAND_STYLE["en"]
    return styles[time_band(hour)]


def greeting_for(hour: int, language: str = "en") -> str:
    words = _GREETING.get(language) or _GREETING["en"]
    return words[time_band(hour)]
