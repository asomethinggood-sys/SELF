"""Persian names for every command.

Typing ``.وضعیت`` should feel as natural as ``.status``: the interface is
bilingual, so the command surface is too. Keeping the mapping in one table
(rather than sprinkling Farsi strings across sixty ``Command(...)`` calls)
means it can be reviewed, tested for collisions, and extended without touching
feature code.

The English name always keeps working, and a command may carry several Persian
names where people genuinely use more than one word.
"""

from __future__ import annotations

#: command name -> extra Persian aliases
PERSIAN_ALIASES: dict[str, tuple[str, ...]] = {
    # core
    "help": ("کمک", "راهنما"),
    "commands": ("فرمان‌ها", "دستورها"),
    "status": ("وضعیت",),
    "ping": ("پینگ",),
    "diagnostics": ("عیب‌یابی",),
    "features": ("قابلیت‌ها",),
    "feature": ("قابلیت",),
    "stop": ("توقف",),
    "lang": ("زبان",),
    "config": ("پیکربندی",),
    "selfcheck": ("بررسی",),
    "about": ("درباره",),
    "id": ("شناسه",),
    "prefix": ("پیشوند",),
    "dryrun": ("آزمایشی",),
    "settings": ("تنظیمات",),
    # messaging
    "autoreply": ("پاسخ‌خودکار",),
    "away": ("دورباش",),
    "template": ("قالب",),
    "schedule": ("زمان‌بندی",),
    "scheduled": ("زمان‌بندی‌ها",),
    "fight": ("دعوا",),
    "suggest": ("پیشنهاد",),
    # detection
    "watch": ("دیده‌بان",),
    "trigger": ("محرک",),
    "react": ("واکنش",),
    "rules": ("قوانین",),
    # information
    "search": ("جستجو",),
    "chats": ("چت‌ها",),
    "chatinfo": ("اطلاعات‌چت",),
    "bookmark": ("نشان",),
    "note": ("یادداشت",),
    "media": ("رسانه",),
    "export": ("خروجی",),
    "saved": ("ذخیره‌ها",),
    "labels": ("برچسب‌ها",),
    # language tools
    "digits": ("ارقام",),
    "normalize": ("یکسان‌سازی",),
    "rtl": ("راست‌به‌چپ",),
    "detect": ("تشخیص",),
    "persianize": ("فارسی‌سازی",),
    # management
    "chatsettings": ("تنظیمات‌چت",),
    "alias": ("نام‌مستعار",),
    "quick": ("میان‌بر",),
    "backup": ("پشتیبان",),
    "plugins": ("افزونه‌ها",),
    # profile
    "clock": ("ساعت",),
    "bio": ("بیو",),
    "profile": ("پروفایل",),
    "counter": ("شمارنده",),
    # scheduling
    "remind": ("یادآور",),
    "reminders": ("یادآورها",),
    "tasks": ("کارها",),
    "quiet-hours": ("ساکت",),
    "agenda": ("برنامه",),
    # utilities
    "textstats": ("آمارمتن",),
    "cleanup": ("پاک‌سازی",),
    "transform": ("تبدیل",),
    "calc": ("حساب",),
    "random": ("تصادفی",),
    "coin": ("شیریاپا",),
    "joke": ("جوک",),
    "quote": ("جمله",),
    "time": ("زمان",),
    "convert-time": ("تبدیل‌زمان",),
    "translate": ("ترجمه",),
    "summarize": ("خلاصه",),
    # persona & media
    "yura": ("یورا",),
    "pic": ("عکس", "تصویر"),
    "poem": ("شعر",),
    "fal": ("فال",),
    "roast": ("ریست", "خاک‌برسر"),
}

#: Words that work without the command prefix. Each maps to the command that
#: should run and a fixed subcommand, so ``فال`` is ``.fal`` and ``yura on``
#: stays ``.yura on``. Deliberately tiny: a bare word that runs something must
#: be unmistakable, or ordinary conversation would trigger commands.
BARE_COMMANDS: dict[str, tuple[str, str]] = {
    "yura": ("yura", ""),
    "یورا": ("yura", ""),
    "fal": ("fal", ""),
    "فال": ("fal", ""),
    "poem": ("poem", ""),
    "شعر": ("poem", ""),
    "quote": ("quote", ""),
    "جمله": ("quote", ""),
    "roast": ("roast", ""),
    "خاک‌برسر": ("roast", ""),
}


def aliases_for(name: str) -> tuple[str, ...]:
    return PERSIAN_ALIASES.get(name, ())


def bare_command(token: str) -> tuple[str, str] | None:
    return BARE_COMMANDS.get(token.strip().lower())
