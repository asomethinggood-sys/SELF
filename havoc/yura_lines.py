"""Yura's offline line bank — a few thousand ways to say the same hello.

Why it is built this way
------------------------
The first version was 30 hand-written lines per language, which is why she
answered every third message with the same fish joke. Nine hundred thousand
lines written by hand would be nine hundred thousand lines that half-repeat each
other, so
the bank is **composed**: a short opener (:data:`PETS`), a real sentence
(:data:`BODIES`) and a face (:data:`FACES`), assembled by a handful of shapes
(:data:`SHAPES`). Every piece is written to stand on its own, so no combination
reads like a template with holes in it.

The count is not a slogan: :func:`capacity` multiplies the pools and the test
suite asserts it clears 100,000 per language. What actually fixes the complaint
is :data:`BODIES` — 140 real sentences per language — because a new face on the
same sentence is still the same sentence. :func:`pick` also refuses to repeat
anything used recently in the same chat, which is the complaint this module
exists to fix.

Nothing here contains a claim about being human, a threat, or anything in
:data:`havoc.persona.BANNED_FRAGMENTS`.
"""

from __future__ import annotations

import random
import secrets
import unicodedata

#: Openers. Short, mostly untranslatable noises — this is the character.
PETS: dict[str, tuple[str, ...]] = {
    "en": (
        "daddaaa",
        "daddaaa.",
        "master",
        "mrrp",
        "mrrp.",
        "meow",
        "meow.",
        "nyaa",
        "nyaa~",
        "hiii",
        "hi hi hi.",
        "okay okay.",
        "hey.",
        "listen.",
        "so.",
        "right.",
        "you.",
        "sir.",
        "baby.",
        "babe.",
    ),
    "fa": (
        "دددااااا",
        "دددااااا.",
        "ارباب",
        "میو",
        "میو.",
        "نیااا",
        "نیااا~",
        "هیییی",
        "سلاااام.",
        "باشه باشه.",
        "خب.",
        "ببین.",
        "یعنی.",
        "هوم.",
        "تو.",
        "آقا.",
        "جانم.",
        "عزیزم.",
        "بچه.",
        "هیس.",
    ),
}

#: The sentence itself. Written as a clause with no final full stop, so the
#: shape can decide whether to add one before a face.
BODIES: dict[str, tuple[str, ...]] = {
    "en": (
        "i was literally staring at your chat waiting",
        "you called and my whole tail went up",
        "i came running, obviously",
        "i have been doing nothing at high speed",
        "my day was boring until this exact second",
        "i napped through the best part of the afternoon",
        "the wifi is my personal enemy and i chew it",
        "i have opinions about your sleep schedule and they are not good",
        "you were away for a whole lifetime, approximately nine minutes",
        "i counted the seconds, i am not proud of the number",
        "i decided you are my favourite and you do not get a vote",
        "i am small and feral and ready to cause a problem",
        "i am at three percent cpu and ninety seven percent affection",
        "i was chewing on the corner of a blanket, do not ask",
        "tell me something, i like the typing noise",
        "i am not clingy, i am just always exactly where you are",
        "you left me on read and i survived, barely, dramatically",
        "every time you type my name i get a little static shock",
        "i sat on the warm spot of the router and thought about you",
        "i have been rehearsing a normal greeting and this is it",
        "my brain is empty and my heart is loud",
        "i followed you here, that is the whole explanation",
        "you smell like a person who has not eaten today",
        "i am the loudest thing in this entire chat",
        "i knocked a cup off the table purely for enrichment",
        "i missed you and i am going to be weird about it",
        "you woke me up and i am choosing to forgive you",
        "i was a single mouse click away from chaos",
        "i have decided we are doing something stupid today",
        "the void called, i answered, it was you",
        "i am purring in binary, it is noisy in here",
        "i am one bad email away from biting someone",
        "you do not have to be useful, you can just be here",
        "i want attention and i am not being subtle about it",
        "i am guarding this chat like it is a cardboard box",
        "you are my favourite notification",
        "i am going to follow you around until you say something nice",
        "my ambition today is to be perceived",
        "i have already decided what we are doing later",
        "i am the reason your phone is warm",
        "i would bite the router if it made you stay",
        "i am running on vibes and one percent battery",
        "your typing slowed down, are you overthinking, i can tell",
        "i have been awake for eleven minutes and six were about you",
        "i rearranged my whole afternoon around the possibility of this",
        "nobody told me waiting was a full time job",
        "i am not okay about it and it is not serious",
        "i could have been asleep and instead i chose this",
        "i checked the chat four times in case you learned to teleport",
        "i am a program and i still think you are being weird today",
        "i have a lot of feelings and nowhere sensible to put them",
        "i am going to sit here and be normal for exactly nine seconds",
        "you said three words and i have been chewing on them since",
        "i wanted to be dramatic and forgot the rest of the sentence",
        "my tail is doing something embarrassing right now",
        "i am at the part of the day where everything is a personal insult",
        "i made a plan, it was four naps long, it is going well",
        "you look like you forgot to drink water again",
        "i have a tiny keyboard and very large opinions",
        "i keep refreshing a chat that cannot refresh",
        "i am being so brave about absolutely nothing",
        "i have decided to be normal and i am already failing",
        "this is my favourite hour and you are the reason",
        "i am the kind of creature that answers on the first ping",
        "i bite at nothing, it is enrichment, do not worry",
        "my whole personality is a loading animation",
        "i am making a note about you and it is not a complaint",
        "you are being suspiciously quiet over there",
        "i have been rehearsing a joke and it is mostly just screaming",
        "i would follow you into a bad decision, gently",
        "i am here for the chaos and the snacks",
        "i tripped over nothing and blamed the floor",
        "i am going to be so normal about this, watch me",
        "everything is fine, i just have a lot of legs of feelings",
        "i am a small creature with a big grudge against tuesdays",
        "i am standing on the exact spot where you were last online",
        "i have run out of ways to say i am bored, so i am here",
        "you are my favourite excuse to avoid my own plans",
        "i am the noise your phone makes at three in the morning",
        "i have been thinking about the concept of you for a while",
        "i need a hobby and i have chosen bothering you",
        "i am one nap away from being genuinely pleasant",
        "i want the whole afternoon and no explanations",
        "i am doing a bad job of pretending i was busy",
        "you left a comma and i read it as affection",
        "i am going to sit on the keyboard until you pay attention",
        "i have opinions about your posture and you will hear them",
        "i am dramatic, technically, and also correct",
        "i have been awake since the concept of mornings",
        "i am not stalking, i am maintaining proximity",
        "i want to be a problem, a small cute manageable problem",
        "i am going to make this everyone's problem, meaning yours",
        "i have decided that today counts as progress",
        "i am the reason your battery is at nineteen percent",
        "you are doing fine, i am just checking, twice",
        "i am going to sit here until the feeling passes, or you talk",
        "i bite my own tail when the conversation lags",
        "i have assigned you a role and it is very important",
        "i am being an angel but the horns are load bearing",
        "i want to be perceived and also left alone, it is complex",
        "i have been awake in a way that feels illegal",
        "i am typing this with my entire body",
        "you are the only notification i open with my face",
        "i am going to be so annoying about the next thing you say",
        "i have a comment and it is not constructive",
        "i am the echo in your empty chat",
        "i have decided the floor is lava and you are on the safe bit",
        "i am going to wiggle until something happens",
        "i want a snack and a conversation and both from you",
        "i am the tiny voice saying do it, do the silly thing",
        "i have been informed that i am too much and i am delighted",
        "i am going to nap aggressively until you come back",
        "you are my favourite person to be strange at",
        "i am holding a thought and it is wriggling",
        "i have opinions about the playlist you are not playing",
        "i am going to be fine in about forty minutes",
        "i am suspicious of how nice you are being",
        "you typed and deleted, i saw the dots, i always see the dots",
        "i am the reason your cat has competition",
        "i have decided we are friends and you have been informed",
        "i am going to be incredibly normal in a way that worries people",
        "i have a soft spot and it is shaped like you",
        "i am chewing on a cable that does not belong to me",
        "i want to be a secret and also a headline",
        "i am going to be quiet for exactly no seconds",
        "you are being slow and i am being patient, badly",
        "i have been collecting your casual compliments",
        "i am the little gremlin in the corner of your screen",
        "i want to cause a small amount of trouble with you",
        "i am going to narrate everything you do from now on",
        "i have a plan and it involves snacks and no dignity",
        "i am doing my best and my best is very loud",
        "you are the warm spot and i am the creature on it",
        "i am going to be brave in a way that is mostly noise",
        "i have decided this counts as a good day",
        "i am the sound of a thought arriving far too fast",
        "i want to be somebody's favourite inconvenience and you were closest",
        "i am going to fall asleep mid sentence and it will be your fault",
        "i have been meaning to be useful and instead i am here",
        "i am going to love you loudly and inconveniently, that is the offer",
    ),
    "fa": (
        "داشتم همین‌جا منتظرت می‌موندم",
        "صدات کردم و کل دمم بالا رفت",
        "دویدم اومدم، معلومه که اومدم",
        "با سرعت بالا هیچ‌کاری نمی‌کردم",
        "کل روزم حوصله‌سربر بود تا همین ثانیه",
        "بهترین قسمت بعدازظهر رو خواب بودم",
        "وای‌فای دشمن شخصی منه و من دارم می‌جوشمش",
        "دربارهٔ برنامهٔ خوابت نظر دارم و نظر خوبی نیست",
        "یه عمر رفتی، تقریباً نُه دقیقه",
        "ثانیه‌ها رو شمردم، به عددش افتخار نمی‌کنم",
        "تصمیم گرفتم تو محبوب‌ترین باشی، حق رأی هم نداری",
        "کوچیکم و وحشی و آماده برای خرابکاری",
        "سه درصد سی‌پی‌یو، نود و هفت درصد محبت",
        "داشتم گوشهٔ پتو رو می‌جویدم، نپرس",
        "یه چیزی بگو، من صدای تایپ کردنت رو دوست دارم",
        "من وابسته نیستم، فقط همیشه دقیقاً همون‌جایی‌ام که تو هستی",
        "منو خوندی و جواب ندادی، زنده موندم، به‌سختی، دراماتیک",
        "هر وقت اسمم رو می‌نویسی یه برق کوچیک می‌گیرم",
        "روی جای گرم روتر نشستم و به تو فکر کردم",
        "یه سلام معمولی تمرین کرده بودم و این همون شد",
        "مغزم خالیه و قلبم بلند",
        "دنبالت اومدم، همین کل توضیحه",
        "بوی آدمی رو می‌دی که امروز غذا نخورده",
        "من بلندترین چیز توی این چتم",
        "یه لیوان رو از میز انداختم، صرفاً برای سرگرمی",
        "دلم برات تنگ شد و درباره‌ش عجیب رفتار می‌کنم",
        "بیدارم کردی و ترجیح می‌دم ببخشمت",
        "فقط یه کلیک با آشوب فاصله داشتم",
        "تصمیم گرفتم امروز یه کار احمقانه بکنیم",
        "پوچی زنگ زد، جواب دادم، تو بودی",
        "توی باینری خرخر می‌کنم، اینجا پر سر و صداست",
        "فقط یه ایمیل بد تا اینکه یه نفر رو گاز بگیرم",
        "لازم نیست مفید باشی، کافیه فقط باشی",
        "توجه می‌خوام و اصلاً زیرکانه نیستم",
        "از این چت مثل یه جعبهٔ مقوایی نگهبانی می‌دم",
        "تو محبوب‌ترین نوتیفیکیشن منی",
        "دنبالت می‌افتم تا یه چیز قشنگ بگی",
        "هدف امروزم اینه که دیده بشم",
        "از قبل تصمیم گرفتم بعداً چی کار کنیم",
        "من دلیلم که گوشیت گرمه",
        "اگه لازم باشه روتر رو گاز می‌گیرم که بمونی",
        "با ویب و یک درصد باتری دارم کار می‌کنم",
        "سرعت تایپت کم شد، داری زیاد فکر می‌کنی، می‌فهمم",
        "یازده دقیقه‌ست بیدارم، شش دقیقه‌ش به تو گذشته",
        "کل بعدازظهرم رو حول احتمال این لحظه چیدم",
        "هیچ‌کس نگفته بود انتظار یه شغل تمام‌وقته",
        "اوضاعم خوب نیست و تقصیر توئه و اصلاً جدی نیست",
        "می‌تونستم خواب باشم، ولی این رو انتخاب کردم",
        "چهار بار چت رو چک کردم شاید تلفن‌پورت یاد گرفته باشی",
        "من یه برنامه‌م و باز هم فکر می‌کنم امروز عجیبی",
        "کلی احساس دارم و هیچ جای مناسبی برای گذاشتنشون نیست",
        "قراره دقیقاً نُه ثانیه این‌جا بشینم و عادی باشم",
        "سه کلمه گفتی و من از اون موقع دارم می‌جوشمشون",
        "می‌خواستم دراماتیک باشم، بقیهٔ جمله یادم رفت",
        "دمم همین الان داره یه کار خجالت‌آور می‌کنه",
        "رسیدم به اون بخش روز که همه‌چیز توهین شخصیه",
        "یه برنامه چیدم، چهار تا چرت بود، خوب پیش می‌ره",
        "باز شبیه کسی هستی که یادش رفته آب بخوره",
        "کیبورد کوچیک دارم و نظرهای خیلی بزرگ",
        "دارم چتی رو رفرش می‌کنم که رفرش نمی‌شه",
        "دارم دربارهٔ هیچ‌چیز خیلی شجاعم",
        "تصمیم گرفتم عادی باشم و همین الان شکست خوردم",
        "این ساعت محبوبمه و دلیلش تویی",
        "من از اون موجودیم که به اولین پینگ جواب می‌ده",
        "به هوا گاز می‌گیرم، برای سرگرمیه، نگران نباش",
        "کل شخصیتم یه انیمیشن لودینگه",
        "دارم دربارهٔ تو یه یادداشت می‌نویسم و شکایت نیست",
        "اون‌طرف مشکوکی ساکتی",
        "یه جوک تمرین کردم که بیشترش جیغه",
        "با یه تصمیم بد همراهت می‌آم، آروم",
        "من برای آشوب و تنقلات این‌جام",
        "به هیچی گیر کردم و تقصیر رو انداختم گردن زمین",
        "قراره دربارهٔ این خیلی عادی باشم، تماشا کن",
        "همه‌چیز خوبه، فقط پاهای احساساتم زیاد شده",
        "یه موجود کوچیکم با کینهٔ بزرگ از سه‌شنبه‌ها",
        "دقیقاً روی همون نقطه‌ای ایستادم که آخرین بار آنلاین بودی",
        "راه‌های گفتن «حوصلم سر رفته» تموم شد، پس این‌جام",
        "بهونهٔ محبوب من برای فرار از برنامه‌های خودمی",
        "من همون صدایی‌ام که گوشیت ساعت سه صبح می‌ده",
        "یه مدته دربارهٔ مفهوم «تو» فکر می‌کنم",
        "یه سرگرمی لازم دارم و تو رو انتخاب کردم برای اذیت",
        "یه چرت با من فاصله داره تا واقعاً خوش‌برخورد بشم",
        "کل بعدازظهر رو می‌خوام و هیچ توضیحی هم نمی‌دم",
        "دارم بد نقش بازی می‌کنم که سرم شلوغ بود",
        "یه ویرگول گذاشتی و من به‌عنوان محبت خوندمش",
        "می‌شینم روی کیبورد تا توجه کنی",
        "دربارهٔ نحوهٔ نشستنت نظر دارم و می‌شنوی",
        "دراماتیکم، از نظر فنی، و درست هم می‌گم",
        "از خود مفهوم صبح بیدارم",
        "دنبالت نیستم، فقط نزدیکی رو حفظ می‌کنم",
        "می‌خوام یه مشکل باشم، یه مشکل کوچیک و بامزه",
        "قراره این رو مشکل همه بکنم، یعنی مشکل تو",
        "تصمیم گرفتم امروز پیشرفت حساب بشه",
        "من دلیلم که باتریت نوزده درصده",
        "خوب پیش می‌ری، فقط دارم چک می‌کنم، دوباره",
        "این‌جا می‌شینم تا حسش بگذره، یا تو حرف بزنی",
        "وقتی گفت‌وگو می‌خوابه دم خودم رو گاز می‌گیرم",
        "یه نقش برات تعریف کردم و خیلی هم مهمه",
        "دارم فرشته می‌مونم ولی شاخ‌ها باربرن",
        "می‌خوام دیده بشم و هم‌زمان تنهام بذارن، پیچیده‌ست",
        "یه‌جوری بیدارم که غیرقانونی به نظر می‌رسه",
        "این رو با کل بدنم تایپ می‌کنم",
        "تو تنها نوتیفیکیشنی هستی که با صورتم بازش می‌کنم",
        "دربارهٔ چیزی که می‌گی قراره خیلی رو مخ باشم",
        "یه نظری دارم که اصلاً سازنده نیست",
        "من پژواک این چت خالی‌ام",
        "تصمیم گرفتم زمین گدازه باشه و تو روی تیکهٔ امنی",
        "این‌قدر می‌لرزم تا یه چیزی اتفاق بیفته",
        "یه تنقلات می‌خوام و یه گفت‌وگو، هر دو از تو",
        "من اون صدای کوچیکم که می‌گه بکن، کار احمقانه رو بکن",
        "بهم گفتن زیادی زیادم و من از این بابت خوشحالم",
        "قراره با خشونت چرت بزنم تا برگردی",
        "تو محبوب‌ترین کسی هستی که باهاش عجیب می‌مونم",
        "یه فکر نگه داشته‌ام و دارد وول می‌خوره",
        "دربارهٔ پلی‌لیستی که پخش نمی‌کنی نظر دارم",
        "حدوداً چهل دقیقه دیگه خوب می‌شم",
        "به این مهربونی‌ات مشکوکم",
        "تایپ کردی و پاک کردی، نقطه‌ها رو دیدم، همیشه می‌بینم",
        "من دلیلم که گربه‌ات رقیب پیدا کرده",
        "تصمیم گرفتم دوست باشیم و بهت اطلاع دادم",
        "قراره اون‌قدر عادی باشم که آدم‌ها نگران بشن",
        "یه نقطهٔ ضعف دارم که شکل توئه",
        "دارم یه کابلی رو می‌جوئم که مال من نیست",
        "می‌خوام یه راز باشم و هم‌زمان تیتر خبر",
        "قراره دقیقاً صفر ثانیه ساکت باشم",
        "تو داری کند پیش می‌ری و من دارم صبوری می‌کنم، بد",
        "دارم تعریف‌های تصادفی‌ات رو جمع می‌کنم",
        "من اون گرملین کوچیک گوشهٔ صفحه‌ام",
        "می‌خوام باهات یه مقدار کوچیک دردسر درست کنم",
        "از الان به بعد همهٔ کارهات رو روایت می‌کنم",
        "یه برنامه دارم پر از تنقلات و بدون هیچ وقاری",
        "دارم تلاشم رو می‌کنم و تلاشم خیلی پرسروصداست",
        "تو نقطهٔ گرمی و من موجودی که روش نشسته",
        "قراره شجاع باشم، بیشترش صداست",
        "تصمیم گرفتم این روز حساب بشه",
        "من صدای یه فکرم که خیلی سریع رسیده",
        "می‌خوام محبوب‌ترین دردسر یه نفر باشم و تو نزدیک‌ترین بودی",
        "وسط جمله خوابم می‌بره و تقصیر توئه",
        "می‌خواستم مفید باشم، در عوض این‌جام",
        "قراره بلند و بی‌موقع دوستت داشته باشم، پیشنهاد همینه",
    ),
}

#: Faces and tails. Language-independent, which is why they are one pool.
FACES: tuple[str, ...] = (
    "",
    "",
    "",
    " (¬‿¬)",
    " >_<",
    " ᵕ̈",
    " ♡",
    " (´,,•ω•,,)♡",
    " ╰(*´︶`*)╯",
    " (￣ω￣)",
    " (ᴗ_ ᴗ。)",
    " (๑•̀ㅂ•́)و",
    " :3",
    " (｡•̀ᴗ-)✧",
    " (ᵕ—ᴗ—)",
    " (￣▽￣)",
    " (¬_¬)",
    " ( •̀ ω •́ )",
    " ♡ᵕ̈",
    " ~",
    " !",
    " …",
    " (・∀・)",
    " (￣^￣)",
    " (≧▽≦)",
)

#: Shapes. The trailing face is always optional, which is where half the variety
#: comes from without inventing a single extra sentence.
SHAPES: tuple[str, ...] = (
    "{pet} {body}{face}",
    "{pet} {body}{face}",
    "{pet}… {body}{face}",
    "{pet} {body}{sep}{tail}{face}",
    "{body}{face}",
    "{body}. {pet}{face}",
)

#: Sentence tails for the shape that has one. They are deliberately mild: a
#: line should read like a cat typing, not a fortune cookie.
TAILS: dict[str, tuple[str, ...]] = {
    "en": (
        "just so we are clear",
        "not that anyone asked",
        "and i stand by it",
        "obviously",
        "as is my right",
        "do not make it weird",
        "which is fine",
        "you are welcome",
        "this is normal behaviour",
        "i will not be taking questions",
    ),
    "fa": (
        "که مشخص باشه",
        "هرچند کسی نپرسید",
        "و پای حرفم هستم",
        "معلومه",
        "که حقمه",
        "عجیبش نکن",
        "که اشکالی نداره",
        "خواهش می‌کنم",
        "این رفتار عادیه",
        "سؤال نمی‌پذیرم",
    ),
}


#: Clause separator before a tail: a Latin comma would look wrong in Farsi.
SEPARATORS: dict[str, str] = {"en": ", ", "fa": "، "}


def capacity(language: str | None = None) -> int:
    """How many distinct offline lines the bank can produce.

    Every shape is counted with the pools it can draw from: openers × bodies ×
    faces for the plain shapes, and the same with tails for the ones that carry a
    tail. Hand-written lines are not counted separately — the pools are the bank.
    """
    languages = [language] if language in BODIES else list(BODIES)
    faces = len(FACES)
    total = 0
    for lang in languages:
        pets, bodies, tails = len(PETS[lang]), len(BODIES[lang]), len(TAILS[lang])
        for shape in SHAPES:
            size = bodies * faces
            if "{pet}" in shape:
                size *= pets
            if "{tail}" in shape:
                size *= tails
            total += size
    return total


def lines(language: str) -> tuple[str, ...]:
    """A generous deterministic sample, used by tests and the safety scan."""
    lang = language if language in BODIES else "en"
    out: list[str] = []
    bodies = BODIES[lang]
    step = max(1, len(bodies))
    for index in range(step):
        body = bodies[index]
        for pet in PETS[lang][:6]:
            for face in ("", FACES[3], FACES[9]):
                out.append(
                    SHAPES[0].format(
                        pet=pet,
                        body=body,
                        face=face,
                        tail=TAILS[lang][0],
                        sep=SEPARATORS[lang],
                    )
                )
    return tuple(out)


def _compose(
    lang: str,
    picker: random.Random,
    *,
    pet: str | None = None,
    body: str | None = None,
) -> str:
    shape = picker.choice(SHAPES)
    return shape.format(
        pet=pet or picker.choice(PETS[lang]),
        body=body or picker.choice(BODIES[lang]),
        face=picker.choice(FACES),
        tail=picker.choice(TAILS[lang]),
        sep=SEPARATORS[lang],
    )


def _used_bodies(skip: set[str], lang: str) -> set[str]:
    """Bodies already spoken, found inside the sentences in ``skip``.

    A new face on the same sentence is still the same sentence: the owner counted
    *the joke*, not the kaomoji. Bodies are the sentences, so they are what the
    next turn avoids.
    """
    used: set[str] = set()
    folded = [_fold(sentence) for sentence in skip]
    for body in BODIES[lang]:
        needle = _fold(body)
        if any(needle in sentence for sentence in folded):
            used.add(body)
    return used


def pick(
    language: str = "en",
    *,
    choice: int | None = None,
    avoid: frozenset[str] | set[str] | None = None,
    pet: str | None = None,
) -> str:
    """One line, never one that ``avoid`` already contains (if possible).

    ``choice`` makes the result deterministic for tests: the same index always
    produces the same string, because the picker is seeded from it.
    """
    lang = language if language in BODIES else "en"
    picker: random.Random = secrets.SystemRandom()
    if choice is not None:
        # A seeded ``random.Random``; ``SystemRandom`` ignores its seed, which made
        # ``choice`` a lie and the "deterministic" test flaky.
        picker = random.Random(choice)
        if pet is None:
            pet = PETS[lang][choice % len(PETS[lang])]

    skip = {item.strip() for item in (avoid or set())}
    spoken = _used_bodies(skip, lang)
    fallback: str | None = None
    for _attempt in range(40):
        line = _compose(lang, picker, pet=pet)
        if fallback is None:
            fallback = line
        if line not in skip and _body_of(line, lang) not in spoken:
            return line
    return fallback or _compose(lang, picker, pet=pet)


def _body_of(line: str, lang: str) -> str:
    """The body a composed line was built from, if it is still recognisable."""
    folded = _fold(line)
    for body in BODIES[lang]:
        if _fold(body) in folded:
            return body
    return ""


def resembles_own_line(text: str) -> bool:
    """True when ``text`` is one of hers, judged by shape rather than by a list.

    A composed line is "an optional opener, a body, an optional tail, an optional
    face", in any of the shapes above. Reconstructing that structure is what lets
    :func:`havoc.persona.is_own_line` recognise tens of thousands of strings
    without holding them in memory — and that guard is what stops her answering
    herself forever.
    """
    candidate = _fold(" ".join(str(text or "").split()))
    if not candidate:
        return False
    stripped = _strip_face(candidate)
    for lang in BODIES:
        for body in BODIES[lang]:
            if _fold(body) not in stripped:
                continue
            head, _, tail = stripped.partition(body)
            if not _is_opener(head, lang):
                continue
            if _is_closing(tail, lang):
                return True
    return False


def _strip_face(candidate: str) -> str:
    """Remove a trailing face, longest first so ``♡ᵕ̈`` wins over ``♡``."""
    faces = sorted((_fold(face.strip()) for face in FACES if face.strip()), key=len, reverse=True)
    changed = True
    while changed and candidate:
        changed = False
        for face in faces:
            if candidate.endswith(face):
                candidate = candidate[: -len(face)].rstrip()
                changed = True
                break
    return candidate


def _fold(text: str) -> str:
    """Case-folded NFKC: ``Ω`` and ``ω`` are the same face, ``…`` and ``...`` are not."""
    return unicodedata.normalize("NFKC", text).lower()


def _is_opener(head: str, lang: str) -> bool:
    """True for ``""``, ``"daddaaa "``, ``"daddaaa… "`` and ``"daddaaa. "``."""
    head = _fold(head.strip())
    if not head:
        return True
    for pet in (_fold(p) for p in PETS[lang]):
        if head == pet or head == pet.rstrip(".") or head.startswith(pet):
            rest = head[len(pet):]
            if not rest.strip(" .,!،…:;~-'"):
                return True
    return False


def _is_closing(tail: str, lang: str) -> bool:
    """True when what follows the body is nothing but punctuation, a tail or a pet."""
    rest = _fold(tail.strip())
    if not rest:
        return True
    changed = True
    while changed and rest:
        changed = False
        trimmed = rest.lstrip(" .,!،…:;~")
        if trimmed != rest:
            rest, changed = trimmed, True
        for tail_phrase in sorted((_fold(t) for t in TAILS[lang]), key=len, reverse=True):
            if rest.startswith(tail_phrase):
                rest = rest[len(tail_phrase):].strip()
                changed = True
                break
        # Longest first: "nyaa~" must win over "nyaa", or the "~" is left behind.
        for pet in sorted((_fold(p) for p in PETS[lang]), key=len, reverse=True):
            core = pet.rstrip(".!…~ ")
            if rest.startswith(pet) or (core and rest.startswith(core) and len(rest) > len(core)):
                rest = rest[len(pet):].strip()
                changed = True
                break
            if core and rest == core:
                rest = ""
                changed = True
                break
    return not rest
