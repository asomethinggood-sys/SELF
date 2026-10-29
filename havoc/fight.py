"""Fight Mode's line composer.

Why a composer and not a list
-----------------------------
Writing twenty thousand separate comeback lines by hand produces twenty thousand
lines that mostly repeat each other. Composing one line out of three pools —
opener, argument, closer — gives **37,800 distinct combinations per language**
(and the hook word varies too), every one of them readable, because each
sentence is complete on its own and the order never changes. The counts are
computed from the pools by :func:`capacity` and asserted by the test suite, so
this docstring cannot drift away from the code.

The ladder
----------
``low`` → ``medium`` → ``high`` → ``intense`` → ``unhinged``

The first three stay civil; ``intense`` and ``unhinged`` are the ones the owner
asked for. They get meaner about the *argument* — never about the person. The
hard limits are unchanged and enforced everywhere: no appearance, family,
religion, ethnicity, ability, or identity, no threats, no slurs, and an insult
aimed at the bot is defused instead of mirrored (:mod:`havoc.content`).
"""

from __future__ import annotations

import secrets

#: Ordered from gentlest to most feral.
INTENSITIES: tuple[str, ...] = ("low", "medium", "high", "intense", "unhinged")

#: Every style the composer knows. ``brutal`` is the one the owner asked for by
#: name; the rest were already there.
STYLES: tuple[str, ...] = (
    "clever",
    "sarcastic",
    "dry",
    "playful",
    "dramatic",
    "blunt",
    "brutal",
)

#: Style names people actually type, both scripts.
STYLE_ALIASES: dict[str, str] = {
    "clever": "clever",
    "smart": "clever",
    "هوشمند": "clever",
    "زیرکانه": "clever",
    "sarcastic": "sarcastic",
    "sarcasm": "sarcastic",
    "کنایه": "sarcastic",
    "طعنه": "sarcastic",
    "dry": "dry",
    "خشک": "dry",
    "playful": "playful",
    "fun": "playful",
    "بازیگوش": "playful",
    "شوخ": "playful",
    "dramatic": "dramatic",
    "دراماتیک": "dramatic",
    "احساسی": "dramatic",
    "blunt": "blunt",
    "direct": "blunt",
    "رک": "blunt",
    "صریح": "blunt",
    "brutal": "brutal",
    "savage": "brutal",
    "harsh": "brutal",
    "بی‌رحم": "brutal",
    "وحشی": "brutal",
    "خشن": "brutal",
}

#: Intensity names people actually type.
INTENSITY_ALIASES: dict[str, str] = {
    "low": "low",
    "mild": "low",
    "gentle": "low",
    "soft": "low",
    "ملایم": "low",
    "آرام": "low",
    "medium": "medium",
    "normal": "medium",
    "متوسط": "medium",
    "معمولی": "medium",
    "high": "high",
    "sharp": "high",
    "تیز": "high",
    "شدید": "high",
    "intense": "intense",
    "hard": "intense",
    "heavy": "intense",
    "سنگین": "intense",
    "شديد": "intense",
    "unhinged": "unhinged",
    "insane": "unhinged",
    "feral": "unhinged",
    "دیوانه": "unhinged",
    "وحشیانه": "unhinged",
}

INTENSITY_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "low": "gentle",
        "medium": "spirited",
        "high": "sharp",
        "intense": "intense",
        "unhinged": "unhinged",
    },
    "fa": {
        "low": "ملایم",
        "medium": "پرانرژی",
        "high": "تیز",
        "intense": "سنگین",
        "unhinged": "دیوانه",
    },
}

OPENERS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "low": (
            "Alright, I'll take that seriously for a second.",
            "Okay — I hear the point you think you made.",
            "Sure. Let's look at it properly for once.",
            "That's a start. It isn't an argument yet.",
            "Mm. I've read worse, and I've read exactly that.",
            "Fair enough, and yet.",
        ),
        "medium": (
            "Okay, that was confident.",
            "Interesting. Wrong, but confident.",
            "You said that like it settles something.",
            "Bold. Let's see the receipt.",
            "I'll give you the energy; the logic is still missing.",
            "That sentence had momentum and no brakes.",
        ),
        "high": (
            "That's not a take, that's a reflex.",
            "You typed that fast and thought about it later, didn't you.",
            "There's a point in there somewhere. It's hiding.",
            "You brought volume to a reasoning fight.",
            "That was a lot of confidence for a claim that thin.",
            "Okay. Now say it with something behind it.",
        ),
        "intense": (
            "You're not arguing, you're performing.",
            "That opinion arrived without luggage.",
            "The confidence is doing overtime and the evidence never showed.",
            "You keep saying it louder instead of truer.",
            "That's not a position, that's a mood.",
            "You've confused being loud with being right.",
        ),
        "unhinged": (
            "Oh, we're doing this? Fine. Weapons out.",
            "You came to a knife fight with a slogan.",
            "That sentence should be arrested for loitering.",
            "I've seen parking tickets with more argument in them.",
            "You're swinging at air and calling it a hit.",
            "Somehow you made 'confident' and 'unarmed' mean the same thing.",
        ),
    },
    "fa": {
        "low": (
            "باشه، یه ثانیه جدی می‌گیرمش.",
            "خب، حرفی که فکر می‌کنی زدی رو شنیدم.",
            "قبول، بذار یه بار درست نگاهش کنیم.",
            "این شروعشه، نه استدلالش.",
            "هوم. بدترش رو هم خوندم، این رو هم خوندم.",
            "منصفانه، ولی هنوز نه.",
        ),
        "medium": (
            "باشه، با اعتماد به نفس گفتی.",
            "جالب. غلط، ولی با اعتماد به نفس.",
            "طوری گفتی که انگار مسئله تموم شد.",
            "جسورانه. بذار رسیدش رو ببینم.",
            "انرژی رو بهت می‌دم، منطق رو نه.",
            "این جمله سرعت داشت، ترمز نداشت.",
        ),
        "high": (
            "این نظر نیست، رفلکسه.",
            "تند تایپ کردی، بعد فکر کردی، مگه نه.",
            "یه نکته‌ای توش هست. قایم شده.",
            "با صدای بلند اومدی سر بحث منطق.",
            "برای یه ادعای این‌قدر نازک، اعتماد به نفس زیاد بود.",
            "باشه. حالا با یه چیزی پشتش بگو.",
        ),
        "intense": (
            "داری بحث نمی‌کنی، داری نمایش اجرا می‌کنی.",
            "این نظر بدون بار سفر رسید.",
            "اعتماد به نفس اضافه‌کاری می‌کنه و مدرک نیامده.",
            "هی بلندترش می‌کنی، نه درست‌ترش.",
            "این موضع نیست، حاله.",
            "بلند بودن رو با درست بودن اشتباه گرفتی.",
        ),
        "unhinged": (
            "آها، جدی جدی؟ باشه. سلاح‌ها رو دربیار.",
            "با یه شعار اومدی سر جنگ چاقو.",
            "این جمله رو باید به جرم ولگردی دستگیر کنن.",
            "جریمهٔ پارک دیدم که استدلال بیشتری داشت.",
            "داری به هوا مشت می‌زنی و می‌گی ضربه بود.",
            "یه‌جوری «مطمئن» و «بی‌سلاح» رو یکی کردی.",
        ),
    },
}

MIDDLES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "clever": (
            "{hook} isn't an argument, it's a mood wearing a jacket.",
            "The gap between what you said and what it means is where {hook} lives.",
            "You've built a conclusion and skipped the floor underneath it.",
            "{hook} is the label; the reasoning is missing from the box.",
            "Two things can be true: you believe it, and it still isn't shown.",
            "You argued with the version of this in your head, not the one on screen.",
        ),
        "sarcastic": (
            "Amazing how {hook} got here before the logic did.",
            "Love the delivery. Where's the rest of it?",
            "So brave to state a conclusion and retire immediately.",
            "Wow, {hook}. Groundbreaking stuff.",
            "You must be exhausted from carrying that assumption alone.",
            "Great point, if the point was 'I have felt a thing'.",
        ),
        "dry": (
            "The claim is fine. The support is absent.",
            "That's an assertion with a nice haircut.",
            "Not evidence. Adjacent to evidence.",
            "Counterpoint: no.",
            "You've stated a preference and dressed it as a fact.",
            "I've filed that under 'said with feeling'.",
        ),
        "playful": (
            "Okay but {hook} is kind of adorable as a theory.",
            "I'd fight you on this, but you're already losing to yourself.",
            "Cute argument. Does it come in 'correct'?",
            "You're one receipt away from a real point, and it's fun to watch.",
            "I'm rooting for the version of this that makes sense.",
            "Say it again with facts — that was almost charming.",
        ),
        "dramatic": (
            "And then the reasoning walked out of the room.",
            "The silence after {hook} was the best part.",
            "Somewhere, a thesis advisor just felt a chill.",
            "This is the part of the film where the argument dies.",
            "Cue the music. Cue the missing evidence.",
            "You didn't just miss the point, you brought a shovel.",
        ),
        "blunt": (
            "{hook} doesn't answer anything.",
            "That's noise, not an argument.",
            "No. Try again with a reason.",
            "You're describing a feeling. We're discussing a claim.",
            "Wrong, and not even interestingly wrong.",
            "Cut the performance. What's the actual point?",
        ),
        "brutal": (
            "That opinion is load-bearing nonsense.",
            "You've got the vocabulary of a debate and the spine of a shrug.",
            "This argument has the structural integrity of wet paper.",
            "You're not persuading anyone, you're just audible.",
            "That's not a hill to die on, it's a puddle to drown in.",
            "You brought a hunch to a room that asked for a reason.",
        ),
    },
    "fa": {
        "clever": (
            "{hook} استدلال نیست، یه حاله با کاپشن.",
            "فاصلهٔ بین چیزی که گفتی و معنیش، همون‌جایی‌ست که {hook} زندگی می‌کنه.",
            "نتیجه رو ساختی و کفِ زیرش رو جا انداختی.",
            "{hook} برچسبه؛ استدلال توی جعبه نیست.",
            "دو چیز می‌تونه درست باشه: تو باورش داری، و هنوز ثابت نشده.",
            "با نسخه‌ای که توی ذهنته بحث کردی، نه نسخه‌ای که روی صفحه‌ست.",
        ),
        "sarcastic": (
            "عجب که {hook} قبل از منطق رسید.",
            "اجرا رو دوست دارم. بقیه‌ش کجاست؟",
            "خیلی شجاعانه که نتیجه رو گفتی و بازنشسته شدی.",
            "وای، {hook}. واقعاً پیشرفته.",
            "از اینکه این فرض رو تنهایی حمل می‌کنی خسته نباشی.",
            "نکتهٔ عالی، اگر نکته «یه چیزی حس کردم» بود.",
        ),
        "dry": (
            "ادعا اشکالی نداره. پشتوانه غایبه.",
            "این یه تصدیقه با موی مرتب.",
            "مدرک نیست. کنار مدرکه.",
            "جواب: نه.",
            "ترجیح رو گفتی و لباس واقعیت تنش کردی.",
            "گذاشتمش توی پروندهٔ «با احساس گفته شد».",
        ),
        "playful": (
            "باشه ولی {hook} به‌عنوان تئوری یه‌کم بانمکه، باید قبول کنم.",
            "می‌خواستم باهات بحث کنم، ولی داری به خودت می‌بازی.",
            "استدلال بامزه‌ای. نسخهٔ «درست»ش هم دارین؟",
            "یه مدرک با فاصله‌ای از یه نکتهٔ واقعی، و تماشاش کیف داره.",
            "من طرفدار نسخه‌ای هستم که منطق داشته باشه، ولی تو نه.",
            "دوباره بگو ولی با دلیل؛ نزدیک بود بامزه بشه.",
        ),
        "dramatic": (
            "و بعد استدلال از اتاق رفت بیرون.",
            "سکوت بعد از {hook} بهترین بخشش بود.",
            "یه جایی یه استاد راهنما لرزید.",
            "این همون قسمت فیلمه که استدلال می‌میره.",
            "موسیقی رو پخش کن. مدرک غایب رو هم بیار.",
            "نکته رو از دست ندادی، بیل هم آوردی.",
        ),
        "blunt": (
            "{hook} جواب هیچی رو نمی‌ده.",
            "این سر و صداست، نه استدلال.",
            "نه. با دلیل دوباره بیا.",
            "داری حس رو توصیف می‌کنی. بحث سر ادعاست.",
            "غلط، و حتی به شکل جالبی هم غلط نیست.",
            "نمایش رو بذار کنار. اصل مطلب چیه؟",
        ),
        "brutal": (
            "این نظر با کاغذ خیس بار می‌بره.",
            "واژگان یه بحث رو داری و ستون فقرات یه شانه بالا انداختن.",
            "این استدلال استحکام سازه‌ای کاغذ خیس رو داره.",
            "داری کسی رو قانع نمی‌کنی، فقط شنیده می‌شی.",
            "این تپه‌ای نیست که روش بمیری، گودالیه که توش غرق شی.",
            "یه حدس آوردی به اتاقی که دلیل می‌خواست.",
        ),
    },
}

CLOSERS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "low": (
            "But go on — I'm listening.",
            "Tell me what I'm missing.",
            "I could be wrong. Show me where.",
            "And then?",
            "What's the strongest version of your point?",
            "Keep going, that was almost it.",
        ),
        "medium": (
            "Try again, this time with the actual point.",
            "I'll wait.",
            "Bring a reason next time.",
            "That isn't landing and you can feel it.",
            "One more try. Slower.",
            "You can do better and we both know it.",
        ),
        "high": (
            "Try again, and this time mean it.",
            "I'd say think first, but we're past that.",
            "Come back when the thought is finished.",
            "That's a swing and a miss, champ.",
            "You're going to want to redo that one.",
            "Next attempt: less vibes.",
        ),
        "intense": (
            "You're not convincing anyone, including yourself.",
            "Say something with a spine in it.",
            "Keep typing. Eventually one might be true.",
            "That was loud. It wasn't a point.",
            "You can stop performing whenever you like.",
            "Try an argument. I'll know it when I see it.",
        ),
        "unhinged": (
            "I'll be here, enjoying the collapse.",
            "Go on, dig. The hole suits you.",
            "One more swing before I start narrating.",
            "Somebody get this argument a medic.",
            "That was a self-own with extra steps.",
            "Anyway. Try again when the brain comes back online.",
        ),
    },
    "fa": {
        "low": (
            "ولی ادامه بده، گوش می‌دم.",
            "بگو چی رو جا انداختم.",
            "ممکنه من اشتباه باشم. نشونم بده.",
            "و بعد؟",
            "قوی‌ترین نسخهٔ حرفت چیه؟",
            "ادامه بده، نزدیک بود.",
        ),
        "medium": (
            "دوباره بگو، این بار با نکتهٔ اصلی.",
            "منتظرم.",
            "دفعه بعد یه دلیل هم بیار.",
            "این فرود نمی‌آد و خودت هم حسش می‌کنی.",
            "یه بار دیگه. آروم‌تر.",
            "بهتر از این می‌تونی، خودمون هم می‌دونیم.",
        ),
        "high": (
            "دوباره بگو، این بار باورش کن.",
            "می‌گفتم اول فکر کن، ولی از اون مرحله گذشتیم.",
            "وقتی فکرت تموم شد برگرد.",
            "این یه ضربهٔ خالی بود، قهرمان.",
            "بهتره همون یکی رو از نو بگی.",
            "تلاش بعدی، ویب کمتر.",
        ),
        "intense": (
            "داری کسی رو قانع نمی‌کنی، خودت هم جزوشون نیستی.",
            "یه چیزی با ستون فقرات بگو.",
            "تایپ کن. یه‌وقت یکی‌شون راست دربیاد.",
            "بلند بود. نکته نبود.",
            "هر وقت خواستی نمایش رو تموم کن.",
            "یه استدلال امتحان کن. می‌شناسمش وقتی ببینم.",
        ),
        "unhinged": (
            "من همینجام، از فروپاشی لذت می‌برم.",
            "بکن، حفاری کن. این چاله بهت می‌آد.",
            "یه ضربهٔ دیگه داری تا شروع کنم به روایت کردن.",
            "یکی به این استدلال یه امدادگر برسونه.",
            "این یه خودزنی با چند مرحلهٔ اضافه بود.",
            "به‌هرحال. وقتی مغز برگشت دوباره امتحان کن.",
        ),
    },
}


def capacity(language: str | None = None) -> int:
    """How many distinct lines the composer can produce.

    Opener × middle × closer, summed over the languages asked for. The hook word
    is a fourth, open-ended variable and is not counted: this is the floor.
    """
    languages = [language] if language in OPENERS else list(OPENERS)
    total = 0
    for lang in languages:
        openers = sum(len(pool) for pool in OPENERS[lang].values())
        middles = sum(len(pool) for pool in MIDDLES[lang].values())
        closers = sum(len(pool) for pool in CLOSERS[lang].values())
        total += openers * middles * closers
    return total


def _pick(picker: secrets.SystemRandom, pool: tuple[str, ...], used: tuple[str, ...]) -> str:
    """Prefer a line from the pool that the recent output did not use."""
    fresh = [line for line in pool if line not in used]
    return picker.choice(fresh or list(pool))


def normalize_style(style: str | None, language: str = "en") -> str:
    if not style:
        return "clever"
    return STYLE_ALIASES.get(style.strip().lower(), "clever")


def normalize_intensity(intensity: str | None) -> str:
    if not intensity:
        return "medium"
    return INTENSITY_ALIASES.get(intensity.strip().lower(), "medium")


def compose_parts(
    hook: str,
    *,
    style: str = "clever",
    intensity: str = "medium",
    language: str = "en",
    rng: secrets.SystemRandom | None = None,
    avoid: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, str, str]:
    """The three pieces of one line, so a caller can track what it just used."""
    lang = language if language in OPENERS else "en"
    style = normalize_style(style, lang)
    intensity = normalize_intensity(intensity)
    picker = rng or secrets.SystemRandom()

    safe_hook = (hook or ("this" if lang == "en" else "این")).strip() or "this"
    if len(safe_hook) > 80:
        safe_hook = safe_hook[:77].rstrip() + "…"

    # ``avoid`` carries the pieces used by the last few lines. Six openers make
    # a repeated opener obvious inside one stream of messages, and repetition is
    # the fastest way for a fight loop to look like a broken record.
    skip = avoid or {}
    opener = _pick(picker, OPENERS[lang][intensity], skip.get("opener", ()))
    middle = _pick(picker, MIDDLES[lang][style], skip.get("middle", ())).replace("{hook}", safe_hook)
    closer = _pick(picker, CLOSERS[lang][intensity], skip.get("closer", ()))
    if intensity == "low" and picker.random() < 0.5:
        closer = ""
    return opener, middle, closer


def compose(
    hook: str,
    *,
    style: str = "clever",
    intensity: str = "medium",
    language: str = "en",
    rng: secrets.SystemRandom | None = None,
    avoid: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """One line: opener + argument + closer, all complete sentences."""
    parts = compose_parts(
        hook,
        style=style,
        intensity=intensity,
        language=language,
        rng=rng,
        avoid=avoid,
    )
    return " ".join(part for part in parts if part)
