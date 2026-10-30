"""The things you ask her for, as opposed to the things she says on her own.

``.yura recap`` · ``.yura diary`` · ``.yura faces`` · ``.yura say`` ·
``.yura voice`` · ``.yura poster`` · ``.yura caption`` · ``.yura react``

Each one is deliberately small and each one degrades to a plain sentence when the
thing it needs is missing — no poster files, no TTS package, no picture yet. A
command that fails silently is worse than a command that says "not yet".
"""

from __future__ import annotations

import random
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..command_registry import CommandContext
from ..services.tts import SPEECH_LANGUAGES, TextToSpeech, detect_speech_language
from ..utils.formatting import bold, escape
from ..utils.markup import rich
from ..utils.mood import mood_line
from ..utils.time import to_local, utc_now
from ..utils.typeface import rule

__all__ = [
    "FACE_BOOK",
    "POSTERS",
    "caption",
    "diary",
    "faces",
    "poster",
    "react",
    "recap",
    "say",
    "tts_for",
    "voice",
]

#: Her faces, by what she is feeling. Not a menu of scenarios: a vocabulary.
FACE_BOOK: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "smug": ("(¬‿¬)", "⌐■_■", "╰(▔∀▔)╯"),
        "hurt": ("(｡•́︿•̀｡)", "(っ- ‸ - ς)", "ಥ_ಥ"),
        "feral": ("(╯°□°）╯", "ᕦ(ò_óˇ)ᕤ", "(҂◡_◡)"),
        "sleepy": ("(－_－) zzZ", "(ᵕ—ᴗ—)", "⌯⌲"),
        "soft": ("♡( ◡‿◡ )", "(´｡• ᵕ •｡`)", "٩(◕‿◕)۶"),
        "watching": ("(¬_¬)", "ಠ_ಠ", "☉_☉"),
    },
    "fa": {
        "smug": ("(¬‿¬)", "⌐■_■", "╰(▔∀▔)╯"),
        "hurt": ("(｡•́︿•̀｡)", "(っ- ‸ - ς)", "ಥ_ಥ"),
        "feral": ("(╯°□°）╯", "ᕦ(ò_óˇ)ᕤ", "(҂◡_◡)"),
        "sleepy": ("(－_－) zzZ", "(ᵕ—ᴗ—)", "⌯⌲"),
        "soft": ("♡( ◡‿◡ )", "(´｡• ᵕ •｡`)", "٩(◕‿◕)۶"),
        "watching": ("(¬_¬)", "ಠ_ಠ", "☉_☉"),
    },
}

#: Where the poster images live. Empty until the build workflow has run once.
POSTER_DIR = Path(__file__).resolve().parent.parent / "data" / "posters"
POSTERS = POSTER_DIR / "index.json"

#: Telegram only accepts a fixed set for a reaction, and its meaning is fixed
#: too: these read as "noted", "yes", "warm", "sharp". Symbols cannot be used
#: here — the API rejects anything outside the allowed set.
REACTIONS = ("❤", "🔥", "👀", "🙈", "💯", "⚡")


def tts_for(runtime: Any) -> TextToSpeech:
    """One instance per runtime, so counters and errors are not per-message."""
    engine = getattr(runtime, "tts", None)
    if engine is None:
        engine = TextToSpeech(runtime)
        runtime.tts = engine
    return engine


def _local_hour(ctx: CommandContext) -> int:
    try:
        return to_local(utc_now(), ctx.runtime.config.timezone).hour
    except Exception:  # pragma: no cover - bad zone
        return 12


def _clock(ctx: CommandContext, moment: Any) -> str:
    try:
        return to_local(moment, ctx.runtime.config.timezone).strftime("%H:%M")
    except Exception:  # pragma: no cover - bad zone
        return "--:--"


# ---------------------------------------------------------------------------
# recap / diary
# ---------------------------------------------------------------------------
def _recap_lines(rows: list[tuple[str, str, Any]], ctx: CommandContext, limit: int = 8) -> list[str]:
    """The last few things that were said, with the time each was said."""
    lines: list[str] = []
    for role, content, moment in rows[-limit:]:
        who = "you" if role == "user" else "me"
        said = " ".join(str(content).split())[:110]
        lines.append(f"`{_clock(ctx, moment)}` *{who}* — {escape(said)}")
    return lines


async def recap(ctx: CommandContext) -> None:
    """Today, in a handful of lines with times."""
    since = utc_now() - timedelta(hours=24)
    rows = await ctx.runtime.memory.recent_with_times(ctx.chat_id, since=since, limit=120)
    header = bold(ctx.t("yura.recap_title"))
    if not rows:
        await ctx.reply(escape(ctx.t("yura.recap_empty")))
        return
    lines = _recap_lines(rows, ctx)
    tone = f"*{escape(mood_line(_local_hour(ctx)))}*"
    await ctx.reply("\n".join([header, *lines, f"_{tone}_"]))


async def diary(ctx: CommandContext) -> None:
    """The same day, written down and sent as a file."""
    since = utc_now() - timedelta(hours=24)
    rows = await ctx.runtime.memory.recent_with_times(ctx.chat_id, since=since, limit=200)
    if not rows:
        await ctx.reply(escape(ctx.t("yura.recap_empty")))
        return
    memories = await ctx.runtime.memory.list(ctx.chat_id, limit=30)
    today = _clock(ctx, utc_now())
    body = [f"# diary — {today}", ""]
    body += [f"- **{_clock(ctx, moment)}** {role}: {content}" for role, content, moment in rows]
    if memories:
        body += ["", "## what i keep", ""]
        body += [f"- {memory.text}" for memory in memories]
    folder = Path(ctx.runtime.config.data_dir) / "yura"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"diary-{utc_now().strftime('%Y%m%d-%H%M')}.md"
    target.write_text("\n".join(body) + "\n", encoding="utf-8")
    await ctx.runtime.sender.send_media(
        ctx.chat_id,
        str(target),
        caption=bold(escape(ctx.t("yura.diary_caption", count=len(rows)))),
        reply_to=ctx.message.id if getattr(ctx, "message", None) is not None else None,
    )


# ---------------------------------------------------------------------------
# faces
# ---------------------------------------------------------------------------
async def faces(ctx: CommandContext, words: list[str]) -> None:
    """Her faces, by mood — and one picked if a mood was named."""
    book = FACE_BOOK.get(ctx.language) or FACE_BOOK["en"]
    wanted = (words[0].lower() if words else "").strip()
    if wanted:
        key = wanted if wanted in book else next((k for k in book if k.startswith(wanted)), None)
        if key is None:
            await ctx.reply(
                escape(ctx.t("yura.faces_unknown", mood=wanted, moods=", ".join(book)))
            )
            return
        faces_list = " ".join(book[key])
        await ctx.reply(f"{bold(key)}\n{escape(faces_list)}")
        return
    lines = [bold(escape(ctx.t("yura.faces_title")))]
    for mood, options in book.items():
        lines.append(f"*{escape(mood)}* — {escape(' '.join(options))}")
    lines.append(f"_{escape(ctx.t('yura.faces_hint'))}_")
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# speech
# ---------------------------------------------------------------------------
async def say(ctx: CommandContext, text: str) -> None:
    """One voice note, only when asked for."""
    spoken = (text or "").strip()
    if not spoken:
        await ctx.reply(escape(ctx.t("yura.say_needs_text")))
        return
    engine = tts_for(ctx.runtime)
    # The script of the sentence decides the mouth: Russian and Japanese are read
    # by their own voices, not by the English one.
    language = detect_speech_language(spoken)
    result = await engine.say(spoken, language)
    if result is None:
        await ctx.reply(
            escape(
                ctx.t(
                    "yura.say_failed",
                    error=engine.last_error or "unknown",
                    prefix=ctx.runtime.prefix,
                )
            )
        )
        return
    folder = Path(ctx.runtime.config.data_dir) / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{utc_now().strftime('%Y%m%d-%H%M%S')}{result.suffix()}"
    target.write_bytes(result.audio)
    await ctx.runtime.sender.send_media(
        ctx.chat_id,
        str(target),
        caption=f"{rule()} {escape(result.voice)}",
        voice_note=True,
        reply_to=ctx.message.id if getattr(ctx, "message", None) is not None else None,
    )


async def voice(ctx: CommandContext, words: list[str]) -> None:
    """Show or set the voice. The key is stored base64 and never echoed."""
    engine = tts_for(ctx.runtime)
    action = (words[0].lower() if words else "").strip()
    if action in {"key", "token"}:
        key = words[1] if len(words) > 1 else ""
        if not key:
            await ctx.reply(escape(ctx.t("yura.voice_key_usage", prefix=ctx.runtime.prefix)))
            return
        await engine.set_key(key)
        _best_effort_delete(ctx)
        await ctx.reply_ok(escape(ctx.t("yura.voice_key_saved")))
        return
    if action in {"off", "edge"}:
        await ctx.runtime.store.set_setting("tts.provider", "edge")
        await ctx.reply_ok(escape(ctx.t("yura.voice_edge")))
        return
    if action in {"groq", "openai", "on"}:
        await ctx.runtime.store.set_setting("tts.provider", "openai")
        await ctx.reply_ok(
            escape(ctx.t("yura.voice_groq", prefix=ctx.runtime.prefix))
        )
        return
    state = await engine.describe()
    spoken = engine.spoken
    await ctx.reply(
        "\n".join(
            [
                bold(escape(ctx.t("yura.voice_title"))),
                f"`{escape(state)}`",
                escape(ctx.t("yura.voice_spoken", count=spoken)),
                escape(ctx.t("yura.voice_languages", languages=" · ".join(SPEECH_LANGUAGES))),
                f"_{escape(ctx.t('yura.voice_hint', prefix=ctx.runtime.prefix))}_",
            ]
        )
    )




def _best_effort_delete(ctx: CommandContext) -> None:
    """A key typed into a chat is a key in a chat. Remove it if we can."""
    message = getattr(ctx, "message", None)
    if message is None:
        return
    try:
        ctx.runtime.client  # noqa: B018 - presence check only
    except Exception:  # pragma: no cover - defensive
        return
    delete = getattr(ctx.runtime.client, "delete_messages", None)
    if delete is None:
        return
    try:
        import asyncio

        asyncio.create_task(delete(ctx.chat_id, [message.id]))
    except Exception:  # pragma: no cover - deleting is a courtesy
        pass


# ---------------------------------------------------------------------------
# posters
# ---------------------------------------------------------------------------
def poster_index() -> list[dict[str, Any]]:
    """The built posters, or an empty list when the workflow has not run."""
    try:
        import json

        data = json.loads(POSTERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("file")]


async def poster(ctx: CommandContext, words: list[str]) -> None:
    """One verse, as a picture."""
    index = poster_index()
    if not index:
        await ctx.reply(escape(ctx.t("yura.poster_missing")))
        return
    wanted = " ".join(words).strip().lower()
    options = [
        item
        for item in index
        if not wanted or wanted in str(item.get("poet", "")).lower()
        or wanted in str(item.get("title", "")).lower()
    ] or index
    choice = random.choice(options)
    path = POSTER_DIR / str(choice["file"])
    if not path.exists():
        await ctx.reply(escape(ctx.t("yura.poster_missing")))
        return
    caption = f"{bold(escape(str(choice.get('poet', ''))))}\n*{escape(str(choice.get('title', '')))}*"
    await ctx.runtime.sender.send_media(
        ctx.chat_id,
        str(path),
        caption=caption,
        reply_to=ctx.message.id if getattr(ctx, "message", None) is not None else None,
    )


# ---------------------------------------------------------------------------
# caption / react
# ---------------------------------------------------------------------------
async def caption(ctx: CommandContext) -> None:
    """A second look at the last picture she was shown."""
    answer = await ctx.runtime.yura_chat.caption_last_picture(ctx.chat_id, "en")
    text = answer.text if answer.source == "model" else ctx.t("yura.caption_none")
    await ctx.reply(rich(text) if answer.source == "model" else escape(text))


async def react(ctx: CommandContext, words: list[str]) -> None:
    """React to the message being replied to, with a symbol from a fixed set."""
    message = getattr(ctx, "message", None)
    target = getattr(message, "reply_to_msg_id", None) if message is not None else None
    if not target:
        await ctx.reply(escape(ctx.t("yura.react_needs_reply")))
        return
    wanted = (words[0] if words else "").strip()
    emoji = wanted if wanted in REACTIONS else random.choice(REACTIONS)
    ok = await ctx.runtime.sender.react(ctx.chat_id, target, emoji)
    if ok:
        await ctx.reply(escape(ctx.t("yura.react_done", emoji=emoji)))
    else:
        await ctx.reply(escape(ctx.t("yura.react_failed")))

