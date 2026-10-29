"""Offline content collections: jokes, quotes and Fight Mode templates.

Everything here works with zero network access and is bilingual. AI is an
optional enhancement, never a requirement (spec §2, feature areas 27 and 67).

Fight Mode rules encoded in the templates and enforced by the command layer:
no threats, no slurs, no private information, no harassment of third parties —
only light, contextual wordplay aimed at the replied-to message.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

JOKES: dict[str, tuple[str, ...]] = {
    "en": (
        "I told my computer I needed a break. It said: 'No problem, I'll go to sleep.' Now it won't wake up.",
        "Why do programmers prefer dark mode? Because light attracts bugs.",
        "My password is the last eight digits of pi. Good luck guessing which eight.",
        "I would tell you a UDP joke, but you might not get it.",
        "A SQL query walks into a bar, approaches two tables and asks: 'May I join you?'",
        "There are two hard things in computing: cache invalidation, naming things, and off-by-one errors.",
        "I asked my phone to be more honest. Now it just says 'low battery' whenever I open social media.",
        "Debugging: being the detective in a crime movie where you are also the murderer.",
        "My code doesn't have bugs — it has randomly generated features.",
        "I finally understood recursion. I finally understood recursion.",
        "A byte walks into a bar looking down. The bartender asks, 'What's wrong?' It says, 'I lost a bit.'",
        "I wanted to make a joke about the cloud, but it's too foggy up there.",
    ),
    "fa": (
        "به کامپیوترم گفتم خسته‌ام گفت: «من هم، بریم بخوابیم.» الان هر دو بیدار نمی‌شیم.",
        "چرا برنامه‌نویس‌ها شب‌کار می‌کنند؟ چون باگ‌ها از نور فرار می‌کنند.",
        "رمز عبورم هشت رقم آخر عدد پی است. حدس بزن کدام هشت رقم!",
        "می‌خواستم یک جوک UDP تعریف کنم، ولی ممکنه به دستت نرسه.",
        "یک کوئری SQL وارد کافه شد، به دو جدول نگاه کرد و گفت: «اجازه هست بهتون join بشم؟»",
        "دو کار سخت در برنامه‌نویسی هست: نام‌گذاری چیزها، و خطای off-by-one.",
        "از گوشی‌ام خواستم صادق‌تر باشد؛ حالا هر بار شبکه اجتماعی را باز می‌کنم می‌گوید «باتری کم است».",
        "دیباگ کردن یعنی کارآگاه یک فیلم جنایی باشی که خودت هم قاتلی.",
        "کدم باگ ندارد؛ امکانات تصادفی دارد.",
        "بالاخره بازگشت را فهمیدم. بالاخره بازگشت را فهمیدم.",
        "یک بایت با غم وارد کافه شد. باریستا پرسید: «چی شده؟» گفت: «یک بیت گم کردم.»",
        "می‌خواستم درباره ابر شوخی کنم، ولی هوا آن بالا خیلی مه‌آلود بود.",
    ),
}

QUOTES: dict[str, tuple[str, ...]] = {
    "en": (
        "Small steps, repeated, beat big plans abandoned.",
        "The tool you understand is faster than the tool you admire.",
        "You do not need more time; you need fewer open loops.",
        "Write it down. A memory is a rumor, a note is evidence.",
        "Slow is smooth, smooth is fast.",
        "If it is not tested, it is not finished.",
        "Attention is the only currency that cannot be borrowed.",
        "Say less, mean more.",
        "Finish one thing before starting the next. Momentum is compound interest.",
        "A calm reply is a competitive advantage.",
    ),
    "fa": (
        "قدم‌های کوچک و تکرارشده، از نقشه‌های بزرگ رهاشده قوی‌ترند.",
        "ابزاری که می‌فهمی، از ابزاری که تحسین می‌کنی سریع‌تر است.",
        "به زمان بیشتر نیاز نداری؛ به کارهای نیمه‌تمام کمتر نیاز داری.",
        "بنویسش. حافظه شایعه است، یادداشت مدرک.",
        "آرام باش، پیوسته باش؛ پیوستگی سریع‌تر می‌رساند.",
        "اگر آزمایش نشده، تمام نشده.",
        "توجه تنها سرمایه‌ای است که قرض نمی‌شود.",
        "کمتر بگو، بیشتر معنا کن.",
        "یکی را تمام کن، بعد سراغ بعدی برو. پیشرفت، سود مرکب است.",
        "پاسخ آرام، یک مزیت رقابتی است.",
    ),
}

FIGHT_STYLES: dict[str, tuple[str, ...]] = {
    "sarcastic": (
        "Wow, {hook} — did you workshop that or did it escape?",
        "I would argue, but {hook} is doing all the heavy lifting already.",
        "That was almost a point. It got lost on the way.",
    ),
    "dry": (
        "Noted. {hook}.",
        "Bold claim. Zero evidence. Classic.",
        "I have read worse. Once. In a fortune cookie.",
    ),
    "clever": (
        "{hook}, which is a creative way of saying you ran out of arguments.",
        "If confidence were evidence, {hook} would be a thesis.",
        "You brought {hook} to a logic fight. Brave.",
    ),
    "playful": (
        "Okay okay, {hook} — but I still like my version better ◡",
        "Careful, {hook}; I am starting to enjoy this.",
        "That almost made me change my mind. Almost. ◡",
    ),
    "dramatic": (
        "And then, out of nowhere: {hook}. The crowd gasped.",
        "History will remember this moment. Mostly for {hook}.",
        "You have chosen chaos. I respect it, from a safe distance.",
    ),
    "blunt": (
        "That is not an argument. That is {hook}.",
        "Hard no. Try again with facts.",
        "You are describing {hook}. We were talking about the actual point.",
    ),
}

FIGHT_STYLES_FA: dict[str, tuple[str, ...]] = {
    "sarcastic": (
        "وای، {hook} — این جمله رو خودت ساختی یا از جایی فرار کرد؟",
        "می‌خواستم بحث کنم، ولی {hook} همه کار رو انجام داد.",
        "نزدیک بود یه نکته بگی. تو راه گم شد.",
    ),
    "dry": (
        "ثبت شد. {hook}.",
        "ادعای بزرگ، صفر مدرک. کلاسیک.",
        "بدترش رو هم خوندم. یه بار. توی کلوچه شانس.",
    ),
    "clever": (
        "{hook} — راه خلاقانه‌ای برای گفتن اینکه استدلال تموم شده.",
        "اگر اعتماد به نفس مدرک بود، {hook} یک پایان‌نامه می‌شد.",
        "با {hook} اومدی سر بحث منطق. شجاعانه.",
    ),
    "playful": (
        "باشه باشه، {hook} — ولی من هنوز نسخه خودم رو بیشتر دوست دارم ◡",
        "حواسم باشه، {hook}؛ دارم کیف می‌کنم.",
        "نزدیک بود نظرم عوض شه. نزدیک. ◡",
    ),
    "dramatic": (
        "و ناگهان: {hook}. جمعیت نفسش حبس شد.",
        "تاریخ این لحظه رو یادش می‌مونه. بیشتر به‌خاطر {hook}.",
        "هرج‌ومرج رو انتخاب کردی. از فاصله امن احترام می‌گذارم.",
    ),
    "blunt": (
        "این استدلال نیست. این {hook} است.",
        "نه. با دلیل دوباره بیا.",
        "داری {hook} رو توصیف می‌کنی؛ حرف سر اصل مطلب بود.",
    ),
}

STYLE_ALIASES: dict[str, str] = {
    "sarcastic": "sarcastic", "sarcasm": "sarcastic", "کنایه": "sarcastic", "طعنه": "sarcastic",
    "dry": "dry", "خشک": "dry",
    "clever": "clever", "smart": "clever", "هوشمند": "clever", "زیرکانه": "clever",
    "playful": "playful", "fun": "playful", "بازیگوش": "playful", "شوخ": "playful",
    "dramatic": "dramatic", "دراماتیک": "dramatic", "احساسی": "dramatic",
    "blunt": "blunt", "direct": "blunt", "رک": "blunt", "صریح": "blunt",
}

INTENSITY_LABELS: dict[str, dict[str, str]] = {
    "en": {"low": "gentle", "medium": "spirited", "high": "sharp"},
    "fa": {"low": "ملایم", "medium": "پرانرژی", "high": "تیز"},
}

#: Words that make a comeback about words, not about a person. When the replied
#: message already contains one of these, HAVOC answers calmly instead of
#: mirroring the insult — repetition is how a "fight" escalates.
INSULT_MARKERS: tuple[str, ...] = (
    "idiot", "stupid", "dumb", "moron", "loser", "clown", "pathetic", "ugly",
    "shut up", "nobody cares",
    "احمق", "خنگ", "نفهم", "بی‌شعور", "بیشعور", "کودن", "دسته‌گل", "بیشعور",
)

#: Calm, non-escalating answers used when the incoming message is an insult.
DEFUSE_LINES: dict[str, tuple[str, ...]] = {
    "en": (
        "Noted. I would rather keep the conversation, not win it.",
        "Strong words for someone with a weak argument. What is the actual point?",
        "I will skip the labels and answer the question underneath them.",
        "Opinions about me are free; the argument still needs evidence.",
    ),
    "fa": (
        "ثبت شد. ترجیح می‌دهم گفتگو را نگه دارم، نه اینکه برنده شوم.",
        "جمله تند است، استدلال نه. اصل مطلب چیست؟",
        "از برچسب‌ها می‌گذرم و به سؤال زیرشان جواب می‌دهم.",
        "نظر درباره من آزاد است؛ استدلال هنوز مدرک می‌خواهد.",
    ),
}

#: Filler removed before picking the "hook" keyword of a message.
STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        """a an and are as at be been but by can could did do does for from had has
        have he her him his how i if in into is it its me my no not of on or our
        she so than that the their them then there these they this to too was we
        were what when where which who why will with you your just really very""".split()
    ),
    "fa": frozenset(
        """و از به با که در را این آن است بود هست هم نه بله من تو او ما شما آنها
        برای تا بر یا اگر چه چیزی کی کجا وقتی پس ولی اما خیلی یک دو""".split()
    ),
}


#: Words that make a "fight" a real conflict. HAVOC refuses these targets.
HARASSMENT_MARKERS: tuple[str, ...] = (
    "kill", "die", "death threat", "hurt you", "beat you", "slur",
    "بکش", "مرگت", "تهدید", "لتت", "می‌زنمت",
)


@dataclass(frozen=True)
class Comeback:
    text: str
    style: str
    language: str
    intensity: str
    source: str  # "offline" | "ai"


def random_joke(language: str = "en") -> str:
    pool = JOKES.get(language) or JOKES["en"]
    return secrets.choice(pool)


def random_quote(language: str = "en") -> str:
    pool = QUOTES.get(language) or QUOTES["en"]
    return secrets.choice(pool)


def random_roast(language: str = "en", hook: str = "") -> str:
    """One roast, optionally aimed at a word from the replied-to message."""
    pool = ROASTS.get(language) or ROASTS["en"]
    template = secrets.choice(pool)
    safe_hook = (hook or ("this" if language == "en" else "این")).strip() or "this"
    # Quoting the echoed word keeps a one-word hook ("wrong") from reading as a
    # typo: it is visibly *their* word, handed back.
    quoted = f"«{safe_hook}»" if language == "fa" else f"“{safe_hook}”"
    return template.replace("{hook}", quoted)


def normalize_style(style: str | None, language: str = "en") -> str:
    if not style:
        return "clever" if language == "en" else "clever"
    return STYLE_ALIASES.get(style.strip().lower(), "clever")


#: Evaluative adjectives are what an argument is *about* only by accident: a hook
#: of "better" or "wrong" makes a comeback read like a typo. They are common
#: enough in disagreement that they belong in the stop list, not the echo.
_GENERIC_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "better best worse worst good bad right wrong great nice dumb smart "
        "always never actually literally honestly obviously clearly basic".split()
    ),
    "fa": frozenset(
        "بهتر بهترین بدتر بد خوب درست غلط اشتباه حتما واقعا اصلا معلوم ساده".split()
    ),
}


def extract_hook(text: str, language: str = "en") -> str:
    """Pick the one word worth echoing, never the whole offending sentence.

    Echoing the full message back at somebody repeats an insult verbatim; a
    short topic word keeps the reply about the subject instead of the person.
    """
    stopwords = STOPWORDS.get(language, STOPWORDS["en"]) | _GENERIC_WORDS.get(language, frozenset())
    words = []
    for raw in (text or "").split():
        word = raw.strip(",.!?;:\"'()[]{}«»…—–-*_~/")
        if not word or word.startswith(("@", "#", "http")):
            continue
        if word.lower() in stopwords or len(word) < 3:
            continue
        words.append(word)
    if not words:
        return "that" if language == "en" else "این"
    # Prefer a concrete, mid-length word: neither "ok" nor an endless compound.
    words.sort(key=lambda w: (abs(len(w) - 6), -len(w)))
    hook = words[0].strip()
    return hook[:24].rstrip() if len(hook) > 24 else hook


def is_insult(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in INSULT_MARKERS)


def build_offline_comeback(
    *,
    hook: str,
    style: str = "clever",
    intensity: str = "medium",
    language: str = "en",
    source_text: str | None = None,
) -> Comeback:
    """Compose a comeback from local templates — always available offline."""
    style = normalize_style(style, language)
    if source_text and is_insult(source_text):
        # Never mirror an insult. Answer the argument, not the label.
        pool = DEFUSE_LINES.get(language) or DEFUSE_LINES["en"]
        text = secrets.choice(pool)
        if intensity == "low":
            text = text.split(". ")[0].rstrip(".") + "."
        return Comeback(text=text, style="defuse", language=language, intensity=intensity, source="offline")
    templates = FIGHT_STYLES_FA if language == "fa" else FIGHT_STYLES
    pool = templates.get(style) or templates["clever"]
    template = secrets.choice(pool)
    safe_hook = (hook or ("this" if language == "en" else "این")).strip()
    if len(safe_hook) > 80:
        safe_hook = safe_hook[:77].rstrip() + "…"
    text = template.format(hook=safe_hook)
    if intensity == "low":
        text = text.rstrip("!.") + ("." if language == "en" else ".")
    return Comeback(text=text, style=style, language=language, intensity=intensity, source="offline")


def contains_harassment(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in HARASSMENT_MARKERS)


#: Gen-Z roasts that a person would actually send back.
#:
#: The rule that keeps them from being cringe: **no slang stuffing**. A line
#: earns its place by being specific about the *message*, not by dropping a
#: trending word into an otherwise dead sentence. No appearance, identity,
#: family or ability jokes — those are not roasting, they are just aiming lower.
ROASTS: dict[str, tuple[str, ...]] = {
    "en": (
        "the confidence is loud, the point is missing.",
        "{hook} is doing a lot of work for an argument that never showed up.",
        "you typed that with your whole chest and still landed on nothing.",
        "this reads like a group project where you did the title slide.",
        "not you arguing with a sentence you clearly didn't finish reading.",
        "your take arrived room temperature.",
        "that's a lot of words to say you didn't check.",
        "you're giving 'sent from my notes app at 3am' and not in the good way.",
        "i would explain it slower, but i don't think speed was the problem.",
        "you had a point. then you kept typing and lost it.",
        "bold claim, zero receipts, classic monday.",
        "that sentence took a lap around the point and went home.",
    ),
    "fa": (
        "اعتماد به نفسش بلنده، خودِ نکته غایبه.",
        "{hook} داره برای یه استدلالی که هیچ‌وقت نیومد، خیلی تلاش می‌کنه.",
        "این‌همه مطمئن نوشتی و آخرش به هیچ‌جا نرسیدی.",
        "این‌طوری می‌گی انگار توی پروژه گروهی فقط اسلاید اول رو تو ساختی.",
        "داری با جمله‌ای بحث می‌کنی که تا آخرش نخوندی.",
        "حرفت دمای اتاق رو داره.",
        "این‌همه کلمه برای اینکه بگی چک نکردی؟",
        "می‌خواستم آروم‌تر توضیح بدم، ولی فکر نکنم مشکل سرعت بوده.",
        "یه نکته داشتی. بعد ادامه دادی و گمش کردی.",
        "ادعای بزرگ، صفر سند. دوشنبه‌ی کلاسیک.",
        "این جمله یه دور دور نکته زد و رفت خونه.",
    ),
}

#: Bound on how mean a roast may get. The command maps `--spice` onto these.
ROAST_SPICE: tuple[str, ...] = ("mild", "medium", "spicy")

ROAST_SYSTEM_PROMPT = (
    "You are a funny, confident friend on a group chat. Write ONE roast line "
    "(max 180 characters) about the quoted message, in {language_name}. Style: "
    "2020s internet humour, dry and specific. Hard rules: no slurs, no jokes "
    "about appearance, race, religion, gender, sexuality, disability, family or "
    "any private information; mock the take, never the person's body or "
    "identity. No hashtags, no emoji spam, no 'skibidi'/'rizz' filler."
)

FIGHT_SYSTEM_PROMPT = (
    "You are a witty but kind conversation partner. Produce ONE short comeback "
    "(max 220 characters) replying to the user's quoted message. Rules: never "
    "threaten, never insult identity, never mention private data, no slurs, no "
    "harassment. Keep it playful wordplay. Answer in {language_name} only."
)
