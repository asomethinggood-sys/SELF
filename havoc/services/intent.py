"""What he asked for, read as an instruction rather than as conversation.

The hands in :mod:`havoc.services.actions` are only used when the model writes a
``tool`` block, and a free model will sometimes just… answer. The owner's report
after 1.6 was exactly that: "turn off my live clock" got a paragraph instead of a
clock being turned off. Depending on a model's mood for whether the account does
anything is not a design, it is a coin flip.

So this module reads the sentence itself. It is deliberately *narrow and loud*: a
verb that means "do this" plus a thing this account actually knows how to do. If
either half is missing it returns ``None`` and the turn is pure conversation,
exactly as before. What it produces is a line for the console she already has
(``clock off``), so there is no second implementation of anything — one dispatcher,
one set of replies, one audit trail.

English and Persian, because he speaks both to her.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from ..utils.text import emoji_in

#: Verbs that mean "change the state of the account".
OFF_WORDS = (
    "turn off",
    "switch off",
    "shut off",
    "shut down",
    "disable",
    "deactivate",
    "stop",
    "kill",
    "pause",
    "mute",
    "خاموش کن",
    "خاموشش کن",
    "غیرفعال کن",
    "غیر فعال کن",
    "ببند",
    "قطع کن",
    "متوقف کن",
    "خفه کن",
)
ON_WORDS = (
    "turn on",
    "switch on",
    "enable",
    "activate",
    "start",
    "resume",
    "unmute",
    "روشن کن",
    "روشنش کن",
    "فعال کن",
    "باز کن",
    "شروع کن",
    "ادامه بده",
)

#: ``turn off <thing>`` / ``<thing> off`` / ``<thing> رو خاموش کن``.
_OFF_LEAD = re.compile(
    r"(?:turn off|switch off|shut off|disable|deactivate|stop|pause|kill|mute)"
    r"\s+(?:the\s+|my\s+|your\s+)?(?P<thing>[\w\s\u0600-\u06FF\-]{2,40}?)\s*(?:please|now|for me)?\s*[.!?]*$",
    re.IGNORECASE,
)
_ON_LEAD = re.compile(
    r"(?:turn on|switch on|enable|activate|start|resume|unmute)"
    r"\s+(?:the\s+|my\s+|your\s+)?(?P<thing>[\w\s\u0600-\u06FF\-]{2,40}?)\s*(?:please|now|for me)?\s*[.!?]*$",
    re.IGNORECASE,
)
_OFF_TAIL = re.compile(
    r"(?P<thing>[\w\s\u0600-\u06FF\-]{2,40}?)\s+(?:رو\s+)?(?:خاموش کن|غیرفعال کن|غیر فعال کن|ببند|قطع کن|متوقف کن)\s*[.!؟]*$"
)
_ON_TAIL = re.compile(
    r"(?P<thing>[\w\s\u0600-\u06FF\-]{2,40}?)\s+(?:رو\s+)?(?:روشن کن|فعال کن|باز کن|شروع کن)\s*[.!؟]*$"
)
#: ``clock off`` — the shortest form, and the one people actually type.
_SHORT_OFF = re.compile(r"^(?P<thing>[\w\s\u0600-\u06FF\-]{2,40}?)\s+(?:off|disabled?|بسته)$", re.IGNORECASE)
_SHORT_ON = re.compile(r"^(?P<thing>[\w\s\u0600-\u06FF\-]{2,40}?)\s+(?:on|enabled?|روشن)$", re.IGNORECASE)

#: Words that mean nothing on their own and must not be read as a target.
_NOISE = {
    "it",
    "that",
    "this",
    "them",
    "everything",
    "anything",
    "something",
    "yourself",
    "herself",
    "me",
    "now",
    "please",
    "the",
    "a",
    "an",
    "همه",
    "همه‌چیز",
    "این",
    "اون",
    "آن",
}

#: How a person names the thing, mapped to what the account calls it.
ALIASES: dict[str, str] = {
    "live clock": "clock",
    "profile clock": "clock",
    "body clock": "clock",
    "clock": "clock",
    "ساعت": "clock",
    "ساعت زنده": "clock",
    "live profile clock": "clock",
    "poetry": "poetry",
    "poems": "poetry",
    "poem mode": "poetry",
    "شعر": "poetry",
    "فال": "fal",
    "fal": "fal",
    "hafez": "fal",
    "roasts": "roasts",
    "roasting": "roasts",
    "شوخی": "roasts",
    "persona": "persona",
    "yura": "persona",
    "یورا": "persona",
    "autoreply": "autoreply",
    "auto reply": "autoreply",
    "away mode": "away",
    "away": "away",
    "backups": "backups",
    "backup": "backups",
    "بکاپ": "backups",
    "summaries": "summaries",
    "summarize": "summaries",
    "triggers": "triggers",
    "watches": "watches",
    "reactions": "reactions",
    "notifications": "notifications",
    "reminder intent": "reminder_intent",
    "profile scheduler": "profile_scheduler",
    "bio rotation": "bio_rotation",
    "auto translate": "auto_translate",
    "auto translation": "auto_translate",
    "translate automatically": "auto_translate",
    "فیگ": "fight",
    "fight": "fight",
    "fighting": "fight",
    "جنگ": "fight",
    "joke": "joke",
    "jokes": "joke",
    "quote": "quote",
    "quotes": "quote",
    "coin": "coin",
    "random": "random",
}

#: Duration for a reminder: "in one hour", "for 30 minutes", "1h20m", "یک ساعت".
_UNITS = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "ثانیه": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "دقیقه": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
    "ساعت": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "روز": 86400,
}
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_WORD_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "sixty": 60,
    "half": 0.5,
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
    "پانزده": 15,
    "بیست": 20,
    "سی": 30,
    "چهل": 40,
    "پنجاه": 50,
    "نیم": 0.5,
}


@dataclass(frozen=True)
class Intent:
    """One instruction, already expressed as console lines."""

    lines: tuple[str, ...]
    label: str
    #: What the card calls it, e.g. ``clock off``.
    detail: str = ""
    #: Filled in by :func:`detect` for diagnostics.
    source: str = "rules"
    extras: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return bool(self.lines)


def _clean(text: str) -> str:
    folded = (text or "").translate(_PERSIAN_DIGITS)
    folded = folded.replace("\u200c", " ").replace("‌", " ")
    folded = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", folded)
    return " ".join(folded.strip().split()).lower()


def _target(raw: str, known: set[str]) -> str | None:
    """The account's own name for the thing he named, or ``None``."""
    text = _clean(raw).strip(" .,!?:;")
    if not text or text in _NOISE:
        return None
    if text in ALIASES:
        return ALIASES[text]
    if text in known:
        return text
    # "the fal thing" / "my poetry mode" — longest known name wins.
    for name in sorted(known, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text):
            return name
    for alias, name in sorted(ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text):
            return name
    return None


#: How people join two things they want done in one breath.
_JOINERS = re.compile(r"\s*(?:,|،|\band\b|\bplus\b|\u0648)\s*", re.IGNORECASE)


def _switch_list(raw: str, *, on: bool, known: set[str], features: set[str]) -> Intent | None:
    """``turn off fal and poetry`` → both, in one intent.

    A switch phrase names one *or several* things. The first version read only the
    first name, so "turn off fal and poetry" quietly did half of what he asked and
    looked like it had worked.
    """
    parts = [part for part in _JOINERS.split(raw.strip()) if part.strip()]
    if not parts:
        return None
    lines: list[str] = []
    seen: list[str] = []
    labels: list[str] = []
    for part in parts:
        thing = _target(part, known)
        if thing is None or thing in seen:
            continue
        seen.append(thing)
        labels.append(f"{thing} {'on' if on else 'off'}")
        if thing == "clock":
            # The clock has its own on/off, and switching the *feature* off would
            # hide the command instead of putting the clock away.
            lines.append(f"clock {'on' if on else 'off'}")
        elif thing in features:
            lines.append(f"feature {thing} {'on' if on else 'off'}")
        else:
            # A command with no feature behind it: the account can still stop
            # serving it, and `.disable` is the switch for exactly that.
            lines.append(f"{'enable' if on else 'disable'} {thing}")
    if not lines:
        return None
    return Intent(
        lines=tuple(lines),
        label="feature" if any(line.startswith("feature") for line in lines) else "command",
        detail=" · ".join(labels),
    )


def _duration(text: str) -> tuple[int | None, str]:
    """``(seconds, what is left of the sentence)`` for a reminder-ish request."""
    folded = _clean(text)
    total = 0
    found = False
    # 1h30m / 90m / 2 hours / "one hour"
    for match in re.finditer(
        r"(?<![\w.])(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|ثانیه|دقیقه|ساعت|روز)\b",
        folded,
    ):
        amount = float(match.group(1))
        unit = match.group(2)
        factor = _UNITS.get(unit) or _UNITS.get(unit.rstrip("s")) or 0
        if factor:
            total += int(amount * factor)
            found = True
    if not found:
        for match in re.finditer(
            r"(?<![\w.])(a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|forty-five|sixty|half|یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده|پانزده|بیست|سی|چهل|پنجاه|نیم)\s*"
            r"(seconds?|minutes?|hours?|days?|ثانیه|دقیقه|ساعت|روز)",
            folded,
        ):
            amount = _WORD_NUMBERS.get(match.group(1), 0)
            factor = _UNITS.get(match.group(2)) or _UNITS.get(match.group(2).rstrip("s")) or 0
            if factor:
                total += int(amount * factor)
                found = True
    if not found:
        return None, folded
    # A bare number with no unit is minutes, which is how people say "remind me 20".
    if not found:
        return None, folded
    return (total or None), folded


def _reminder(text: str) -> Intent | None:
    folded = _clean(text)
    trigger = re.search(
        r"\b(remind me|reminder|set a reminder|set a timer|timer|wake me|alert me|یادآور|یادم بنداز|تایمر|یادآوری)\b",
        folded,
    )
    if not trigger:
        return None
    seconds, _ = _duration(folded)
    if not seconds:
        # "یادم بنداز ساعت ۵ شیر بخرم" names a clock time, not a countdown. The
        # command's own parser knows `HH:MM`; all that is missing is the shape.
        clock = _FA_CLOCK_TIME.search(folded)
        if clock:
            # Only punctuation goes — not letters. `.strip("هکر")` ate the last
            # letter of "جلسه", which is how a reminder about a meeting became a
            # reminder about "جلس".
            what = folded[clock.end() :].strip(" .,!؟?؛:،")
            said_fa = bool(re.search(r"[\u0600-\u06FF]", folded))
            what = re.sub(r"^(?:که|را|رو)\s+", "", what).strip() or (
                "یادآوری" if said_fa else "reminder"
            )
            # "فردا ساعت ۹ بهم یادآوری کن" is the *order*, not the subject: the
            # reminder is about nothing he named, so it is a plain alarm.
            what = re.sub(
                r"\s*(?:به\s*م|به\s*من|برای\s*من)?\s*(?:یادآوری|یادم\s*بنداز|یاد\s*بیار)"
                r"\s*(?:کن|بکن|بده)?\s*$",
                "",
                what,
            ).strip(" .,!؟?؛:،") or ("یادآوری" if said_fa else "reminder")
            hour = int(clock.group("hour"))
            minute = int(clock.group("minute") or 0)
            if clock.group("evening") and hour < 12:
                hour += 12
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            # "فردا ساعت ۹" is tomorrow's nine, and saying it as a bare clock
            # time lets the command roll it to tomorrow by accident rather than
            # because he said so.
            day = "tomorrow " if re.search(r"\bفردا\b", folded) else ""
            stamp = f"{day}{hour:02d}:{minute:02d}"
            return Intent(
                lines=(f"remind {stamp} {what}",),
                label="remind",
                detail=f"remind · {stamp}",
            )
        return None
    # What to be reminded about: everything after the trigger, with the timing
    # phrase taken out. "remind me to drink water in 35 minutes" is about water;
    # "یادم بنداز تا یک ساعت دیگه آب بخورم" is about آب, not about the hour.
    what = folded[trigger.end() :]
    what = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|ثانیه|دقیقه|ساعت|روز)\b",
        " ",
        what,
    )
    what = re.sub(
        r"\b(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty|forty-five|sixty|half|"
        r"یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده|پانزده|بیست|سی|چهل|پنجاه|نیم)\s*"
        r"(?:seconds?|minutes?|hours?|days?|ثانیه|دقیقه|ساعت|روز)\b",
        " ",
        what,
    )
    what = re.sub(
        r"\b(?:in|within|after|from now|about|roughly|other|more|"
        r"تا|بعد از|دیگر|دیگه|همین)\b",
        " ",
        what,
    )
    what = re.sub(r"\s+", " ", what).strip(" .,!?؟:;-")
    what = re.sub(r"^(?:to|that|for|and|که|تا|و)\s+", "", what).strip()
    if not what:
        what = "timer"
    seconds = max(30, min(seconds, 366 * 86400))
    when = _human_wait(seconds)
    return Intent(
        lines=(f"remind {when} {what}",),
        label="remind",
        detail=f"{when} · {what}",
    )


def _human_wait(seconds: int) -> str:
    if seconds % 86400 == 0 and seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


#: How a person names a language, to the code a translator wants.
LANGUAGES = {
    "persian": "fa", "farsi": "fa", "پارسی": "fa", "فارسی": "fa", "fa": "fa",
    "english": "en", "انگلیسی": "en", "en": "en",
    "german": "de", "deutsch": "de", "آلمانی": "de", "de": "de",
    "arabic": "ar", "عربی": "ar", "ar": "ar",
    "turkish": "tr", "ترکی": "tr", "tr": "tr",
    "russian": "ru", "روسی": "ru", "ru": "ru",
    "french": "fr", "فرانسوی": "fr", "fr": "fr",
    "spanish": "es", "اسپانیایی": "es", "es": "es",
    "japanese": "ja", "ژاپنی": "ja", "ja": "ja",
    "chinese": "zh", "چینی": "zh", "zh": "zh",
    "italian": "it", "ایتالیایی": "it", "it": "it",
}

_REMEMBER = re.compile(
    r"^(?:please\s+)?(?:remember|note|keep in mind|save this|save note|don'?t forget)"
    r"(?:\s+that)?[:\s]+(?P<body>.{2,300})$",
    re.IGNORECASE,
)
_REMEMBER_FA = re.compile(
    r"^(?:یادداشت کن|یادت باشه|به خاطر بسپار|یادم باشه)[:\s]*(?P<body>.{2,300})$"
)
_SAVE_TO_SAVED = re.compile(
    r"^(?:save|keep|copy|forward|put|stash)\s+(?:this|that|it)\s+"
    r"(?:to|in|into)\s+(?:my\s+|our\s+)?saved(?:\s+messages)?\s*[.!]*$"
    r"|^(?:این|اون|اینو|اونو)\s*(?:رو|را)?\s*(?:توی|به|در)\s*"
    r"(?:پیام\s*های\s*ذخیره\s*شده|ذخیره\s*ها|سیو\s*ها|saved)\s*(?:بفرست|بذار|ذخیره\s*کن)$",
    re.I,
)
_TRANSLATE = re.compile(
    r"^(?:please\s+)?translate\s+(?P<body>.{2,300}?)\s+(?:to|into|به)\s+(?P<lang>[\w\u0600-\u06FF]+)\s*[.!?]*$",
    re.IGNORECASE,
)
_TRANSLATE_FA = re.compile(
    r"^(?P<body>.{2,300}?)\s+(?:رو|را)?\s*(?:به|بە)\s+(?P<lang>[\w\u0600-\u06FF]+)\s+ترجمه کن\s*[.!؟]*$"
)
_SEARCH = re.compile(
    r"^(?:please\s+)?(?:search|look for)\s+(?:for\s+)?(?P<body>.{2,200}?)(?:\s+in (?:this|the) chat)?\s*[.!?]*$",
    re.IGNORECASE,
)


def _knowledge(text: str) -> Intent | None:
    """Remembering, translating and searching: three named shapes, nothing guessed."""
    folded = _clean(text)
    if _SAVE_TO_SAVED.match(folded):
        # "save this to saved messages" used to become a *note*: the word "save"
        # at the front of `_REMEMBER` ate it. What he asked for is a re-send.
        return Intent(lines=("copy saved",), label="copy", detail="copy · saved messages")
    for pattern in (_REMEMBER, _REMEMBER_FA):
        match = pattern.match(folded)
        if match:
            body = match.group("body").strip(" .,!?؟:;-")
            if body and body not in _NOISE:
                return Intent(lines=(f"note add {body}",), label="note", detail=f"note · {body[:40]}")
    for pattern in (_TRANSLATE, _TRANSLATE_FA):
        match = pattern.match(folded)
        if match:
            language = LANGUAGES.get(match.group("lang").strip().lower())
            body = match.group("body").strip(" .,!?؟:;-")
            if language and body:
                return Intent(
                    lines=(f"translate {language} {body}",),
                    label="translate",
                    detail=f"translate → {language}",
                )
    match = _SEARCH.match(folded)
    if match:
        body = match.group("body").strip(" .,!?؟:;-")
        if body and len(body) > 2 and body not in _NOISE:
            return Intent(lines=(f"search {body}",), label="search", detail=f"search · {body[:40]}")
    return None


#: Number words a person types into a sum, mapped to what arithmetic wants.
_MATH_WORDS = (
    (r"\bplus\b|\band\b|\badded to\b", "+"),
    (r"\bminus\b|\bless\b|\btake away\b", "-"),
    (r"\btimes\b|\bmultiplied by\b|\bx\b|\binto\b", "*"),
    (r"\bdivided by\b|\bover\b", "/"),
    (r"\bmod\b|\bmodulo\b", "%"),
    (r"\bto the power of\b|\braised to\b", "**"),
)

#: ``what time is it (in Tokyo)`` — the place is optional and stripped of manners.
_ASK_TIME = re.compile(
    r"^(?:"
    r"(?:what time is it|what time is it now) in (?P<place_a>[^?]+)"
    r"|what(?:'s| is) the time in (?P<place_b>[^?]+)"
    r"|time in (?P<place_c>[^?]+)"
    r"|what time is it|what(?:'s| is) the time|current time|the time please"
    r"|ساعت چنده|ساعت چند است|ساعت چند شده|چه ساعتیه|ساعت چقدره"
    r")\s*(?:now|please|لطفا)?\s*[?.!؟]*$"
)
_ASK_DATE = re.compile(
    r"^(?:what(?:'s| is) (?:the |today'?s )?date|what day is (?:it|today)|today'?s date"
    r"|what is today|تاریخ امروز|امروز چندمه)\s*[?.!؟]*$"
)
_COIN = re.compile(
    r"^(?:(?:please )?(?:flip|toss) (?:a|the) coin(?: for me)?|heads or tails|شیر یا خط)"
    r"\s*[?.!؟]*$"
)
_RANDOM_NUMBER = re.compile(
    r"^(?:(?:give me|pick|choose|generate)\s+)?(?:a\s+)?random\s+(?:number|integer)"
    r"(?:\s+(?:between\s+)?(?P<low>-?\d+)\s*(?:and|to|-)\s*(?P<high>-?\d+))?\s*[?.!]*$"
)
_SUM = re.compile(
    r"^(?:what(?:'s| is)|calculate|compute|how much is|solve|work out|محاسبه)\s+"
    r"(?P<expr>[0-9\s.,+\-*/%^()×÷]+?)\s*[=?]*$"
)
_BACKUP = re.compile(
    r"^(?:(?:make|create|take|run|do)(?: me)?(?: a| an)? backup"
    r"|back(?:ing)? up(?: (?:the |my )?(?:database|data|everything|it))?"
    r"|back (?:the database|everything|it) up"
    r"|(?:پشتیبان|بکاپ) (?:بگیر|بساز))\s*[.!]*$"
)
_SELFCHECK = re.compile(
    r"^(?:(?:run |do )?(?:a )?self[\s-]?check|check yourself|are you (?:ok|okay|healthy|alive)\b"
    r"|خودت رو چک کن)\s*[?.!]*$"
)
#: "are you there" is talking to her; "ping" is asking the round trip.
_PING = re.compile(r"^(?:ping|how fast are you)\s*[?.!]*$")
_LANG_OF_TEXT = re.compile(
    r"^(?:what language is (?:this|that|it)|which language is (?:this|that)|detect (?:the |its )?language"
    r"|این چه زبانیه)\s*[?.!؟]*$"
)
_POEM_ASK = re.compile(
    r"^(?:(?:tell|read|recite|give)(?: me)?(?: a)?(?: short)?(?: (?:hafez|rumi|moulana|saadi|khayyam|فال)?)? poem"
    r"|(?:tell|read|recite)(?: me)? (?:a )?(?:hafez|rumi|saadi|khayyam) poem|a poem|poem please"
    r"|(?:یک )?شعر(?: بگو| بخون)?)$"
)
_AGENDA_WEEK = re.compile(
    r"^(?:what(?:'s| is) on (?:my|the) agenda (?:this|next) week|my week|what do i have this week)\s*[?.!]*$"
)
_AGENDA = re.compile(
    r"^(?:what(?:'s| is) on (?:my|the) agenda|my agenda|what(?:'s| is) planned(?: for today)?"
    r"|what do i have (?:scheduled|planned|today)|what(?:'s| is) (?:on )?today)\s*[?.!]*$"
)
_BOOKMARKS_LIST = re.compile(
    r"^(?:(?:show|list)(?: me)?(?: my)? bookmarks|my bookmarks|what did i bookmark)\s*[?.!]*$"
)
_SUMMARIZE = re.compile(
    r"^(?:summar(?:y|ize|ise)|tl;?dr)(?:\s+(?:the\s+)?(?:last\s+)?(?P<count>\d+)\s+messages?)?"
    r"(?:\s+(?:this|that|here|for me))?\s*[?.!]*$"
)
_VERSION = re.compile(
    r"^(?:(?:what|which) version(?: are you| is (?:this|it))?|your version|version"
    r"|what(?:'s| is) your version|ورژن(?:ت چیه| چیه)?)\s*[?.!؟]*$"
)


#: The auto-reply family, in the shapes people actually write it. Ordered: the
#: "if anyone messages me …" form is the most specific, so it is read first.
_SCOPE_FROM_SENTENCE = re.compile(r"\b(?:private|dms?|direct\s+messages?)\b", re.I)
_AUTOREPLY_IF = re.compile(
    r"^(?:(?:set|put|create|make|add)\s+(?:up\s+)?(?:an?\s+)?)?"
    r"(?:auto[\s-]?reply|automatic\s+reply)?"
    r".*?\b(?:if|when|whenever|any\s+time)\s+(?:anyone|anybody|someone|somebody|a\s+stranger|people)\s+"
    r"(?:dms?|messages?|texts?|writes?|pings?)\s+(?:me|my)\b.*?"
    r"(?:\s+(?:then\s+)?(?:say|says|tell\s+(?:them|him|her)|reply|answer|send(?:\s+back)?|respond|write)"
    r"(?:\s+(?:with|that|:))?\s+)(?P<body>.+)$"
)
_AUTOREPLY = re.compile(
    r"^(?:(?:set|put|create|make|add|start)\s+(?:up\s+)?(?:an?\s+)?)?"
    r"(?P<kind>auto[\s-]?repl(?:y|ies)|automatic\s+repl(?:y|ies))"
    r"(?:\s+(?:to|for|on|in)\s+(?:every|each|any|all)?\s*"
    r"(?P<scope>private(?:\s+chats?)?|dms?|direct\s+messages?|everyone|all\s+chats?|groups?|groups?\s+only))?"
    r"\s*(?:that|which)?\s*(?:[:,-]\s*)?"
    r"(?:(?:saying|says|say|with|reads?)\s+)?"
    r"(?P<body>.+)$"
)
_AUTOREPLY_OFF = re.compile(
    r"^(?:stop|turn\s+off|switch\s+off|disable|kill|remove|delete|end)"
    r"\s+(?:the\s+|my\s+|all\s+)?(?:auto[\s-]?repl(?:y|ies)|automatic\s+repl(?:y|ies)|away\s+message|away\s+mode)"
    r"\s*(?:now|please)?\s*[.!]*$"
)
_REPLY_TO_ALL = re.compile(
    r"^(?:reply|respond|answer)\s+(?:to\s+)?(?:every|each|any|all)\s+"
    r"(?:single\s+)?(?:private\s+chats?|dms?|direct\s+messages?|messages?|chats?|people)\s*"
    # "…that I get", "…I receive": he is naming the inbox, not the text.
    r"(?:(?:that|which)\s+)?(?:i\s+(?:get|receive|got))?\s*"
    r"(?:with|saying|that says|:)?\s+(?P<body>.+)$",
    re.I,
)
_AWAY_ON = re.compile(
    r"^(?:tell\s+(?:everyone|them|people)\s+)?(?:that\s+)?"
    r"(?:i(?:'m|\s+am)\s+(?:away|out|not\s+here|busy|gone)|set\s+(?:me\s+)?away)"
    r"(?:\s+(?:for|until|till)\s+(?P<until>[^,;]+?))?\s*(?:[,;:]\s*(?P<body>.+))?$"
)
_AWAY_OFF = re.compile(
    r"^(?:i(?:'m|\s+am)\s+(?:back|here|home)|turn\s+off\s+away|away\s+off|stop\s+away"
    r"|stop\s+tell\w*\s+(?:them|people|everyone)\s+i(?:'m|\s+am)\s+(?:away|out))\s*[.!]*$"
)
#: Persian orders use the same verbs in a different order: "نامش رو عوض کن بذار
#: لیانا", "بیو رو عوض کن بنویس …". The verb pair is the trigger, the tail is the
#: value — never the other way round.
_FA_NAME = re.compile(
    r"^(?:اسم|نام)(?:\s+من)?(?:ش|مو|م|تو|ت)?(?:\s*(?:رو|را))?\s*"
    r"(?:رو\s*)?(?:عوض\s*کن|تغییر\s*بده|عوض|تغییر|بذار|بزار|بذارش|کن)\s*"
    r"(?:بذار|بزار|به|روی|:)?\s*(?P<name>.+)$"
)
_FA_BIO = re.compile(
    r"^(?:بیو|بیوی|بیوگرافی|بایو)(?:\s*(?:رو|را))?\s*"
    r"(?:عوض\s*کن|تغییر\s*بده|عوض|تغییر|بنویس|بذار|بزار|کن)\s*"
    r"(?:بنویس|بذار|بزار|که|:)?\s*(?P<bio>.+)$"
)
_FA_LEAVE = re.compile(
    r"^(?:از\s*)?(?:این\s*|همین\s*)?(?:گروه|چت|گروه\s*و\s*چت|اینجا)\s*(?:رو|را)?\s*"
    r"(?:برو|ترک\s*کن|خارج\s*شو|بیا\s*بیرون)$"
)
#: "ساکت شو", "ساکت شو ۲ ساعت", "دو ساعت ساکت شو" — a mute with an optional span.
_FA_QUIET = re.compile(
    r"^(?:(?:برای|تا|به\s*مدت)\s*)?(?P<span>\d+\s*(?:ثانیه|دقیقه|ساعت|روز))?\s*"
    r"(?:ساکت|خفه|هیس)(?:\s*باش)?\s*(?:شو|باش|باشه)?\s*"
    r"(?:(?:برای|تا|به\s*مدت)\s*)?(?P<span2>\d+\s*(?:ثانیه|دقیقه|ساعت|روز))?\s*$"
)
_FA_LOUD = re.compile(r"^(?:دیگه\s*)?(?:ساکت\s*نباش|حرف\s*بزن|صدا\s*تو\s*بلند\s*کن)\s*$")
_FA_BAN = re.compile(
    r"^(?:(?:این|اون|همین|همون)(?:و|ش)?\s*)?(?:شخص|یارو|آدم|کاربر|بچه|فرد|طرف)?\s*"
    r"(?:رو|را)?\s*(?P<who>@[\w.]+)?\s*(?:رو|را)?\s*(?:بن|بلاک)\s*کن\s*(?P<who2>@[\w.]+)?$"
)
_FA_KICK = re.compile(
    r"^(?:(?:این|اون|همین|همون)(?:و|ش)?\s*)?(?:شخص|یارو|آدم|کاربر|بچه|فرد|طرف)?\s*"
    r"(?:رو|را)?\s*(?P<who>@[\w.]+)?\s*(?:رو|را)?\s*"
    r"(?:بیرون(?:ش|شون)?|اخراج(?:ش|شون)?|حذف(?:ش|شون)?)\s*کن\s*(?P<who2>@[\w.]+)?$"
)
_NAME_CHANGE = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?(?:change|set|make|put|update)\s+"
    r"(?:my|the)\s+(?:profile\s+|telegram\s+)?(?:display\s+)?name(?:\s+(?:to|as))?\s+(?P<name>.+?)\s*[.!]*$"
)
_BIO_CHANGE = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?(?:change|set|put|update|write)\s+"
    r"(?:my|the)\s+(?:profile\s+)?bio(?:\s+(?:to|as))?\s+(?P<bio>.+?)\s*[.!]*$"
)
_SCOPE_WORDS = {
    "private": "private",
    "private chat": "private",
    "private chats": "private",
    "dm": "private",
    "dms": "private",
    "direct message": "private",
    "direct messages": "private",
    "everyone": "any",
    "all chats": "any",
    "group": "group",
    "groups": "group",
    "groups only": "group",
}


def _lead(text: str) -> str:
    """Drop the vocative: "yura, set …" is an instruction, not a sentence about her."""
    # Case-insensitive on purpose: "Yura, turn the clock off" is the same order as
    # "yura turn the clock off", and the capitalised one was not stripped.
    return re.sub(
        r"^(?:hey\s+|ok\s+|okay\s+)?(?:yura|یورا|لیانا|leena)\b[ ,:؛-]*",
        "",
        text,
        flags=re.I,
    ).strip()


def _clean_body(raw: str) -> str:
    """The message he dictated, without the quotes a person wraps it in."""
    body = " ".join((raw or "").split()).strip()
    body = re.sub(r"^(?:->|=>|>|:|=|-|\(|\)|\[|\])\s*", "", body)
    body = body.strip(" .,!؟?؛;:=")
    for quote in ('"', "'", "`", "“", "«", "»", "”"):
        if len(body) > 1 and body.startswith(quote) and body.endswith(quote):
            body = body[1:-1].strip()
    return body


def _actions(text: str) -> Intent | None:
    """The requests that change how she behaves towards other people.

    These are the ones he photographed: an auto reply for his inbox, a name, a
    bio. Each becomes a console line — ``autoreply add --type any --private => …``
    — so the rule that gets stored is the rule ``.autoreply`` would have stored.
    """
    folded = _lead(_clean(text))

    if _AUTOREPLY_OFF.match(folded):
        return Intent(lines=("autoreply off",), label="autoreply", detail="auto-replies off")

    if _AWAY_OFF.match(folded):
        return Intent(lines=("away off",), label="away", detail="away off")

    match = _AUTOREPLY_IF.match(folded)
    if match:
        body = _clean_body(match.group("body"))
        if body and len(body) > 1 and body not in _NOISE:
            return Intent(
                lines=(f"autoreply add --type any --private => {body}", "autoreply on"),
                label="autoreply",
                detail="auto-reply · DMs",
            )
        return None

    match = _REPLY_TO_ALL.match(folded)
    if match:
        body = _clean_body(match.group("body"))
        if body and len(body) > 1 and body not in _NOISE:
            scope = "private" if re.search(r"\b(?:private|dm|dms|direct)\b", folded) else "any"
            return Intent(
                lines=(f"autoreply add --type any --{scope} => {body}", "autoreply on"),
                label="autoreply",
                detail=f"auto-reply · {scope}",
            )
        return None

    match = _AUTOREPLY.match(folded)
    if match:
        body = _clean_body(match.group("body"))
        scope = _SCOPE_WORDS.get(_clean(match.group("scope") or ""), "")
        if not scope:
            scope = "private" if _SCOPE_FROM_SENTENCE.search(folded) else "any"
        if body and len(body) > 1 and body not in _NOISE:
            return Intent(
                lines=(f"autoreply add --type any --{scope} => {body}", "autoreply on"),
                label="autoreply",
                detail=f"auto-reply · {scope}",
            )

    match = _AWAY_ON.match(folded)
    if match:
        body = _clean_body(match.group("body") or "")
        until = _clean_body(match.group("until") or "")
        if body:
            return Intent(lines=(f"away on {body}",), label="away", detail="away on")
        message = f"I am away until {until}" if until else "I am away right now"
        return Intent(lines=(f"away on {message}",), label="away", detail="away on")

    match = _NAME_CHANGE.match(folded)
    if match:
        name = " ".join(match.group("name").split()).strip(" .,!?\"'")
        if name and name not in _NOISE and len(name) <= 40:
            return Intent(lines=(f"profile name {name}",), label="profile", detail=f"name → {name}")

    match = _BIO_CHANGE.match(folded)
    if match:
        body = _clean_body(match.group("bio"))
        if body and body not in _NOISE:
            return Intent(lines=(f"bio set {body}",), label="profile", detail="bio")

    # ---- the same orders in Persian, in the word orders people type them ----
    match = _FA_NAME.match(folded)
    if match:
        name = _clean_body(match.group("name"))
        if name and name not in _NOISE and len(name) <= 40:
            return Intent(lines=(f"profile name {name}",), label="profile", detail=f"name → {name}")

    match = _FA_BIO.match(folded)
    if match:
        body = _clean_body(match.group("bio"))
        if body and body not in _NOISE:
            return Intent(lines=(f"bio set {body}",), label="profile", detail="bio")

    if _FA_LEAVE.match(folded):
        return Intent(lines=("leave --yes",), label="leave", detail="leave this chat")

    match = _FA_QUIET.match(folded)
    # Only a *span* becomes a quiet window: "ساکت شو" on its own means "stop
    # talking", which `_fa_orders` answers with `.yura off`, closer to the words.
    span = (match.group("span") or match.group("span2") or "").strip() if match else ""
    if span:
        return Intent(lines=(f"quiet-hours on {span}",), label="quiet-hours", detail="quiet hours on")

    match = _FA_BAN.match(folded)
    if match:
        who = _clean_body(match.group("who") or match.group("who2") or "")
        # No name means the message he replied to: the command reads the reply.
        return Intent(
            lines=((f"ban {who} --yes" if who else "ban --yes"),),
            label="ban",
            detail=f"ban {who}" if who else "ban · the replied-to person",
        )

    match = _FA_KICK.match(folded)
    if match:
        who = _clean_body(match.group("who") or match.group("who2") or "")
        return Intent(
            lines=((f"kick {who} --yes" if who else "kick --yes"),),
            label="kick",
            detail=f"kick {who}" if who else "kick · the replied-to person",
        )
    return None


# ---------------------------------------------------------------------------
# The rest of the surface: verbs on a message, the inbox, groups, herself
# ---------------------------------------------------------------------------
# Every pattern here is anchored and short. "why didn't you answer" is a question
# about *her*, so it becomes `.why`; "why didn't you answer that email from the
# bank" is not, and the `\s*$` at the end is what keeps it out.
_FORWARD = re.compile(
    r"^(?:please\s+)?(?:forward|send on|pass on)\s+(?:this|that|it)\s+to\s+(?P<who>.+)$",
    re.I,
)
_COPY = re.compile(
    r"^(?:please\s+)?copy\s+(?:this|that|it)\s+to\s+(?P<who>.+)$", re.I
)
_REPLY_TO = re.compile(
    r"^(?:please\s+)?(?:reply|answer|respond)\s+(?:to\s+)?(?:this|that|it|him|her|them)"
    r"\s*(?:and\s+)?(?:say|saying|with)?\s*(?P<body>.+)$",
    re.I,
)
_EDIT_TO = re.compile(
    r"^(?:please\s+)?(?:change|edit|fix)\s+(?:that|this|it)\s+(?:message\s+)?"
    r"(?:to\s+say|and\s+say|to)\s+(?P<body>.+)$",
    re.I,
)
_DELETE_THAT = re.compile(
    r"^(?:please\s+)?(?:delete|remove|unsend|take back)\s+(?:that|this|it)$"
    # "این پیام رو پاک کن" — in all the word orders people actually type.
    r"|^(?:این|اون|همین|همون)?\s*(?:پیام|پیامو|پیامش|پیامم)?\s*(?:رو|را)?\s*"
    r"(?:پاک(?:ش)?\s*کن|حذف(?:ش)?\s*کن|پاک\s*کنی|بردار)$",
    re.I,
)
_PIN_THAT = re.compile(
    r"^(?:please\s+)?pin\s+(?:that|this|it)$"
    r"|^(?:این|اون|همین)?\s*(?:پیام|پیامو|پیامش)?\s*(?:رو|را)?\s*"
    r"(?:پین|سنجاق|سنجاقش)\s*کن$",
    re.I,
)
_INBOX_ASK = re.compile(
    r"^(?:who'?s|who is|what'?s)\s+(?:in|on)\s+my\s+(?:messages|inbox|dms|chats)$"
    r"|^(?:my\s+)?(?:inbox|dms)$"
    r"|^who\s+am\s+i\s+(?:talking|messaging)\s+to$",
    re.I,
)
_UNREAD_ASK = re.compile(
    r"^(?:what'?s|anything)\s+(?:unread|unanswered|waiting)$"
    r"|^(?:unread|unanswered)$"
    r"|^who\s+is\s+waiting(?:\s+for\s+an?\s+answer)?$",
    re.I,
)
_READ_ASK = re.compile(
    r"^(?:what\s+did|what\s+has)\s+(?P<who>.+?)\s+(?:say|said|write|written|tell\s+you)"
    r"(?:\s+(?:to\s+you|you))?$",
    re.I,
)
_WHOIS_ASK = re.compile(r"^(?:who\s+is|who'?s)\s+(?P<who>@?[\w .-]{2,40})$", re.I)
_GROUPS_ASK = re.compile(r"^(?:my\s+groups|list\s+(?:my\s+)?groups|what\s+groups)$", re.I)
_DOCTOR_ASK = re.compile(
    r"^(?:are\s+you\s+(?:ok|okay|alright|fine|healthy|alive)|you\s+(?:ok|okay))\s*[?!.]*$",
    re.I,
)
_REPAIR_ASK = re.compile(r"^(?:fix|repair|heal)\s+yourself\s*[.!]*$", re.I)
_WHY_ASK = re.compile(
    r"^why\s+(?:didn'?t|don'?t|won'?t|are\s+you\s+not)\s+(?:you\s+)?"
    r"(?:answer|reply|respond|talk)(?:\s+(?:me|to\s+me))?\s*[?!.]*$",
    re.I,
)
_NEWS_ASK = re.compile(r"^(?:what'?s\s+new|whats\s+new|changelog|what\s+changed)\s*[?!.]*$", re.I)
_REMEMBER_ASK = re.compile(
    r"^(?:please\s+)?(?:remember|keep\s+in\s+mind|note\s+that|don'?t\s+forget)\s+"
    r"(?:that\s+)?(?P<fact>.+)$"
    r"|^(?:یادت\s*باشه|به\s*یاد\s*داشته\s*باش|یادداشت\s*کن)\s*(?:که\s*)?(?P<fact_fa>.+)$",
    re.I,
)
_FORGET_ASK = re.compile(
    r"^(?:please\s+)?(?:forget|drop)\s+(?:what\s+(?:i|you)\s+(?:said|know)\s+about\s+)?"
    r"(?P<what>.+)$"
    r"|^(?:فراموش\s*کن|یادت\s*نباشه|پاک\s*کن\s*از\s*حافظه)\s*(?:که\s*)?(?P<what_fa>.+)$",
    re.I,
)
_MEMORIES_ASK = re.compile(
    r"^(?:what\s+do\s+you\s+remember|what\s+do\s+you\s+know\s+about\s+me|your\s+memories"
    r"|show\s+(?:me\s+)?(?:your\s+)?memories)\s*[?!.]*$",
    re.I,
)
_RECALL_ASK = re.compile(
    r"^(?:what\s+do\s+you\s+(?:remember|know)\s+about|what\s+did\s+i\s+tell\s+you\s+about)\s+"
    r"(?P<what>.+)\s*[?!.]*$",
    re.I,
)
_USERNAME_ASK = re.compile(
    r"^(?:change|set|make)\s+(?:your|my|the)\s+(?:username|handle)\s+(?:to\s+)?(?P<name>@?[\w]+)$",
    re.I,
)
_ABILITIES_ASK = re.compile(
    r"^(?:what\s+can\s+you\s+do|what\s+are\s+your\s+(?:abilities|skills|powers)"
    r"|list\s+your\s+(?:abilities|skills)|what\s+are\s+you\s+capable\s+of)\s*[?!.]*$",
    re.I,
)
_CONTACTS_ASK = re.compile(
    r"^(?:who(?:'s|\s+is|\s+are)\s+in\s+my\s+contacts|my\s+contacts"
    r"|show\s+(?:me\s+)?(?:my\s+)?contacts|list\s+(?:my\s+)?contacts)\s*[?!.]*$",
    re.I,
)
_BLOCKED_ASK = re.compile(
    r"^(?:who\s+(?:is|did\s+i)\s+block(?:ed)?|who(?:'s|\s+is)\s+blocked"
    r"|show\s+(?:me\s+)?(?:the\s+)?block\s?list|my\s+block\s?list)\s*[?!.]*$",
    re.I,
)
_DIARY_ASK = re.compile(
    r"^(?:what(?:'s|\s+is)\s+in\s+my\s+saved\s+messages"
    r"|read\s+(?:my\s+)?saved\s+messages|show\s+(?:me\s+)?(?:my\s+)?saved\s+messages)\s*[?!.]*$",
    re.I,
)
_LEAVE_ASK = re.compile(
    r"^(?:please\s+)?(?:leave|exit|quit)\s+(?:this|the)\s+(?:group|chat|channel)\s*[?!.]*$",
    re.I,
)
_RENAME_ASK = re.compile(
    r"^(?:please\s+)?(?:rename|retitle)\s+(?:this|the)\s+(?:group|chat|channel)\s+"
    r"(?:to\s+)?(?P<title>.+)$",
    re.I,
)
_SLOWMODE_ASK = re.compile(
    r"^(?:set\s+)?slow\s?mode\s+(?P<value>off|\d+\s*(?:s|sec|m|min|h|hour)?)\s*[?!.]*$",
    re.I,
)
_PRIVACY_ASK = re.compile(
    r"^(?:what\s+can\s+(?:people|others|strangers)\s+see"
    r"|what\s+(?:do\s+)?(?:people|others)\s+see\s+about\s+me"
    r"|show\s+(?:me\s+)?(?:my\s+)?privacy(?:\s+settings)?"
    r"|who\s+can\s+see\s+(?:my\s+)?(?:number|phone|photo|last\s+seen|profile))\s*[?!.]*$",
    re.I,
)
_SECURITY_ASK = re.compile(
    r"^(?:is\s+(?:my\s+)?(?:two[\s-]?step|2fa|two[\s-]?factor)(?:\s+(?:lock|password|protection|verification))?"
    r"(?:\s+(?:on|enabled|set|active))?"
    r"|do\s+i\s+have\s+(?:two[\s-]?step|2fa|two[\s-]?factor)(?:\s+(?:on|enabled))?"
    r"|is\s+(?:my\s+)?account\s+(?:secure|locked)"
    r"|show\s+(?:me\s+)?(?:my\s+)?security)\s*[?!.]*$",
    re.I,
)
_DRAFT_ASK = re.compile(
    r"^(?:please\s+)?(?:write|save|leave|put)\s+a\s+draft\s+(?:to|for|in)\s+"
    r"(?P<who>@[\w.]+|[\w][\w.@-]{0,30}?)(?:\s*:\s*|\s+(?:saying|that\s+says?)\s+)(?P<body>.+)$",
    re.I,
)
_ARCHIVE_ASK = re.compile(
    r"^(?:please\s+)?(?P<verb>archive|unarchive)\s+(?P<who>@[\w.]+|this\s+chat)$",
    re.I,
)
_PEEK_ASK = re.compile(
    r"^(?:what(?:'s|\s+is)\s+(?:in|behind)|what\s+is)\s+(?:this\s+)?(?:invite|link)\s*"
    r"(?P<link>(?:https?://)?(?:t\.me|telegram\.me)/\S+)$",
    re.I,
)
_WIPE_ASK = re.compile(
    r"^(?:please\s+)?(?:clear|wipe|delete)\s+(?:the\s+)?(?:whole\s+|entire\s+)?"
    r"(?:this\s+|that\s+|our\s+)?"
    r"(?P<what>history|conversation|chat\s+history)(?:\s+(?:of|with)\s+(?P<who>.+))?\s*[?!.]*$",
    re.I,
)
_TOPICS_ASK = re.compile(
    r"^(?:show\s+(?:me\s+)?(?:the\s+)?topics|what\s+topics\s+are\s+there"
    r"|list\s+(?:the\s+)?topics|topics\s+here)\s*[?!.]*$",
    re.I,
)
_NEW_TOPIC_ASK = re.compile(
    r"^(?:please\s+)?(?:start|open|make|create)\s+(?:a\s+)?(?:new\s+)?topic\s+"
    r"(?:called\s+|named\s+|titled\s+)?(?P<title>.+)$",
    re.I,
)
_EXPORT_ASK = re.compile(
    r"^(?:please\s+)?(?:export|save|transcribe)\s+(?:the\s+)?(?:chat|conversation|history)"
    r"\s+(?:with|of)\s+(?P<who>@[\w.]+|.+?)(?:\s+(?:to\s+a\s+file|as\s+a\s+file|file))?\s*[?!.]*$",
    re.I,
)
_VERSION_ASK = re.compile(
    r"^(?:what|which)\s+version\s+are\s+you(?:\s+running)?\s*[?!.]*$", re.I
)


def _fa_when(text: str) -> tuple[str, str] | None:
    """A Persian time phrase and the rest of the sentence, split apart.

    ``یادم بنداز ساعت ۵ شیر بخرم`` → ``("05:00", "شیر بخرم")``. The command layer
    speaks ``HH:MM`` / ``tomorrow HH:MM`` / ``30m``; this is the translation from
    the way the sentence is actually said to that shape.
    """
    folded = " ".join(text.translate(_PERSIAN_DIGITS).split())
    day = ""
    if folded.startswith("فردا"):
        day, folded = "tomorrow ", folded[len("فردا") :].strip()
    elif folded.startswith("امروز"):
        day, folded = "", folded[len("امروز") :].strip()
    match = re.match(r"^(?:ساعت\s*)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>و\s*نیم)?\s+(?P<rest>.+)$", folded)
    if match and ("ساعت" in text or day):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or (30 if match.group("half") else 0))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{day}{hour:02d}:{minute:02d}", match.group("rest").strip()
    counted = re.match(
        r"^(?P<amount>\d{1,3})\s*(?P<unit>ثانیه|دقیقه|ساعت|روز)\s*(?:دیگه|بعد|دیگر)\s*(?P<rest>.*)$",
        folded,
    )
    if counted:
        unit = {"ثانیه": "s", "دقیقه": "m", "ساعت": "h", "روز": "d"}[counted.group("unit")]
        rest = counted.group("rest").strip() or "یادآوری"
        return f"{counted.group('amount')}{unit}", rest
    return None


def _fa_orders(folded: str) -> Intent | None:
    """The Persian orders that name a real ability, in the words he types them."""
    # The clock is *her* clock (the live time on the profile), not the wall clock.
    if re.match(r"^ساعت(?:ت|ت\s*رو|ت\s*را|م|م\s*رو|ه)?\s*(?:رو|را)?\s*(?:روشن|برگردون|برگردان|فعال|نشون)\s*کن", folded):
        return Intent(lines=("clock on",), label="clock", detail="clock on")
    if re.match(r"^ساعت(?:ت|ت\s*رو|ت\s*را|م|م\s*رو|ه)?\s*(?:رو|را)?\s*(?:خاموش|ببند|بردار|حذف)\s*کن", folded):
        return Intent(lines=("clock off",), label="clock", detail="clock off")

    if re.match(r"^(?:از\s*)?(?:این\s*|همین\s*)?گروه\s*(?:رو|را)?\s*(?:برو|ترک\s*کن|خارج\s*شو|بیا\s*بیرون)", folded):
        return Intent(lines=("leave --yes",), label="admin", detail="leave this group")

    match = re.match(
        r"^(?:اسم|نام)(?:م|ش|ت|ت\s*رو|م\s*رو|ش\s*رو)?\s*(?:رو|را)?\s*"
        r"(?:عوض\s*کن|تغییر\s*بده|بذار|بزار|کن)\s*(?:بذار|بزار|به|روی|:)?\s*(?P<name>.+)$",
        folded,
    )
    if match:
        name = match.group("name").strip(' .,!؟"«»')
        if name and len(name) <= 40:
            return Intent(lines=(f"profile name {name}",), label="profile", detail=f"name → {name}")

    match = re.match(
        r"^بیو(?:ت|م|ش)?\s*(?:رو|را)?\s*"
        r"(?:عوض\s*کن|تغییر\s*بده|بنویس|بذار|بزار|کن)\s*(?:بنویس|بذار|بزار|:)?\s*(?P<bio>.+)$",
        folded,
    )
    if match:
        bio = match.group("bio").strip(' .,!؟"«»')
        if bio and len(bio) > 1:
            return Intent(lines=(f"bio set {bio}",), label="profile", detail="bio")

    # "این شخص رو بن کن" — the target is the message he replied to, and the
    # command reads the reply rather than making him type a handle.
    if re.match(
        r"^(?:این|اون)?\s*(?:شخص|کاربر|یارو|آدم|بچه|طرف|مردم)?\s*(?:رو|را)?\s*بن\s*کن",
        folded,
    ):
        return Intent(lines=("ban --yes",), label="admin", detail="ban · the replied-to person")
    if re.match(
        r"^(?:این|اون)?\s*(?:شخص|یارو|آدم|طرف)?\s*(?:رو|را)?\s*(?:اخراج|بیرون|بیرونش)\s*کن",
        folded,
    ):
        return Intent(lines=("kick --yes",), label="admin", detail="kick · the replied-to person")

    if re.match(r"^(?:ساکت\s*شو|خفه\s*شو|خفه\s*بابا|ساکت)$", folded):
        return Intent(lines=("yura off",), label="persona", detail="quiet in this chat")
    if re.match(r"^(?:برگرد|دوباره\s*حرف\s*بزن|حرف\s*بزن|ساکت\s*نباش|دیگه\s*ساکت\s*نباش)$", folded):
        return Intent(lines=("yura on",), label="persona", detail="talking again")

    match = re.match(r"^یادم\s*(?:بنداز|بیار|بندازی)\s+(?P<body>.+)$", folded)
    if match:
        split = _fa_when(match.group("body"))
        if split:
            when, rest = split
            return Intent(lines=(f"remind {when} {rest}",), label="remind", detail=f"remind · {when}")
    return None


def _surface(text: str) -> Intent | None:
    """Message verbs, the inbox, groups, and questions about herself."""
    original = _tidy(_lead(_tidy(text)))
    folded = original.lower()
    if not folded or len(folded) > 70:
        return None

    order = _fa_orders(folded)
    if order is not None:
        return order

    match = _FORWARD.match(original)
    if match:
        who = _who(match.group("who"))
        if who:
            return Intent(lines=(f"forward {who}",), label="msg", detail=f"forward · {who}")

    match = _COPY.match(original)
    if match:
        who = _who(match.group("who"))
        if who:
            return Intent(lines=(f"copy {who}",), label="msg", detail=f"copy · {who}")

    match = _REPLY_TO.match(original)
    if match:
        body = match.group("body").strip(' .,!؟"\'')
        if body and len(body) > 1:
            return Intent(lines=(f"reply {body}",), label="msg", detail="reply")

    match = _EDIT_TO.match(original)
    if match:
        body = match.group("body").strip(' .,!؟"\'')
        if body and len(body) > 1:
            return Intent(lines=(f"edit {body}",), label="msg", detail="edit")

    if _DELETE_THAT.match(folded):
        return Intent(lines=("delete",), label="msg", detail="delete")
    if _PIN_THAT.match(folded):
        return Intent(lines=("pin",), label="msg", detail="pin")

    if _INBOX_ASK.match(folded):
        return Intent(lines=("inbox",), label="inbox", detail="inbox")
    if _UNREAD_ASK.match(folded):
        return Intent(lines=("unread",), label="inbox", detail="unread")

    match = _READ_ASK.match(original)
    if match:
        who = _who(match.group("who"))
        if who:
            return Intent(lines=(f"read {who}",), label="inbox", detail=f"read · {who}")

    # "who is in my contacts" is not "who is <somebody>": the address book is
    # asked for before the whois route can read the phrase as a name.
    if _CONTACTS_ASK.match(folded):
        return Intent(lines=("contacts",), label="people", detail="contacts")

    match = _WHOIS_ASK.match(original)
    if match:
        who = match.group("who").strip(" .,!?")
        if who and who.lower() not in _NOISE:
            return Intent(lines=(f"whois {who}",), label="inbox", detail=f"whois · {who}")

    if _GROUPS_ASK.match(folded):
        return Intent(lines=("groups",), label="chats", detail="groups")

    match = _REMEMBER_ASK.match(original)
    if match:
        fact = (match.group("fact") or match.group("fact_fa") or "").strip(" .,!?")
        if fact and len(fact) > 3:
            return Intent(lines=(f"remember {fact}",), label="memory", detail="remember")

    match = _FORGET_ASK.match(original)
    if match:
        what = (match.group("what") or match.group("what_fa") or "").strip(" .,!?")
        if what and len(what) > 1:
            return Intent(lines=(f"forget {what}",), label="memory", detail="forget")

    if _MEMORIES_ASK.match(folded):
        return Intent(lines=("memories",), label="memory", detail="memories")

    match = _RECALL_ASK.match(original)
    if match:
        what = match.group("what").strip(" .,!?")
        if what:
            return Intent(lines=(f"recall {what}",), label="memory", detail="recall")

    match = _USERNAME_ASK.match(original)
    if match:
        name = match.group("name").lstrip("@")
        return Intent(lines=(f"username {name}",), label="profile", detail=f"username · {name}")

    if _ABILITIES_ASK.match(folded):
        return Intent(lines=("abilities",), label="self", detail="abilities")

    match = _EXPORT_ASK.match(original)
    if match:
        who = _who(match.group("who"))
        if who:
            return Intent(
                lines=(f"exportchat {who}",), label="read", detail=f"export · {who}"
            )

    if _TOPICS_ASK.match(folded):
        return Intent(lines=("topics",), label="chats", detail="topics")

    match = _NEW_TOPIC_ASK.match(original)
    if match:
        title = match.group("title").strip(" .,!?")
        if title:
            return Intent(
                lines=(f"topic new {title}",), label="chats", detail=f"new topic · {title[:40]}"
            )

    match = _PEEK_ASK.match(original)
    if match:
        link = match.group("link").strip(" .,!?")
        return Intent(lines=(f"peek {link}",), label="chats", detail=f"peek · {link[:40]}")

    match = _WIPE_ASK.match(original)
    if match:
        who = (match.group("who") or "").strip(" .,!?")
        line = "clearhistory" + (f" {who}" if who else "")
        return Intent(lines=(line,), label="msg", detail="clear history")

    match = _DRAFT_ASK.match(original)
    if match:
        who = _who(match.group("who"))
        body = match.group("body").strip(' .,!?')
        if who and body:
            return Intent(
                lines=(f"draft {who} {body}",), label="chats", detail=f"draft · {who}"
            )

    match = _ARCHIVE_ASK.match(original)
    if match:
        verb = match.group("verb").lower()
        who = match.group("who").strip()
        if who.lower() in {"this chat", "this group"}:
            who = ""  # the command falls back to the chat he is standing in
        if who:
            return Intent(
                lines=(f"{verb} {who}",), label="chats", detail=f"{verb} · {who}"
            )

    if _PRIVACY_ASK.match(folded):
        return Intent(lines=("privacy",), label="account", detail="privacy")

    if _SECURITY_ASK.match(folded):
        return Intent(lines=("security",), label="account", detail="security")

    if _BLOCKED_ASK.match(folded):
        return Intent(lines=("blocklist",), label="people", detail="blocklist")

    if _DIARY_ASK.match(folded):
        return Intent(lines=("diary",), label="read", detail="saved messages")

    if _LEAVE_ASK.match(folded):
        return Intent(lines=("leave",), label="group", detail="leave")

    match = _RENAME_ASK.match(original)
    if match:
        title = match.group("title").strip(" .,!?")
        if title:
            return Intent(lines=(f"settitle {title}",), label="group", detail=f"rename · {title[:40]}")

    match = _SLOWMODE_ASK.match(original)
    if match:
        value = " ".join(match.group("value").split())
        return Intent(lines=(f"slowmode {value}",), label="group", detail=f"slowmode · {value}")

    if _DOCTOR_ASK.match(folded):
        return Intent(lines=("diagnose",), label="self", detail="diagnose")
    if _REPAIR_ASK.match(folded):
        return Intent(lines=("repair",), label="self", detail="repair")
    if _WHY_ASK.match(folded):
        return Intent(lines=("why",), label="self", detail="why")
    if _NEWS_ASK.match(folded):
        return Intent(lines=("news",), label="self", detail="news")
    if _VERSION_ASK.match(folded):
        return Intent(lines=("version",), label="self", detail="version")
    return None


# ---------------------------------------------------------------------------
# The toolbox: sending, reacting, ignoring, timelines
# ---------------------------------------------------------------------------
# These are the sentences he actually types at his phone, and they are the ones a
# keyword matcher gets wrong most often, because the *target* and the *body* sit
# inside the same line. So each shape is anchored, the body is taken verbatim, and
# a sentence without an explicit "who" stays conversation.
_SAY = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:message|text|dm|msg|ping)\s+(?P<who>@[\w.]+|saved messages|saved|me|[a-z][\w.]{1,20})"
    r"\s*(?:,|and)?\s*(?:saying|say|that|to say|telling (?:him|her|them))?\s*(?P<body>.+)$",
    re.I,
)
#: "write to @x and wish him a happy birthday": the verb is *write*, so the body
#: is an instruction, not a quote. `send`/`message` stay literal — "message @x and
#: say I'm late" must send those words, not ask a model what to say.
_COMPOSE = re.compile(
    r"^(?:please\s+)?(?:write|compose|draft)\s+(?:a\s+(?:message|note)\s+)?"
    r"(?:to\s+|for\s+)?(?P<who>@[\w.]+|saved messages|our saved messages|my saved messages|saved)"
    r"\s*(?:,|and)?\s*(?:saying|say|that|to say|telling (?:him|her|them))?\s*(?P<body>.+)$",
    re.I,
)
#: The target first, no "to": "send @sara surprise with a spoiler". Only shapes
#: that are unmistakably a chat — a handle, a number, saved messages — because a
#: bare word here would turn "send him the file" into a send to a person "him".
_SEND_FIRST = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:send|message|text|dm|msg|pass|deliver)\s+"
    r"(?P<who>@[\w.]+|saved messages|our saved messages|my saved messages|saved|\d{4,})"
    r"\s+(?P<body>.+)$",
    re.I,
)
_SEND_TO = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:send|forward|pass|deliver)\s+"
    # "forward this to Sara" is a *forward* of a real message, not a send of the
    # word "this": the demonstrative hands it to `_surface`.
    r"(?!(?:this|that|it)\s+(?:to|for)\b)(?P<body>.+?)\s+(?:to|for)\s+(?P<who>.+)$",
    re.I,
)
_SAY_IN = re.compile(
    r"^(?:please\s+)?(?:say|write|put|send)\s+(?P<body>.+?)\s+"
    r"(?:in|to|into)\s+(?P<who>our saved messages|saved messages|my saved messages|saved)$",
    re.I,
)
_SAY_INTO = re.compile(
    r"^(?:then\s+)?(?:in|into|to)\s+(?P<who>our saved messages|saved messages|my saved messages|saved)"
    r"\s*(?:,|and)?\s*(?:say|saying|write|put|post)?\s*(?P<body>.+)$",
    re.I,
)
#: "به سارا پیام بده که دیر می‌رسم" / "پیام بده به سارا دیر می‌رسم" — the Persian
#: way of dictating a message, in both word orders.
_SEND_FA = re.compile(
    r"^(?:please\s+)?(?:به|برای)\s+(?P<who>@[\w.]+|[^\s،]+)\s*"
    r"(?:پیام|پیامک|مسیج)\s*(?:بده|بفرست|بزن)\s*(?:که|با\s*متن|:)?\s*(?P<body>.+)$",
    re.I,
)
_SEND_FA2 = re.compile(
    r"^(?:please\s+)?(?:پیام|پیامک|مسیج)\s*(?:بده|بفرست|بزن)\s+"
    r"(?:به|برای)\s+(?P<who>@[\w.]+|[^\s،]+)\s*(?:که|:)?\s*(?P<body>.+)$",
    re.I,
)

#: "say I'm a good girl and repeat 100 times" — the count is the instruction, and
#: the place is optional because "here" is where he is standing.
_REPEAT_AFTER_SAY = re.compile(
    r"^(?:please\s+)?(?:say|tell\s+(?:them|him|her|everyone)|write|send|post)\s+(?P<body>.+?)\s+"
    r"(?:and\s+)?(?:repeat(?:ing)?\s+(?:it\s+)?|send\s+it\s+(?:again\s+)?)"
    r"(?P<count>\d{1,3}|a couple of|three|five|ten|twice)\s+times?"
    r"(?:\s+(?:in|to|into)\s+(?P<where>this\s+(?:group|chat|channel|room)|here|the\s+group|the\s+chat))?"
    r"\s*[.!]*$",
    re.I,
)
#: "say I'm a good girl and repeat 100 times" — the count is the instruction and
#: "here" is where he is standing when no room is named.
_REPEAT_AFTER_SAY = re.compile(
    r"^(?:please\s+)?(?:say|tell\s+(?:them|him|her|everyone)|write|send|post)\s+(?P<body>.+?)\s+"
    r"(?:and\s+)?(?:repeat(?:ing)?\s+(?:it\s+)?|send\s+it\s+(?:again\s+)?)?"
    r"(?P<count>\d{1,3}|a\s+couple\s+of|three|five|ten|twice)\s+times?"
    r"(?:\s+(?:in|to|into)\s+(?P<where>this\s+(?:group|chat|channel|room)|here|the\s+group|the\s+chat))?"
    r"\s*[.!]*$",
    re.I,
)
_REPEAT_FIRST = re.compile(
    r"^(?:please\s+)?(?:repeat|send|say|post)\s+(?P<body>.+?)\s+"
    r"(?P<count>\d{1,3}|a\s+couple\s+of|three|five|ten|twice)\s+times?"
    r"(?:\s+(?:in|to|into)\s+(?P<where>this\s+(?:group|chat|channel|room)|here|the\s+group))?"
    r"\s*[.!]*$",
    re.I,
)
#: "send hi in 10 different messages in this group" — a burst, not a repeat of one.
_BURST = re.compile(
    r"^(?:please\s+)?(?:send|say|write|put|post)\s+(?P<body>.+?)\s+"
    r"(?:in|as)\s+(?P<count>\d{1,3}|a\s+couple\s+of|three|five|ten)\s+"
    r"(?:different\s+|separate\s+|new\s+|individual\s+)?messages?"
    r"\s+(?:in|to|into)\s+(?P<where>this\s+(?:group|chat|channel|room)|here|the\s+group|the\s+chat)"
    r"\s*[.!]*$",
    re.I,
)

#: The Persian burst: "«من دختر خوبم» رو ۱۰۰ بار بگو", "۱۰ تا پیام جدا بفرست".
#: The count comes after the words, and the room is named with "تو این گروه".
_REPEAT_FA = re.compile(
    # "«من دختر خوبم» رو ۱۰۰ بار بگو" — and "۱۰ بار بگو سلام".
    r"^(?:(?P<body1>.+?)\s*(?:رو|را)?\s*)?"
    r"(?<!\d)(?P<count1>\d{1,3}|صد|دویست|سه|پنج|ده)\s*(?:بار|دفعه)\s*"
    r"(?:جدا|پیوسته|پشت\s*سر\s*هم)?\s*(?:بگو|بفرست|بنویس|بزن)\s*(?P<body2>.+)?$"
    # "۱۰ تا پیام جدا بفرست" — the count is the number of messages.
    r"|^(?P<count3>\d{1,3}|ده|پنج|سه)\s*[تت]ا\s*پیام\s*(?:جدا|دیگر|مختلف)?\s*"
    r"(?:بفرست|بگو|بنویس)\s*(?P<body4>.+?)?\s*$"
    # "بگو من دختر خوبم ۱۰۰ بار"
    r"|^(?:بگو|بفرست|بنویس)\s+(?P<body3>.+?)\s*(?:رو|را)?\s*"
    r"(?<!\d)(?P<count2>\d{1,3}|صد|دویست|سه|پنج|ده)\s*(?:بار|دفعه)\s*$",
    re.I,
)
_REPEAT_FIRST = re.compile(
    r"^(?:please\s+)?(?:repeat|send|say|post)\s+(?P<body>.+?)\s+"
    r"(?P<count>\d{1,3}|a couple of|three|five|ten|twice)\s+times?"
    r"(?:\s+(?:in|to|into)\s+(?P<where>this\s+(?:group|chat|channel|room)|here|the\s+group))?"
    r"\s*[.!]*$",
    re.I,
)
#: "send hi in 10 different messages in this group" — a burst, not a repeat of one.
_BURST = re.compile(
    r"^(?:please\s+)?(?:send|say|write|put|post)\s+(?P<body>.+?)\s+"
    r"(?:in|as)\s+(?P<count>\d{1,3}|a couple of|three|five|ten)\s+"
    r"(?:different\s+|separate\s+|new\s+|individual\s+)?messages?"
    r"\s+(?:in|to|into)\s+(?P<where>this\s+(?:group|chat|channel|room)|here|the\s+group|the\s+chat)"
    r"\s*[.!]*$",
    re.I,
)
_IGNORE = re.compile(
    r"^(?:please\s+)?(?:don'?t|do not|never)\s+(?:talk|answer|reply|respond)\s+"
    r"(?:to\s+|back to\s+)?(?P<who>@[\w.]+|[\w][\w\s.-]{1,40})$",
    re.I,
)
_UNIGNORE = re.compile(
    r"^(?:please\s+)?(?:talk|answer|reply|respond)\s+(?:to\s+)?(?P<who>@[\w.]+|[\w][\w\s.-]{1,40})\s+again$"
    r"|^(?:unignore|unblock|stop ignoring)\s+(?P<who2>@[\w.]+|[\w][\w\s.-]{1,40})$",
    re.I,
)
_REACT = re.compile(
    r"^(?:please\s+)?(?:react|put a reaction|leave a reaction|add a reaction)\s*"
    r"(?:to (?:that|this|it|his message|her message|their message)\s*)?"
    r"(?:with\s+|using\s+)?(?P<emoji>.+)$",
    re.I,
)
#: The Persian order has three word orders and one verb in common: the reaction
#: word itself. Everything else in the sentence is noise, so the verb is the
#: trigger and the remaining token is the reaction.
#: The words that make "react" an *order* rather than a topic.
_REACT_ORDER = re.compile(
    r"\b(?:react|reaction)\b|بده|بکن|\bکن\b|بذار|بگذار|بنداز|بگیر", re.I
)
_REACT_FA_VERB = re.compile(
    r"(?:واکنش|ری\s*[-\u200c]?\s*اکشن|راکشن|ریакشن)", re.I
)
"""The words that are never the reaction: they are the sentence around it."""
_MESSAGE_WORD = re.compile(r"پیام|پست|مسج|مسیج", re.I)
_REACT_FA_NOISE = {
    "به", "بهش", "بهاش", "به‌ش", "این", "اینو", "اون", "اونو", "ایش", "اینا",
    "پیام", "پیامش", "پیامو", "پیام‌ش", "پست", "یورا", "لطفا", "لطفاً", "یه",
    "یک", "بده", "بکن", "کن", "بذار", "بگذار", "بزار", "بنداز",
    "برای", "روی", "رو", "با", "و", "please", "react", "reaction", "add",
    "put", "leave", "with", "to", "his", "her", "their", "message", "the",
}
#: "یورا به پیامش قلب واکنش بده" — `قلب` is a word for a heart. Translated to
#: the reaction Telegram actually accepts, because a reaction is bytes, not prose.
_REACTION_WORDS = {
    "قلب": "❤️", "هارت": "❤️", "دل": "❤️", "عشق": "❤️", "heart": "❤️", "love": "❤️",
    "لایک": "👍", "like": "👍", "پسند": "👍", "thumbs up": "👍",
    "خنده": "🤣", "لول": "🤣", "laugh": "🤣", "lol": "🤣",
    "آتش": "🔥", "fire": "🔥", "ستاره": "⭐", "star": "⭐",
    "دست": "👏", "تشویق": "👏", "clap": "👏", "گل": "💐", "flower": "💐",
    "خوشحال": "🥰", "دوستت دارم": "🥰", "هزار": "💯", "صد": "💯", "hundred": "💯",
    "فکر": "🤔", "think": "🤔", "گریه": "😭", "cry": "😭", "تعجب": "🤯", "wow": "🤯",
}
_PLAN_STOP = re.compile(
    r"^(?:stop|cancel|kill|end)\s+(?:the\s+)?(?:plan|timeline)\s*(?P<what>.*)$|"
    r"^(?:stop|cancel)\s+(?P<what2>that|it|everything)$",
    re.I,
)
_PLAN_ASK = re.compile(r"^(?:what'?s (?:planned|scheduled)|my plans|show (?:me )?the plans?)$", re.I)
#: A clock time as people write it: "ساعت ۵", "ساعت 17:30", "at 9 am".
_FA_CLOCK_TIME = re.compile(
    r"(?:ساعت|at)\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<evening>عصر|شب|pm)?",
    re.I,
)
_TIMES = re.compile(r"\b(?P<count>\d{1,3}|a couple of|three|five|ten)\s+times\b", re.I)
_SPOILER = re.compile(r"\b(?:with a |as a |in a )?spoiler\b|\bhidden\b", re.I)
_SILENT = re.compile(r"\b(?:silently|quietly|without a sound|no notification)\b", re.I)
_DELETE_AFTER = re.compile(
    r"\b(?:and )?(?:delete|remove|unsend)\s+(?:it|them)?\s*(?:after|in)\s+"
    r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours)\b",
    re.I,
)
_SEQUENCE = re.compile(r"\b(?:and then|then|after that|afterwards|next)\b|\b(?:بعد|سپس)\b", re.I)
_FOR_A_WHILE = re.compile(
    r"\bfor\s+\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours)\b",
    re.I,
)
#: Counting words for "ten times". Deliberately *not* named _WORD_NUMBERS:
#: that dict already exists for durations ("one hour"), and shadowing it
#: turned every "set a timer in one hour" into silence.
_COUNT_WORDS = {"a couple of": 2, "three": 3, "five": 5, "ten": 10}

#: Guards the plan reader from reading itself: a clause inside a timeline is an
#: instruction, and must not be re-read as another timeline.
_READING_PLAN = False


_COUNT_WORDS_LOCAL = {
    "a couple of": 2, "twice": 2, "three": 3, "five": 5, "ten": 10,
}


def _count_of(raw: str) -> int:
    """How many messages "repeat 100 times" asks for, clamped to her limit."""
    text = " ".join(str(raw or "").lower().split())
    if text in _COUNT_WORDS_LOCAL:
        return _COUNT_WORDS_LOCAL[text]
    try:
        return max(0, min(int(text), 500))
    except ValueError:
        return 0


def _repeat_count(raw: str) -> int:
    """The number in "repeat 40 times", clamped to what the sender allows."""
    text = str(raw or "").strip().lower()
    if text == "twice":
        return 2
    value = _COUNT_WORDS.get(text)
    if value is None:
        try:
            value = int(text)
        except ValueError:
            value = 0
    return max(0, min(int(value or 0), 500))


def _reaction_from(raw: str) -> str:
    """The glyph in "a heart", "❤️", "a strong thumbs up", or nothing.

    A glyph wins over a word, and a word is looked up in the same table the
    Persian orders use — "react with a heart" is not a different instruction.
    """
    text = str(raw or "").strip()
    glyph = emoji_in(text)
    if glyph:
        return glyph
    words = [word for word in re.split(r"[\s،,.!؟?]+", text.lower()) if word]
    for word in words:
        if word in {"a", "an", "the", "some", "aesthetic", "please", "with", "using"}:
            continue
        if word in _REACTION_WORDS:
            return _REACTION_WORDS[word]
    joined = " ".join(words)
    for phrase, emoji in sorted(_REACTION_WORDS.items(), key=lambda item: -len(item[0])):
        if phrase in joined:
            return emoji
    return ""


def _points_at_message(text: str) -> bool:
    """"این پیام …" with a verb: the sentence is about the message he replied to."""
    return bool(_MESSAGE_WORD.search(text)) and bool(_REACT_ORDER.search(text))


def _fa_reaction(text: str) -> str:
    """Pull the reaction out of a Persian order, or return nothing.

    "به پیامش قلب واکنش بده", "به این پیام ری‌اکشن بده ❤️", "بهش ری اکشن 🔥 بده"
    are one instruction in three shapes. The verb is already known to be present;
    what is left after the connective words is the reaction — either a glyph or a
    word for one.
    """
    words = [word for word in re.split(r"[\s،,.!؟?]+", str(text or "")) if word]
    glyphs: list[str] = []
    spoken: list[str] = []
    for word in words:
        bare = word.strip()
        if _REACT_FA_VERB.fullmatch(bare):
            continue
        if emoji_in(bare):
            glyphs.append(bare)
            continue
        lowered = bare.lower()
        if lowered in _REACT_FA_NOISE:
            continue
        if not re.search(r"[\u0600-\u06FF]", bare):
            continue
        spoken.append(lowered)
    for candidate in glyphs:
        cleaned = emoji_in(candidate)
        if cleaned:
            return cleaned
    for candidate in spoken:
        if candidate in _REACTION_WORDS:
            return _REACTION_WORDS[candidate]
    joined = " ".join(spoken)
    for phrase, emoji in sorted(_REACTION_WORDS.items(), key=lambda item: -len(item[0])):
        if phrase in joined:
            return emoji
    return ""


def _reach_options(body: str) -> tuple[str, list[str]]:
    """Pull the options out of a dictated message, verbatim text preserved."""
    text = str(body or "").strip()
    flags: list[str] = []
    match = _TIMES.search(text)
    if match:
        raw = match.group("count").lower()
        count = _COUNT_WORDS.get(raw)
        if count is None:
            try:
                count = int(raw)
            except ValueError:
                count = None
        if count and 1 < count <= 500:
            flags += ["--repeat", str(count)]
        text = (text[: match.start()] + " " + text[match.end():]).strip(" ,.")
    match = _DELETE_AFTER.search(text)
    if match:
        flags += ["--delete-after", f"{match.group('amount')}{match.group('unit').lower()}"]
        text = (text[: match.start()] + " " + text[match.end():]).strip(" ,.")
    if _SPOILER.search(text):
        flags.append("--spoiler")
        text = _SPOILER.sub(" ", text).strip(" ,.")
    if _SILENT.search(text):
        flags.append("--silent")
        text = _SILENT.sub(" ", text).strip(" ,.")
    text = re.sub(r"\s+", " ", text).strip(" ,.;")
    return text, flags


def _who(raw: str) -> str:
    who = " ".join((raw or "").split()).strip(" .,!")
    who = re.sub(r"^(?:to|for|my|our)\s+", "", who, flags=re.I)
    if who.lower() in {"saved", "saved messages", "my saved messages", "our saved messages"}:
        return "saved"
    if who.lower() in {"me", "myself"}:
        return "me"
    return who


def _tidy(text: str) -> str:
    """:func:`_clean` without the lowercasing: the body he dictated is verbatim.

    "say I'm your good girl" is a message to send, so the capital I and the
    apostrophe are the content, not formatting to be normalised away.
    """
    tidied = (text or "").translate(_PERSIAN_DIGITS)
    tidied = tidied.replace("\u200c", " ").replace("‌", " ")
    tidied = re.sub(r"[\u200e\u200f\u202a-\u202e]", "", tidied)
    return " ".join(tidied.strip().split())


def _reach(text: str, *, allow_plan: bool = True) -> Intent | None:
    """Sending, reacting, ignoring and timelines, read from the sentence."""
    original = _tidy(_lead(_tidy(text)))
    folded = original.lower()
    if not folded:
        return None

    if _PLAN_ASK.match(folded):
        return Intent(lines=("plan list",), label="plan", detail="plans")

    match = _PLAN_STOP.match(original) if allow_plan else None
    if match:
        what = (match.group("what") or match.group("what2") or "").strip()
        return Intent(lines=(f"plan stop {what}".strip(),), label="plan", detail="stop")

    # A timeline is read before anything else: "clock off for ten minutes, then on
    # and off again after three" must not be answered with one clock command.
    if allow_plan and (_SEQUENCE.search(folded) or _FOR_A_WHILE.search(folded)):
        lines = _plan_lines(original)
        if lines:
            return Intent(lines=lines, label="plan", detail="timeline")

    match = _IGNORE.match(original)
    if match:
        who = _who(match.group("who"))
        if who:
            return Intent(lines=(f"ignore on {who}",), label="ignore", detail=f"ignore {who}")

    match = _UNIGNORE.match(original)
    if match:
        who = _who(match.group("who") or match.group("who2") or "")
        if who:
            return Intent(lines=(f"ignore off {who}",), label="ignore", detail=f"unignore {who}")

    match = _REACT.match(original)
    if match and not re.match(r"^(?:react|reaction)\b\s*$", folded, re.I):
        # The same rule in English: "react to this" with no glyph gets the heart.
        emoji = _reaction_from(match.group("emoji")) or (
            "❤️" if _REACT_ORDER.search(folded) else ""
        )
        if emoji:
            return Intent(lines=(f"emoji {emoji}",), label="react", detail=f"react {emoji}")

    # "ایموجی 🔥 بگیر" — the Persian word for emoji is the verb on its own.
    if re.search(r"ایموجی|ایموجی\s*بده|اموجی", folded) and _REACT_ORDER.search(folded):
        glyph = _fa_reaction(folded)
        if glyph:
            return Intent(lines=(f"emoji {glyph}",), label="react", detail=f"react {glyph}")

    if _REACT_FA_VERB.search(folded):
        # "به این پیام واکنش بده" names no glyph. A heart is the answer that is
        # never wrong, and doing nothing is the answer that always is.
        emoji = _fa_reaction(folded) or (
            "❤️" if _REACT_ORDER.search(folded) else ""
        )
        if emoji:
            return Intent(lines=(f"emoji {emoji}",), label="react", detail=f"react {emoji}")
    elif _points_at_message(folded):
        # "به این پیام ❤️ بده" never says the word *reaction* — the glyph is the
        # verb. Only a real glyph or a word for one counts here, because
        # "این پیام رو پاک کن" also points at a message.
        emoji = _fa_reaction(folded)
        if emoji:
            return Intent(lines=(f"emoji {emoji}",), label="react", detail=f"react {emoji}")

    spoken_once = _lead(original)
    for pattern in (_SEND_FA, _SEND_FA2):
        match = pattern.match(spoken_once)
        if not match:
            continue
        body = _clean_body(match.group("body"))
        who = match.group("who").strip(" ،,.:؛")
        if body and len(body) > 1 and who and body not in _NOISE:
            return Intent(
                lines=(f"send {who} {body}",),
                label="send",
                detail=f"send · {who}",
            )
    for pattern in (_BURST, _REPEAT_AFTER_SAY, _REPEAT_FIRST):
        match = pattern.match(spoken_once)
        if not match:
            continue
        body = _clean_body(match.group("body"))
        count = _repeat_count(match.group("count"))
        if not body or len(body) < 1 or count < 2:
            continue
        # Nothing but a number is not a message ("repeat 3 times 3 times").
        if body.isdigit():
            continue
        if body.lower() in _NOISE:
            continue
        # A named recipient survives: "send @sara boo 3 times" is to Sara.
        target = ""
        addressed = re.match(r"(@[\w.]+|saved messages|saved)\s+(?P<rest>.+)$", body, re.I)
        if addressed:
            target = addressed.group(1)
            body = addressed.group("rest").strip()
        if not target:
            # No room named: the one he is standing in. Sending to Saved
            # Messages instead is how "repeat it 100 times" went nowhere.
            target = "--here"
        where = target
        return Intent(
            lines=(" ".join(part for part in ("send", where, body, "--repeat", str(count)) if part),),
            label="send",
            detail=f"send · {count}×",
        )

    match = _REPEAT_FA.match(spoken_once)
    if match:
        body = _clean_body(
            match.group("body1")
            or match.group("body2")
            or match.group("body3")
            or match.group("body4")
            or ""
        )
        count = _repeat_count(
            (match.group("count1") or match.group("count2") or match.group("count3") or "")
            .replace("صد", "100")
            .replace("دویست", "200")
            .replace("سه", "3")
            .replace("پنج", "5")
            .replace("ده", "10")
        )
        # "…تو این گروه" names the room, not the words being sent.
        body = re.sub(r"\s*(?:تو|توی|در)\s*این\s*(?:گروه|چت|جا)\s*$", "", body).strip()
        if body and count >= 2 and not body.isdigit():
            return Intent(
                lines=(f"send --here {body} --repeat {count}",),
                label="send",
                detail=f"send · {count}×",
            )

    for pattern in (_REPEAT_AFTER_SAY, _REPEAT_FIRST, _BURST):
        match = pattern.match(original)
        if not match:
            continue
        body = match.group("body").strip(" \"'«»“”")
        count = _count_of(match.group("count"))
        if not body or count < 2 or body.isdigit():
            continue
        target = "--here"
        named = re.match(r"(@[\w.]+|saved messages?)\s+(?P<rest>.+)$", body, re.I)
        if named:
            target = named.group(1)
            body = named.group("rest").strip()
        return Intent(
            lines=(f"send {target} {body} --repeat {count}",),
            label="send",
            detail=f"send · {count}×",
        )

    match = _SAY_INTO.match(original)
    if match:
        body, flags = _reach_options(match.group("body"))
        if body and len(body) > 1:
            return Intent(
                lines=(" ".join(["send saved", body, *flags]),),
                label="send",
                detail="send · saved messages",
            )

    match = _SAY_IN.match(original)
    if match:
        body, flags = _reach_options(match.group("body"))
        if body and len(body) > 1:
            return Intent(
                lines=(" ".join(["send saved", body, *flags]),),
                label="send",
                detail="send · saved messages",
            )

    match = _SAY.match(original)
    if match:
        who = _who(match.group("who"))
        body, flags = _reach_options(match.group("body"))
        if who and body and len(body) > 1 and body.lower() not in _NOISE:
            return Intent(
                lines=(" ".join(["send", who, body, *flags]),), label="send", detail=f"send · {who}"
            )

    match = _COMPOSE.match(original)
    if match:
        body, flags = _reach_options(match.group("body"))
        who = _who(match.group("who"))
        if body and len(body) > 1:
            return Intent(
                lines=(" ".join(["send", who, body, "--compose", *flags]),),
                label="send",
                detail=f"write · {who}",
            )

    match = _SEND_FIRST.match(original)
    if match:
        body, flags = _reach_options(match.group("body"))
        who = _who(match.group("who"))
        if body and len(body) > 1:
            return Intent(
                lines=(" ".join(["send", who, body, *flags]),),
                label="send",
                detail=f"send · {who}",
            )

    match = _SEND_TO.match(original)
    if match:
        who = _who(match.group("who"))
        body, flags = _reach_options(match.group("body"))
        targets = [part.strip() for part in re.split(r",|\band\b", who) if part.strip()]
        if targets and body and len(body) > 1:
            joined = ",".join(targets)
            command = "broadcast" if len(targets) > 2 else "send"
            return Intent(
                lines=(" ".join([command, joined, body, *flags]),),
                label="send",
                detail=f"send · {len(targets)}",
            )

    return None


def _plan_lines(sentence: str) -> tuple[str, ...]:
    """Read a whole timeline sentence into one console line."""
    from .plans import build_plan

    plan = build_plan(sentence, intent=sys.modules[__name__])
    if plan.empty or len(plan.steps) < 2:
        return ()
    return (f"plan do {sentence}",)


def _utility(text: str) -> Intent | None:
    """The small, unambiguous asks: the time, a coin, a sum, a poem.

    Every rule is a whole sentence — anchored at ``^`` and closed at ``$`` — for
    the same reason :func:`_misc` is: "the time in that poem was beautiful" is a
    sentence about a poem, not a request for a clock in Tokyo. A question that
    happens to contain the words is conversation; a question that *is* the words
    is an instruction.
    """
    folded = _clean(text)

    match = _ASK_TIME.match(folded)
    if match:
        place = next((value for value in match.groups() if value), None)
        if place:
            place = re.sub(r"\b(?:please|now|right now|لطفا)\b", "", place).strip(" ,.?!؟")
            # "the time in that poem about" is a sentence about a poem. A place is
            # a short name, not a clause carrying pronouns or content words.
            if len(place.split()) > 3 or re.search(
                r"\b(?:that|this|it|about|poem|song|book|story|verse|line|movie|photo)\b", place
            ):
                # The words were there, the ask was not: this is a sentence about
                # a poem that happens to mention a time.
                return None
        line = f"time {place}" if place else "time"
        return Intent(lines=(line,), label="time", detail=line)

    if _ASK_DATE.match(folded):
        return Intent(lines=("time",), label="time", detail="date")

    if _VERSION.match(folded):
        return Intent(lines=("about",), label="about", detail="about")

    if _COIN.match(folded):
        return Intent(lines=("coin",), label="coin", detail="coin")

    match = _RANDOM_NUMBER.match(folded)
    if match:
        if match.group("low") and match.group("high"):
            line = f"random {match.group('low')} {match.group('high')}"
        else:
            line = "random"
        return Intent(lines=(line,), label="random", detail=line)

    spoken = folded
    for word, symbol in _MATH_WORDS:
        spoken = re.sub(word, f" {symbol} ", spoken)
    spoken = " ".join(spoken.split())
    match = _SUM.match(spoken)
    if match:
        expression = " ".join(match.group("expr").strip(" =?").split())
        if re.search(r"\d", expression) and re.search(r"[-+*/%^]", expression):
            line = f"calc {expression}"
            return Intent(lines=(line,), label="calc", detail=line)

    if _BACKUP.match(folded):
        return Intent(lines=("backup export",), label="backup", detail="backup export")

    if _SELFCHECK.match(folded):
        return Intent(lines=("selfcheck",), label="selfcheck", detail="selfcheck")

    if _PING.match(folded):
        return Intent(lines=("ping",), label="ping", detail="ping")

    if _LANG_OF_TEXT.match(folded):
        return Intent(lines=("detect",), label="detect", detail="detect")

    if _POEM_ASK.match(folded):
        return Intent(lines=("poem",), label="poem", detail="poem")

    if _AGENDA_WEEK.match(folded):
        return Intent(lines=("agenda week",), label="agenda", detail="agenda week")

    if _AGENDA.match(folded):
        return Intent(lines=("agenda today",), label="agenda", detail="agenda today")

    if _BOOKMARKS_LIST.match(folded):
        return Intent(lines=("bookmark list",), label="bookmark", detail="bookmark list")

    match = _SUMMARIZE.match(folded)
    if match:
        count = match.group("count")
        line = f"summarize {count}" if count else "summarize"
        return Intent(lines=(line,), label="summarize", detail=line)

    return None


#: "send me a sticker with a cat", "استیکر قلب بفرست". The emoji or the word is
#: the argument; a sticker with no subject is not a command.
_STICKER_EN = re.compile(
    r"^(?:please\s+)?(?:send\s+(?:me\s+)?)?(?:a\s+|an\s+)?sticker"
    r"(?:\s+(?:of|with|for|that\s+(?:is|says?))?\s*(?P<what>.+?))?\s*[.!]*$",
    re.I,
)
_STICKER_FA = re.compile(
    r"^(?:please\s+)?(?:یک\s+)?استیکر"
    r"(?:\s*(?:از|با|به\s+شکل)?\s*(?P<what>.+?))?"
    r"\s*(?:بفرست|بده|بنداز|بزن)?\s*[.!؟?]*$",
    re.I,
)


_QUIET_ASK = re.compile(r"(?:quiet\s*hours|ساعت\s*(?:های\s*)?سکوت|سکوت\s*ساعت)")
_QUIET_ON = re.compile(
    r"(?:روشن|فعال|بذار|تنظیم|از|on|enable|start|set)", re.I
)
_QUIET_OFF = re.compile(r"(?:خاموش|غیرفعال|بردار|لغو|off|disable|stop|cancel|clear)", re.I)


def _quiet_hours(folded: str) -> Intent | None:
    """"quiet hours" and "ساعت سکوت" are one feature, and it is not the clock."""
    if not _QUIET_ASK.search(folded):
        return None
    if _QUIET_OFF.search(folded):
        return Intent(lines=("quiet-hours off",), label="quiet", detail="quiet hours off")
    if _QUIET_ON.search(folded):
        return Intent(lines=("quiet-hours on",), label="quiet", detail="quiet hours on")
    return Intent(lines=("quiet-hours status",), label="quiet", detail="quiet hours")


#: "ساعت چنده؟" / "what time is it" — a question with a number for an answer.
_TIME_ASK = re.compile(
    r"^(?:ساعت\s*(?:چند(?:ه|ست)?|چند\s*شد|چیست)|چه\s*ساعتی(?:ه|ست)?)\s*[؟?!.]*$"
    r"|^(?:what(?:'s|\s+is)?\s+the\s+time|what\s+time\s+is\s+it)\s*[?!.]*$",
    re.I,
)


def _time_ask(folded: str) -> Intent | None:
    if _TIME_ASK.match(_lead(folded)):
        return Intent(lines=("time",), label="time", detail="time")
    return None


#: What she can do, in his words rather than the command list's.
_CAPABILITY_ASK = re.compile(
    r"^(?:چه\s*(?:کار(?:ها|هایی)?|بلدی)|چیکار\s*می\s*تونی\s*(?:بکنی|کنی)|"
    r"چه\s*قابلیت(?:‌ها|هایی)?\s*(?:داری|هست)|"
    r"what\s+can\s+you\s+do|what\s+do\s+you\s+do|list\s+(?:your\s+)?abilities)\s*[؟?!.]*$",
    re.I,
)


def _capability_ask(folded: str) -> Intent | None:
    if _CAPABILITY_ASK.match(_lead(folded)):
        return Intent(lines=("help",), label="lookup", detail="help")
    return None


#: "برگرد" / "come back": she was turned off and he wants her talking again.
_COME_BACK = re.compile(
    r"^(?:برگرد|برگردی|برگرد به\s*حرف|come\s+back|be\s+back)\s*[.!؟?]*$", re.I
)


def _come_back(folded: str) -> Intent | None:
    if _COME_BACK.match(_lead(folded)):
        return Intent(lines=("yura on",), label="persona", detail="yura on")
    return None


def _sticker(text: str) -> Intent | None:
    """A sticker request, if that is what this is."""
    line = _lead(_clean(text))
    for pattern in (_STICKER_FA, _STICKER_EN):
        match = pattern.match(line)
        if not match:
            continue
        what = (match.group("what") or "").strip(" .,!؟?")
        # "a sticker of a cat" / "a sticker with a cat" is a cat.
        what = re.sub(r"^(?:a|an|the|one|یکی|یک)\s+", "", what, flags=re.I).strip()
        if not what or what in _NOISE or len(what) > 40:
            return None
        return Intent(lines=(f"sticker {what}",), label="sticker", detail=f"sticker · {what}")
    return None


def _misc(text: str) -> Intent | None:
    """The read-only shapes: a question opener, or an order to show something.

    Every rule here is anchored at the start of the sentence on purpose. The first
    draft matched keywords anywhere, which meant "i was thinking about you" opened
    the `.about` card in the middle of a conversation. A lookup needs to *ask*.
    """
    folded = _clean(text)
    # "make a note milk" carries its own words, so it cannot be a fixed line.
    note = re.match(r"^(?:make|add|write)\s+(?:a\s+)?note\s*[:\-]?\s*(?P<body>.{2,300})$", folded)
    if note:
        body = _clean_body(note.group("body"))
        if body and body not in _NOISE:
            return Intent(lines=(f"note add {body}",), label="note", detail=f"note · {body[:40]}")
    # "cancel reminder 3"
    cancel = re.match(
        r"^(?:cancel|delete|remove|drop)\s+(?:the\s+)?(?:reminder|task)\s*#?(?P<id>\d{1,9})$",
        folded,
    )
    if cancel:
        return Intent(
            lines=(f"reminders cancel {cancel.group('id')}",),
            label="reminders",
            detail=f"cancel reminder {cancel.group('id')}",
        )
    # "schedule hi at 9am" — the words and the time, in the order he says them.
    scheduled = re.match(
        r"^(?:schedule|post|send\s+later)\s+(?P<body>.{2,200}?)\s+(?:(?:at|for|on)\s+)?"
        r"(?P<when>\d{1,2}(?::\d{2})?\s*(?:am|pm)?|tomorrow(?:\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\s*[.!]*$",
        folded,
    )
    if scheduled:
        when = re.sub(r"\s+", "", scheduled.group("when"))
        return Intent(
            lines=(f"schedule {when} {scheduled.group('body').strip()}",),
            label="schedule",
            detail=f"schedule · {when}",
        )
    rules: list[tuple[str, str]] = [
        (r"^(?:what can you do|what do you do|list (?:your )?(?:commands|features)|show (?:me )?(?:the )?(?:commands|help)|help me|^help$|کمک|دستورها)", "help"),
        (r"^(?:who are you|what are you|what is your version|your version|tell me about (?:yourself|you)\b|کی هستی|ورژن)", "about"),
        (r"^(?:status|what is your status|your status|how is the bot|وضعیت)$", "status"),
        (r"^(?:settings|show (?:me )?(?:the )?settings|تنظیمات)", "settings"),
        (r"^(?:features|list (?:the )?features|قابلیت ?ها)", "features"),
        (r"^(?:what do you remember|show (?:me )?(?:your )?memory|your memory|حافظه)", "yura memory"),
        (r"^(?:what did you do|show (?:me )?(?:your )?(?:last )?actions|your actions|کارهای اخیر)", "yura actions"),
        (r"^(?:my reminders|list (?:my )?reminders|show (?:me )?(?:my )?reminders|یادآورها)", "reminders"),
        (r"^(?:my notes|list (?:my )?notes|show (?:me )?(?:my )?notes|یادداشت‌ها)", "note list"),
        (r"^(?:agenda|my agenda|what is on today|today'?s plan|برنامه امروز)", "agenda today"),
        (r"^(?:quiet hours|ساعت سکوت)", "quiet-hours status"),
        # "فال بگیر" is the order; "فال" alone is the noun. Anchoring only the
        # noun is why she discussed a fortune instead of drawing one.
        (r"^(?:(?:برام|برای\s*من|یه|یک)\s*)*(?:fal(?:\s+(?:for\s+me|please))?"
         r"|فال(?:\s*حافظ)?(?:\s*(?:بگیر|بنداز|بیار|بزن|کن))?)\s*[.!؟?]*$", "fal"),
        (r"^(?:(?:برام|برای\s*من|یه|یک)\s*)*(?:poem|شعر(?:ی)?"
         r"(?:\s*(?:بگو|بخوان|بخون|بگیر|بزن))?)\s*[.!؟?]*$", "poem"),
        (r"^(?:(?:برام|برای\s*من|یه|یک)\s*)*(?:joke|tell me a joke|لطیفه|جوک"
         r"(?:\s*(?:بگو|بزن|تعریف\s*کن))?|یکی\s+بگو)\s*[.!؟?]*$", "joke"),
        (r"^(?:(?:برام|برای\s*من|یه|یک)\s*)*(?:quote|give me a quote|جمله"
         r"(?:\s*(?:بگو|بزن|بخوان))?)\s*[.!؟?]*$", "quote"),
        (r"^(?:say something in (?:persian|farsi|english)|حرف بزن)", "yura"),
        # The ones the audit found falling through to a shrug.
        (r"^(?:unpin)(?:\s+(?:this|that|it))?\s*[.!]*$", "unpin"),
        (r"^(?:what'?s|what\s+is|anything)\s+unread\s*[.!?]*$|^(?:unread)$", "unread"),
        (r"^(?:translate\s+(?:this|that|it)|ترجمه\s*کن|این\s*رو\s*ترجمه\s*کن)\s*[.!؟]*$", "translate"),
        (r"^(?:summarize|summarise|خلاصه\s*کن)(?:\s+(?:this|the)\s+(?:chat|group|conversation))?\s*[.!؟]*$", "summarize"),
        (r"^(?:show\s+(?:me\s+)?(?:my\s+)?chats|my\s+chats|list\s+(?:my\s+)?chats|چت\s*ها)$", "chats"),
        (r"^(?:back\s*up\s+now|run\s+a\s+backup|make\s+a\s+backup|backup\s+now)\s*[.!]*$", "backup export"),
        (r"^(?:ping|پینگ)\s*[.!]*$", "ping"),
        (r"^(?:what'?s\s+the\s+date(?:\s+today)?|today'?s\s+date|امروز\s*(?:چندمه|چنده|چند\s*امه))\s*[.!؟]*$", "time"),
        (r"^(?:bookmark\s+(?:this|that|it)|save\s+(?:this|that|it)\s+as\s+a\s+bookmark)\s*[.!]*$", "bookmark save"),
        # "از این چت خلاصه بگیر" — the Persian way of asking for a summary names
        # the chat first and the verb last.
        (r"^(?:از\s+)?(?:این\s+)?(?:چت|گروه|گفتگو|مکالمه)(?:\s+(?:رو|را))?\s*"
         r"(?:خلاصه|جمع\s*بندی)\s*(?:بگیر|بکن|کن|بده)?\s*[.!؟]*$", "summarize"),
        (r"^(?:خلاصه|جمع\s*بندی)\s*(?:این\s+)?(?:چت|گروه|گفتگو|مکالمه)"
         r"(?:\s+(?:رو|را))?\s*(?:بگیر|بکن|کن|بده)?\s*[.!؟]*$", "summarize"),
    ]
    for pattern, line in rules:
        if re.search(pattern, folded):
            return Intent(lines=(line,), label="lookup", detail=line)
    return None


def detect(
    text: str,
    *,
    features: set[str] | frozenset[str] = frozenset(),
    commands: set[str] | frozenset[str] = frozenset(),
    allow_plan: bool = True,
) -> Intent | None:
    """Read ``text`` as an instruction, or return ``None`` for conversation."""
    folded = _clean(text)
    if not folded or len(folded) > 400:
        return None
    known = {name for name in set(commands) | set(features) if name and name.isascii()}

    reminder = _reminder(folded)
    if reminder is not None:
        return reminder

    knowledge = _knowledge(folded)
    if knowledge is not None:
        return knowledge

    reached = _reach(text, allow_plan=allow_plan)
    if reached is not None:
        return reached

    surface = _surface(text)
    if surface is not None:
        return surface

    # Quiet hours is a clock-shaped sentence that is not about the clock: "ساعت
    # سکوت رو روشن کن" used to switch the live clock on, which is a different
    # feature answering to a word inside another one's name.
    quiet = _quiet_hours(folded)
    if quiet is not None:
        return quiet
    for early in (_time_ask, _capability_ask, _come_back):
        found = early(folded)
        if found is not None:
            return found

    # Clock is the one the owner says most, and it has its own subcommands. It is
    # also the one that must not misfire: "do you like the clock on my profile?"
    # is a question, and "on" in it is not a switch.
    if re.search(r"\bclock\b|\bساعت\b", folded):
        # A question about the clock is a question: "ساعت روشنه؟" asks, it does not
        # order. Read that before the on/off verbs, which would otherwise match the
        # word روشن inside it.
        if re.search(r"[؟?]\s*$", text or "") and re.search(r"ساعت.*(?:روشن|خاموش)|(?:روشن|خاموش).*ساعت", folded):
            return Intent(lines=("clock status",), label="clock", detail="clock status")
        if re.search(
            r"(?:turn|switch|shut|take|put)\w*\b.*\boff\b"
            r"|\bdisable\b|\bstop the clock\b|\bclock off\b"
            r"|خاموش|بردار|حذف کن|ببند",
            folded,
        ):
            return Intent(lines=("clock off",), label="clock", detail="clock off")
        if re.search(
            r"(?:turn|switch|put)\w*\b.*\b(?:back\s+)?on\b"
            r"|\benable\b|\bbring (?:it|the clock) back\b|\bshow (?:it|the clock)\b"
            r"|روشن|برگردون|نشان بده",
            folded,
        ):
            return Intent(lines=("clock on",), label="clock", detail="clock on")
        if re.search(
            r"\b(?:clock )?(?:status|state)\b|is the clock (?:on|off|working)\b"
            r"|\bshow me the clock\b|وضعیت\s*ساعت|ساعت (?:روشن است|روشنه|خاموشه)\b",
            folded,
        ) or (folded.rstrip(" ؟?") != folded and re.search(r"ساعت.*(?:روشن|خاموش)", folded)):
            return Intent(lines=("clock status",), label="clock", detail="clock status")

    acted = _actions(folded)
    if acted is not None:
        return acted

    utility = _utility(folded)
    if utility is not None:
        return utility

    # "turn off X and Y" / "X off" / "X رو خاموش کن"
    for pattern, on in ((_OFF_LEAD, False), (_ON_LEAD, True), (_OFF_TAIL, False), (_ON_TAIL, True)):
        for match in pattern.finditer(folded):
            intent = _switch_list(match.group("thing"), on=on, known=known, features=set(features))
            if intent is not None:
                return intent
    for pattern, on in ((_SHORT_OFF, False), (_SHORT_ON, True)):
        match = pattern.match(folded)
        if match:
            intent = _switch_list(match.group("thing"), on=on, known=known, features=set(features))
            if intent is not None:
                return intent

    if re.search(r"\b(turn|switch|shut)\b.*\b(off|on)\b", folded):
        # A switch verb with no nameable target is a sentence about switching, not
        # a request — never guess.
        return None

    sticker = _sticker(text)
    if sticker is not None:
        return sticker

    # The vocative is not part of the sentence: "یورا فال بگیر" asks for a
    # fortune exactly like "فال بگیر" does.
    return _misc(_lead(folded))
