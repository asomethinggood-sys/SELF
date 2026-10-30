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
from dataclasses import dataclass, field

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


def _misc(text: str) -> Intent | None:
    """The read-only shapes: a question opener, or an order to show something.

    Every rule here is anchored at the start of the sentence on purpose. The first
    draft matched keywords anywhere, which meant "i was thinking about you" opened
    the `.about` card in the middle of a conversation. A lookup needs to *ask*.
    """
    folded = _clean(text)
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
        (r"^(?:fal|فال)$", "fal"),
        (r"^(?:poem|شعر)$", "poem"),
        (r"^(?:joke|tell me a joke|لطیفه|جوک)", "joke"),
        (r"^(?:quote|give me a quote|جمله)", "quote"),
        (r"^(?:say something in (?:persian|farsi|english)|حرف بزن)", "yura"),
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

    return _misc(folded)
