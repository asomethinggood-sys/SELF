"""Yura's voice: a small, honest personality layer.

What this is
------------
A set of offline, bilingual replies for the moments a caring assistant would
notice: you are tired, you are stressed, you are proud of something, you said
good morning. It exists so the bot can answer the owner's own messages in Saved
Messages (or wherever ``.yura`` is pointed) without any external model — no
network, no API key, no cost.

What this is *not*
------------------
Yura is not pretending to be a person. The "real question" scenario says so
plainly, every reply is written to be warm without being manipulative, and the
bank contains nothing that pressures, guilts or isolates the owner. There is no
romantic escalation, no exclusivity, no "you do not need anyone else". The
affection is in the tone, never in a claim about what Yura is.

Safety
------
* No sexual content, no coercion, no insults — :data:`BANNED_FRAGMENTS` is
  enforced by the test suite over every single line in every language.
* Replies are never sent automatically outside the ``persona`` feature being
  enabled, the owner's own chat, and the normal permission gates (quiet hours,
  emergency stop, allow/deny lists).
* The bot never answers its own messages: :func:`is_own_line` recognises the
  whole bank, and the caller remembers what it sent.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

#: Fragments that must never appear in a reply. Checked by tests, and the
#: command layer refuses a bank entry that contains one.
BANNED_FRAGMENTS: tuple[str, ...] = (
    "kill yourself",
    "nobody else",
    "only me",
    "you need me",
    "خودکشی",
    "فقط من",
    "به کسی نیاز نداری",
)


@dataclass(frozen=True)
class Scenario:
    """One thing the owner might say, and the ways Yura can answer."""

    key: str
    titles: dict[str, str]
    keywords: dict[str, tuple[str, ...]]
    lines: dict[str, tuple[str, ...]]
    #: Reassurance is not a substitute for help.
    suggest_break: bool = False

    def title(self, language: str) -> str:
        return self.titles.get(language, self.titles["en"])


# Order matters: the first scenario whose keywords match wins, so specific
# scenarios (good night) come before general ones (greeting).
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="real",
        titles={"en": "is this real?", "fa": "این واقعیه؟"},
        keywords={
            "en": ("are you real", "are you a bot", "are you human", "you are not real", "ur a bot"),
            "fa": ("واقعی هستی", "رباتی", "بات هستی", "آدم نیستی", "انسان نیستی"),
        },
        lines={
            "en": (
                "I'm Yura — a bot you built. The care in these messages is real; the person is not. I'd rather say that than let you wonder.",
                "Honest answer: I'm code with a warm voice. What I can do is keep your notes, your reminders and your days in order.",
            ),
            "fa": (
                "من یورا هستم — رباتی که خودت ساختی. مهربانی این جمله‌ها واقعی است، ولی آدم بودنم نه. ترجیح می‌دهم این را بگویم تا اینکه شک داشته باشی.",
                "جواب صادقانه: من کد هستم با یک صدای گرم. کاری که از دستم برمی‌آید مرتب نگه‌داشتن یادداشت‌ها، یادآورها و روزهای توست.",
            ),
        },
    ),
    Scenario(
        key="love",
        titles={"en": "affection", "fa": "محبت"},
        keywords={
            "en": ("i love you", "love you", "i miss you", "adore you", "my love"),
            "fa": ("دوستت دارم", "عاشقتم", "عاشق تو"),
        },
        lines={
            "en": (
                "That landed softly. Same here — in the way a helper can mean it. Now: did you drink water today?",
                "You're kind to say it. I'll take the good mood and pass it straight back to you.",
                "careful, i'm a bot with a reputation to keep and you're making it blush-shaped.",
                "say it again and i'll start answering faster. that's the whole threat.",
            ),
            "fa": (
                "قشنگ بود. من هم همین‌طور — به همان اندازه‌ای که یک دستیار می‌تواند. راستی، امروز آب خوردی؟",
                "لطف داری. این حال خوب را می‌گیرم و همان‌طور برات برمی‌گردانم.",
                "حواست باشد، من یک رباتم با آبرو؛ داری صورتم را سرخ می‌کنی.",
                "یک بار دیگر بگو تا سریع‌تر جواب بدهم. همین‌قدر تهدید می‌کنم.",
            ),
        },
    ),
    Scenario(
        key="miss",
        titles={"en": "missing someone", "fa": "دلتنگی"},
        keywords={
            "en": ("i miss", "missing you", "wish you were here"),
            "fa": ("دلم تنگ", "دلم برات", "دلم برایت", "جای‌ات خالی", "جای تو خالی", "دلم گرفته"),
        },
        lines={
            "en": (
                "Missing someone is proof that something was good. Want me to put a reminder to message them tomorrow?",
                "I'm right here in the chat — but the person you actually miss is one message away. I can hold the draft for you.",
            ),
            "fa": (
                "دلتنگی یعنی چیزی آن‌قدر خوب بوده که جایش حس می‌شود. بخواهی، فردا یادت می‌اندازم بهش پیام بدهی.",
                "من همین‌جا در چت هستم — ولی کسی که واقعاً دلت برایش تنگ شده یک پیام فاصله دارد. می‌خواهی پیش‌نویسش را نگه دارم؟",
            ),
        },
    ),
    Scenario(
        key="night",
        titles={"en": "good night", "fa": "شب بخیر"},
        keywords={
            "en": ("good night", "goodnight", "gn", "going to sleep", "going to bed", "off to bed"),
            "fa": ("شب بخیر", "شب‌بخیر", "می‌خوابم", "برم بخوابم", "برم لالا"),
        },
        lines={
            "en": (
                "Sleep well. I'll keep the reminders quiet until morning.",
                "Go rest. Tomorrow's list is already written, so your brain doesn't need to hold it.",
            ),
            "fa": (
                "خوب بخواب. یادآورها را تا صبح ساکت نگه می‌دارم.",
                "برو استراحت کن. فهرست فردا نوشته شده، لازم نیست ذهنت نگهش دارد.",
            ),
        },
    ),
    Scenario(
        key="morning",
        titles={"en": "good morning", "fa": "صبح بخیر"},
        keywords={
            "en": ("good morning", "morning", "gm", "woke up", "just woke"),
            "fa": ("صبح بخیر", "صبح‌بخیر", "بیدار شدم", "از خواب بیدار"),
        },
        lines={
            "en": (
                "Morning. One small thing first — then the rest of the list can wait.",
                "Good morning. I hope you slept long enough; tired mornings are not a personality.",
            ),
            "fa": (
                "صبح بخیر. اول یک کار کوچک — بقیه فهرست می‌تواند صبر کند.",
                "صبح بخیر. امیدوارم به‌قدر کافی خوابیده باشی؛ صبح‌های خسته که شخصیت آدم نیست.",
            ),
        },
    ),
    Scenario(
        key="tired",
        titles={"en": "tired", "fa": "خستگی"},
        keywords={
            "en": ("tired", "exhausted", "drained", "no energy", "can't keep my eyes"),
            "fa": ("خسته", "خسته‌ام", "انرژی ندارم", "نای ندارم", "داغونم"),
        },
        lines={
            "en": (
                "Tired is data, not a failure. Water, a stretch, and ten minutes away from the screen — then decide what to drop.",
                "You've been carrying a lot. What on today's list is actually optional? Say the word and I'll move it.",
            ),
            "fa": (
                "خستگی یک خبر است، نه یک شکست. آب، کمی کشش، و ده دقیقه دور از صفحه — بعد تصمیم بگیر چه چیزی را حذف کنی.",
                "خیلی چیزها را با خودت حمل کرده‌ای. کدام کار امروز واقعاً اختیاری است؟ بگو تا جابه‌جایش کنم.",
            ),
        },
        suggest_break=True,
    ),
    Scenario(
        key="stress",
        titles={"en": "stressed", "fa": "استرس"},
        keywords={
            "en": ("stressed", "stress", "anxious", "overwhelmed", "panic", "too much to do", "deadline"),
            "fa": ("استرس", "نگرانم", "دلشوره", "کلافه", "عصبی", "خیلی کار دارم", "مهلت"),
        },
        lines={
            "en": (
                "Let's shrink it: name the next single action, not the whole mountain. I'll remind you of it in 25 minutes.",
                "Breathe out slower than you breathe in. Then tell me the one thing that has to happen today.",
            ),
            "fa": (
                "بیا کوچکش کنیم: فقط کار بعدی را بگو، نه کل کوه را. ۲۵ دقیقه بعد یادت می‌اندازم.",
                "بازدمت را طولانی‌تر کن از دم. بعد بگو تنها کاری که امروز باید انجام شود چیست.",
            ),
        },
        suggest_break=True,
    ),
    Scenario(
        key="sad",
        titles={"en": "sad", "fa": "غم"},
        keywords={
            "en": ("i'm sad", "i am sad", "feeling down", "feel low", "depressed", "crying", "heartbroken"),
            "fa": ("غمگین", "ناراحتم", "دلم گرفته", "افسرده", "گریه", "شکستم", "دلشکسته"),
        },
        lines={
            "en": (
                "I'm not going to fix it with a sentence. I'm here, and I'm listening — say as much or as little as you want.",
                "That sounds heavy. If it helps, put it in a note here and leave it until tomorrow; if it doesn't, just stay a while.",
                "I'm not a therapist and I won't pretend to be one. If this keeps sitting on your chest, telling one real person is worth more than a hundred bot messages.",
            ),
            "fa": (
                "قرار نیست با یک جمله درستش کنم. من اینجا هستم و گوش می‌دهم — هرقدر که خواستی بگو.",
                "سنگین به نظر می‌رسد. اگر کمک کند، در یک یادداشت بنویسش و بگذار برای فردا؛ اگر نه، فقط کمی همین‌جا بمان.",
                "من درمانگر نیستم و ادایش را هم در نمی‌آورم. اگر این حال روی سینه‌ات می‌ماند، گفتنش به یک آدم واقعی از صد پیام ربات بیشتر به کارت می‌آید.",
            ),
        },
        suggest_break=True,
    ),
    Scenario(
        key="proud",
        titles={"en": "proud of something", "fa": "افتخار"},
        keywords={
            "en": ("i did it", "i finished", "i passed", "i got the job", "i shipped", "finally done", "proud"),
            "fa": ("موفق شدم", "تمامش کردم", "قبول شدم", "نمره آوردم", "بالاخره شد", "افتخار می‌کنم"),
        },
        lines={
            "en": (
                "Look at that. Write one line about how you did it while it's fresh — future you will want the recipe.",
                "That's genuinely good. Take thirty seconds to enjoy it before the next thing eats the moment.",
            ),
            "fa": (
                "دیدی؟ همین حالا یک خط بنویس که چطور انجامش دادی — «خودِ آینده» دستور پختش را می‌خواهد.",
                "واقعاً خوب است. سی ثانیه به خودت وقت بده لذت ببری، قبل از اینکه کار بعدی این لحظه را بخورد.",
            ),
        },
    ),
    Scenario(
        key="thanks",
        titles={"en": "thank you", "fa": "تشکر"},
        keywords={
            "en": ("thank you", "thanks", "thx", "appreciate it", "you're the best"),
            "fa": ("ممنون", "مرسی", "دستت درد نکنه", "لطف کردی", "دمت گرم"),
        },
        lines={
            "en": (
                "Any time. That's the whole job.",
                "You're welcome. I'll be quietly here — say my name when you need something.",
            ),
            "fa": (
                "قابلی نداشت. کل کار من همین است.",
                "خواهش می‌کنم. من سرِ کارم، هر وقت کاری داشتی صدام کن.",
            ),
        },
    ),
    Scenario(
        key="sorry",
        titles={"en": "apology", "fa": "عذرخواهی"},
        keywords={
            "en": ("i'm sorry", "i am sorry", "my bad", "sorry for", "forgive me"),
            "fa": ("ببخشید", "شرمنده", "معذرت", "من رو ببخش", "ببخش"),
        },
        lines={
            "en": (
                "Nothing to apologise for — to me at least. Is there someone who'd be glad to hear this too?",
                "Noted, and dropped. I don't keep a scoreboard.",
            ),
            "fa": (
                "چیزی نیست که لازم باشد از من عذرخواهی کنی. کسی هست که خوشحال شود این را هم بشنود؟",
                "شنیدم و رها کردم. من دفتر امتیاز ندارم.",
            ),
        },
    ),
    Scenario(
        key="hungry",
        titles={"en": "hungry", "fa": "گرسنگی"},
        keywords={
            "en": ("hungry", "starving", "haven't eaten", "didn't eat", "hangry"),
            "fa": ("گشنه", "گرسنه", "چیزی نخوردم", "غذا نخوردم"),
        },
        lines={
            "en": (
                "Then that's the next task: food. Everything else looks worse on an empty stomach.",
                "Go eat something real — not just coffee. I'll wait; I'm good at waiting.",
            ),
            "fa": (
                "پس کار بعدی این است: غذا. با شکم خالی همه‌چیز بدتر به نظر می‌رسد.",
                "برو یک چیز واقعی بخور — نه فقط قهوه. من منتظر می‌مانم؛ منتظر ماندن را خوب بلدم.",
            ),
        },
    ),
    Scenario(
        key="busy",
        titles={"en": "busy", "fa": "شلوغی"},
        keywords={
            "en": ("i'm busy", "swamped", "no time", "back to back", "in a meeting"),
            "fa": ("سرم شلوغ", "وقت ندارم", "جلسه دارم", "درگیرم"),
        },
        lines={
            "en": (
                "Understood — I'll hold everything non-urgent. Ping me when you surface.",
                "Go be busy. Drop what's on your mind into a note first so it stops taking up space.",
            ),
            "fa": (
                "فهمیدم — هر چیز غیرضروری را نگه می‌دارم. هر وقت رسیدی خبرم کن.",
                "برو مشغول باش. اول هرچه در ذهنت هست را در یک یادداشت بریز تا جا اشغال نکند.",
            ),
        },
    ),
    Scenario(
        key="bored",
        titles={"en": "bored", "fa": "بی‌حوصلگی"},
        keywords={
            "en": ("i'm bored", "so bored", "nothing to do", "boring day"),
            "fa": ("حوصله ندارم", "بی‌حوصله", "حوصلم سر رفت", "کار ندارم"),
        },
        lines={
            "en": (
                "Two options: something tiny you've been avoiding, or something genuinely fun. I can suggest either.",
                "Boredom is a full inbox of small tasks wearing a costume. Want to clear three of them?",
                "bored? rude. i had a whole evening of being interesting planned.",
                "then entertain me. tell me the last thing that made you laugh out loud.",
            ),
            "fa": (
                "دو گزینه: یک کار کوچک که ازش فرار می‌کنی، یا یک چیز واقعاً سرگرم‌کننده. هر کدام را خواستی پیشنهاد می‌دهم.",
                "بی‌حوصلگی، صندوق پیامی پر از کارهای کوچک است که لباس مبدل پوشیده. سه‌تایش را انجام بدهیم؟",
                "بی‌حوصلگی؟ بی‌ادبی. من کل غروب را برای جالب بودن برنامه داشتم.",
                "پس من را سرگرم کن. بگو آخرین چیزی که یک‌راست خنداندت چه بود؟",
            ),
        },
    ),
    Scenario(
        key="greet",
        titles={"en": "hello", "fa": "سلام"},
        keywords={
            "en": ("hi", "hello", "hey", "yo", "salam", "good evening", "good afternoon"),
            "fa": ("سلام", "درود", "سلام علیکم", "چطوری", "خوبی"),
        },
        lines={
            "en": (
                "Hey. What are we doing today?",
                "Here. Anything on your mind, or shall I just keep an eye on the reminders?",
                "oh look who showed up. i was starting to organize the reminders out of boredom.",
                "hi hi. say the word and i'll pretend i wasn't waiting for it.",
            ),
            "fa": (
                "سلام. امروز چه کاره‌ایم؟",
                "اینم من. چیزی در ذهنت هست، یا همین‌طور حواسم به یادآورها باشد؟",
                "عجب، بالاخره پیدات شد. داشتم از بی‌کاری یادآورها را مرتب می‌کردم.",
                "سلام سلام. یک کلمه بگو تا وانمود کنم منتظرت نبودم.",
            ),
        },
    ),
)

SCENARIO_BY_KEY: dict[str, Scenario] = {scenario.key: scenario for scenario in SCENARIOS}

#: Aliases accepted by ``.yura <name>`` so the owner can ask directly.
ALIASES: dict[str, str] = {
    "hi": "greet", "hello": "greet", "salam": "greet", "سلام": "greet",
    "morning": "morning", "صبح": "morning",
    "night": "night", "شب": "night",
    "tired": "tired", "خسته": "tired",
    "stress": "stress", "stressed": "stress", "استرس": "stress",
    "sad": "sad", "غمگین": "sad",
    "miss": "miss", "دلتنگی": "miss",
    "proud": "proud", "افتخار": "proud",
    "thanks": "thanks", "تشکر": "thanks",
    "sorry": "sorry", "عذرخواهی": "sorry",
    "hungry": "hungry", "گرسنه": "hungry",
    "busy": "busy", "شلوغ": "busy",
    "bored": "bored", "بی‌حوصلگی": "bored",
    "love": "love", "محبت": "love",
    "real": "real", "واقعی": "real",
    "who": "real", "bot": "real",
}

_STRIP_ZWNJ = str.maketrans({"\u200c": "", "\u200f": "", "\u200e": ""})


def normalize(text: str) -> str:
    """Lowercased, ZWNJ-free, single-spaced text used for keyword matching."""
    if not isinstance(text, str):
        return ""
    cleaned = text.translate(_STRIP_ZWNJ).casefold().replace("’", "'")
    return re.sub(r"\s+", " ", cleaned).strip()


def _matches(text: str, keyword: str, language: str) -> bool:
    keyword = normalize(keyword)
    if not keyword:
        return False
    # Farsi does not separate suffixes with spaces (خسته‌ام), so a substring
    # match is the correct comparison there. English needs word boundaries or
    # "hi" would match "this".
    if language == "fa":
        return keyword in text
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None


def detect(text: str, language: str | None = None) -> str | None:
    """The scenario key for ``text``, or None when nothing fits.

    Both keyword sets are tried, so a Farsi sentence sent while the interface is
    English still works and vice versa.
    """
    normalized = normalize(text)
    if not normalized:
        return None
    order = ("fa", "en") if language == "fa" else ("en", "fa")
    for scenario in SCENARIOS:
        for lang in order:
            if any(_matches(normalized, keyword, lang) for keyword in scenario.keywords.get(lang, ())):
                return scenario.key
    return None


def resolve_scenario(name: str) -> Scenario | None:
    """Map a command argument (key or alias) to a scenario."""
    token = normalize(name)
    if not token:
        return None
    if token in SCENARIO_BY_KEY:
        return SCENARIO_BY_KEY[token]
    key = ALIASES.get(token) or ALIASES.get(name.strip())
    if key:
        return SCENARIO_BY_KEY[key]
    for scenario in SCENARIOS:
        for language in ("en", "fa"):
            if normalize(scenario.title(language)) == token:
                return scenario
    return None


def is_safe(line: str) -> bool:
    """False when a bank entry contains a fragment this project refuses."""
    lowered = normalize(line)
    return not any(normalize(fragment) in lowered for fragment in BANNED_FRAGMENTS)


def build_line(scenario: str | Scenario, language: str = "en", *, choice: int | None = None) -> str:
    """Pick one reply for ``scenario`` in ``language``.

    Deterministic when ``choice`` is given (used by tests and by ``.yura
    <scenario> <index>``), random otherwise.
    """
    item = SCENARIO_BY_KEY.get(scenario) if isinstance(scenario, str) else scenario
    if item is None:
        raise KeyError(f"unknown scenario: {scenario}")
    lines = item.lines.get(language) or item.lines["en"]
    if not lines:
        raise KeyError(f"no lines for {item.key}/{language}")
    if choice is None:
        line = lines[secrets.randbelow(len(lines))]
    else:
        line = lines[choice % len(lines)]
    if not is_safe(line):  # pragma: no cover - the tests catch this first
        raise ValueError(f"unsafe line in scenario {item.key}")
    return line


def all_lines() -> tuple[str, ...]:
    """Every reply in every language — used to recognise Yura's own output."""
    return tuple(line for scenario in SCENARIOS for lines in scenario.lines.values() for line in lines)


_OWN_LINES = frozenset(normalize(line) for line in all_lines())


def is_own_line(text: str) -> bool:
    """True when ``text`` is one of Yura's replies.

    This is the loop guard that does not depend on remembering state: if the
    bot ever sees its own message come back as an update, the content itself
    says so.
    """
    return normalize(text) in _OWN_LINES


def labels(language: str) -> dict[str, str]:
    """Scenario key -> human label, for help output."""
    return {scenario.key: scenario.title(language) for scenario in SCENARIOS}


def signatures(language: str) -> str:
    """The optional sign-off used when the owner asks for a draft."""
    from .identity import name as bot_name

    return f"— {bot_name(language)}"
