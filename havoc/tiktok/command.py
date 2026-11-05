"""The command surface, ported: same names, TikTok-shaped answers.

On Telegram a command can draw a card, attach a keyboard, or send a voice note.
In a TikTok DM it can send text, and that is all. So this file is the *same
command vocabulary* with every answer reduced to what survives in a chat bubble:

    .like <url>              .follow @someone           .block @someone
    .unfollow @someone       .comment <url> <text>      .post <path> [caption]
    .draw <prompt>           .remind 30m <thing>        .yura <anything>
    .status                  .help                      .stop

What is *not* here, and why: nothing that needs a keyboard (the settings menus),
nothing that renders HTML (the cards), nothing that sends media as a gallery.
``.draw`` and ``.post`` produce a file path instead of an upload — the browser
can attach a file from disk, and the run does not need a Telegram round-trip to
hand the owner a picture.

Every command that touches the account spends an allowance from
:mod:`havoc.tiktok.limits` *through* :mod:`havoc.tiktok.actions`, and everything
that only reads is free. ``.stop`` is the one command that overrules the rest:
it halts the run instead of answering.
"""

from __future__ import annotations

import re
from datetime import UTC
from typing import Any

from .voice import receipt, refusal

__all__ = ["HELP", "dispatch", "parse"]

#: The help text. Short enough to arrive as one DM, which is the constraint that
#: shapes it — Telegram's paginated help does not survive here.
HELP = (
    "yura on tiktok ♡\n"
    ".like <url> · .comment <url> <text>\n"
    ".follow @x · .unfollow @x · .block @x\n"
    ".post <file> [caption] · .draw <prompt>\n"
    ".remind 30m water · .status · .stop\n"
    "or just talk to me"
)

#: ``.remind 30m drink water`` / ``.remind ۲ ساعت آب``
_REMIND = re.compile(
    r"^(?P<amount>\d+)\s*(?P<unit>s|m|h|d|sec|min|hour|day|ثانیه|دقیقه|ساعت|روز)\s+(?P<what>.+)$",
    re.I,
)
_UNITS = {
    "s": 1, "sec": 1, "ثانیه": 1,
    "m": 60, "min": 60, "دقیقه": 60,
    "h": 3600, "hour": 3600, "ساعت": 3600,
    "d": 86400, "day": 86400, "روز": 86400,
}
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def parse(line: str) -> tuple[str, str]:
    """Split ``like https://…`` into ``("like", "https://…")``."""
    line = line.strip()
    if not line:
        return "", ""
    head, _, rest = line.partition(" ")
    return head.lower().lstrip("."), rest.strip()


def _url(text: str) -> str:
    found = re.search(r"https?://\S+", text)
    return found.group(0) if found else ""


def _handle(text: str) -> str:
    found = re.search(r"@([A-Za-z0-9._]{2,24})", text)
    if found:
        return found.group(1)
    stripped = text.strip().lstrip("@")
    return stripped if re.fullmatch(r"[A-Za-z0-9._]{2,24}", stripped) else ""


def _seconds(spec: str) -> int:
    match = _REMIND.match(spec.translate(_PERSIAN_DIGITS))
    if not match:
        return 0
    return int(match.group("amount")) * _UNITS.get(match.group("unit").lower(), 0)


async def dispatch(raw: str, *, bot: Any, owner: bool, message: Any) -> str:
    """Run one dot command and return the one line it answers with.

    ``owner`` is checked here and not by the caller: a stranger in a DM who types
    ``.block @someone`` must not be able to make this account block anyone. Every
    write is owner-only; ``.help``, ``.status``, ``.yura`` and ``.draw`` are not.
    """
    name, rest = parse(raw)
    if not name:
        return ""

    if name in {"help", "commands", "?", "start"}:
        return HELP
    if name in {"stop", "panic", "halt"}:
        if not owner:
            return refusal("budget", "that")
        bot.challenge = False
        bot.stats["stopped"] = 1
        return "stopping. nothing else goes out."

    if name in {"yura", "یورا"}:
        return await bot.answer(rest or "", message, owner=owner)

    if name in {"status", "state"}:
        counts = " ".join(
            f"{action} {bot.budget.spent(action)}/{bot.budget.remaining(action) + bot.budget.spent(action)}"
            for action in ("like", "comment", "follow", "post", "dm")
        )
        return (
            f"run: {bot.mode} · answered {bot.stats['answered']} · "
            f"seen {bot.stats['seen']}\n{counts}"
        )

    if name in {"remind", "reminder", "یادآوری"}:
        return await _remind(bot, rest, owner=owner)

    if name in {"draw", "imagegen", "نقاشی"}:
        return await _draw(bot, rest)

    # ---- things that touch the account ----------------------------------
    writes = {
        "like": ("like", "url"),
        "unlike": ("unlike", "url"),
        "comment": ("comment", "url"),
        "follow": ("follow", "handle"),
        "unfollow": ("unfollow", "handle"),
        "block": ("block", "handle"),
        "post": ("upload", "file"),
    }
    if name in writes:
        if not owner:
            return refusal("budget", name)
        action = getattr(bot, "actions", None)
        if action is None:
            return "my hands aren't set up in this run"
        if name == "comment":
            url = _url(rest)
            text = rest.replace(url, "", 1).strip() if url else rest
            if not url:
                return "send me the video link too: .comment <url> your words"
            result = await action.comment(url, text)
        elif name == "post":
            path, _, caption = rest.partition(" ")
            result = await action.upload(path, caption=caption.strip())
        elif writes[name][1] == "url":
            url = _url(rest)
            if not url:
                return "which video? send me the link"
            result = await getattr(action, writes[name][0])(url)
        else:
            handle = _handle(rest)
            if not handle:
                return "who? send me their @handle"
            result = await getattr(action, writes[name][0])(handle)
        bot.stats["actions"] += 1
        return receipt(result.action, target=rest, ok=result.ok)

    # An unknown dot command is a sentence, not an error: she answers it.
    return await bot.answer(raw, message, owner=owner)


async def _remind(bot: Any, rest: str, *, owner: bool) -> str:
    """``.remind 30m water`` — the scheduler, if this runtime has one."""
    if not owner:
        return refusal("budget", "reminders")
    seconds = _seconds(rest)
    what = _REMIND.sub(lambda m: m.group("what"), rest.translate(_PERSIAN_DIGITS)).strip()
    if not seconds:
        return "how long? .remind 30m water"
    scheduler = getattr(getattr(bot, "runtime", None), "scheduler", None)
    if scheduler is None:
        return f"noted — {what} in {seconds // 60}m (no scheduler in this run, so it won't ping you)"
    from datetime import datetime, timedelta

    run_at = datetime.now(UTC) + timedelta(seconds=seconds)
    try:
        await scheduler.enqueue("reminder", run_at, {"text": what})
    except Exception as exc:  # noqa: BLE001
        return f"couldn't set that reminder: {exc}"
    return f"ok — {what} in {seconds // 60}m"


async def _draw(bot: Any, prompt: str) -> str:
    """``.draw cats`` — the same image generator, returning a path to attach."""
    if not prompt:
        return "draw what?"
    runtime = getattr(bot, "runtime", None)
    generator = getattr(runtime, "image_gen", None)
    if generator is None:
        return "no picture server in this run"
    try:
        result = await generator.draw(prompt)
    except Exception as exc:  # noqa: BLE001
        return f"couldn't draw that: {exc}"
    path = getattr(result, "path", "") or str(result)
    return f"drawn: {path}"
