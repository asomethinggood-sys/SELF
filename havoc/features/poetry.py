"""Poetry: ``.poem`` (شعر) and ``.fal`` (فال).

``.poem`` picks one poem from the shipped corpus and lays it out as a quotation:
the body in a blockquote, the poet in italics under it, the title in a quiet
footer. ``.poem poets`` lists the corpus; ``.poem حافظ`` (or ``.poem hafez``)
draws only from that poet.

``.fal`` is a Hafez divination. The point of a fal is the picture — a printed
card with the couplets and the interpretation — so it renders a PNG and sends
that, nothing else. Text mode exists for two cases: the ``text`` argument, and a
machine without Pillow (then Yura says so instead of silently sending less).

Feature areas: 27 (offline content), 63 (Persian display), 100 (media output).
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any

from .. import corpus, ui
from ..command_registry import Command, CommandContext, CommandError
from ..services import fal_image
from ..utils.formatting import code, escape
from ..utils.text import truncate

#: Poets whose name is a Latin transliteration: `accept both`.
_LANGUAGE_WORDS = {
    "fa": "fa",
    "farsi": "fa",
    "persian": "fa",
    "فارسی": "fa",
    "en": "en",
    "english": "en",
    "انگلیسی": "en",
}

#: Long poems are collapsed so a chat is not filled by one message.
_COLLAPSE_AFTER = 10


# ---------------------------------------------------------------------------
# .poem
# ---------------------------------------------------------------------------
async def cmd_poem(ctx: CommandContext) -> None:
    action = (ctx.sub or "").lower()
    words = [word for word in ctx.args if word.strip()]

    if action in {"poets", "list", "ls"} or (words and words[0].lower() in {"poets", "list"}):
        await ctx.reply(_poet_index(ctx))
        return

    if action in {"search", "find", "جستجو"} or (words and words[0].lower() in {"search", "find"}):
        query = " ".join(words[1:] if words and words[0].lower() in {"search", "find"} else words).strip()
        if not query:
            raise CommandError("poem.search_needs_word", prefix=ctx.runtime.prefix)
        matches = corpus.search_poems(query)
        if not matches:
            raise CommandError("poem.no_match", query=escape(query), prefix=ctx.runtime.prefix)
        poem = random.SystemRandom().choice(matches)
        await ctx.reply(_poem_card(poem, note=ctx.t("poem.found_in", count=len(matches), query=escape(query))))
        return

    query = " ".join(words).strip()
    language: str | None = None
    poet: str | None = None
    topic_note = ""

    if query:
        if query.lower() in _LANGUAGE_WORDS:
            language = _LANGUAGE_WORDS[query.lower()]
        else:
            # A name first, then a topic, then a plain word search: "شعر ایرج‌میرزا"
            # and "شعر شراب" are the same shape of request.
            poet = corpus.resolve_poet(query)
            if poet is None:
                terms = corpus.topic_terms(query)
                matches = corpus.search_poems(query, terms=terms or None)
                if matches:
                    poem = random.SystemRandom().choice(matches)
                    await ctx.reply(
                        _poem_card(
                            poem,
                            note=ctx.t(
                                "poem.found_in",
                                count=len(matches),
                                query=escape(terms[0] if terms else query),
                            ),
                        )
                    )
                    return
                raise CommandError(
                    "poem.unknown_poet",
                    query=escape(query),
                    prefix=ctx.runtime.prefix,
                )

    # Default to the chat's language; fall back to whichever corpus exists.
    if language is None and poet is None:
        language = ctx.language

    try:
        poem = corpus.sample_poem(language=language, poet=poet)
    except corpus.CorpusError:
        # A Persian chat asking for a Persian poem on a corpus with no Persian
        # poems (or vice versa) should still get a poem, not an error.
        try:
            poem = corpus.sample_poem(poet=poet)
        except corpus.CorpusError as exc:
            raise CommandError("poem.empty") from exc

    await ctx.reply(_poem_card(poem, note=topic_note))


def _poem_card(poem: dict[str, Any], *, note: str = "") -> str:
    text = str(poem.get("text", "")).strip()
    lines = text.count("\n") + 1
    # Persian verse is set in italics: it separates the quoted couplets from
    # Yura's own voice, which is never italic.
    card = ui.quote_card(
        text,
        str(poem.get("poet", "")),
        expandable=lines > _COLLAPSE_AFTER,
        italics=poem.get("language") == "fa",
    )
    title = str(poem.get("title", "")).strip()
    blocks = [card]
    if title and title not in text:
        blocks.append(ui.note(escape(truncate(title, 90))))
    if note:
        blocks.append(ui.footer(note))
    return "\n".join(blocks)


def _poet_index(ctx: CommandContext) -> str:
    table = corpus.poets()
    rows: list[str] = []
    for name, count in table.items():
        marker = "❧" if any("\u0600" <= char <= "\u06ff" for char in name) else "⌖"
        rows.append(f"{marker} {escape(name)} {ui.note(ui.DOT)} {code(str(count))}")
    header = ui.title("❦", ctx.t("poem.poets_header", count=len(table), poems=corpus.poem_count()))
    return "\n\n".join([header, ui.panel(rows), ui.footer(ctx.t("poem.poets_hint", prefix=ctx.runtime.prefix))])


# ---------------------------------------------------------------------------
# .fal
# ---------------------------------------------------------------------------
async def cmd_fal(ctx: CommandContext) -> None:
    wants_text = (ctx.sub or "").lower() in {"text", "متن"} or "text" in ctx.args
    numeric = next((word for word in (ctx.sub or "", *ctx.args) if word.isdigit()), "")

    try:
        reading = corpus.sample_fal(reading_id=int(numeric) if numeric else None)
    except corpus.CorpusError as exc:
        raise CommandError("fal.empty", detail=escape(str(exc))) from exc

    usable, reason = fal_image.available()
    if wants_text or not usable:
        await ctx.reply(_fal_text(reading, ctx, reason=reason if not usable and not wants_text else ""))
        return

    try:
        payload = fal_image.render_fal(reading)
    except (OSError, ValueError) as exc:  # font missing, Pillow broken
        ctx.runtime.logger.warning("fal: rendering failed (%s), sending text instead", exc)
        await ctx.reply(_fal_text(reading, ctx, reason="render"))
        return

    handle = tempfile.NamedTemporaryFile(prefix="yura-fal-", suffix=".png", delete=False)
    path = Path(handle.name)
    try:
        path.write_bytes(payload)
        result = await ctx.sender.send_media(ctx.chat_id, str(path), reply_to=getattr(ctx.message, "id", None))
    except OSError as exc:
        ctx.runtime.logger.warning("fal: could not write the card (%s)", exc)
        await ctx.reply(_fal_text(reading, ctx, reason="render"))
        return
    finally:
        handle.close()
        path.unlink(missing_ok=True)

    if result.dry_run:
        # Dry run must not imply a picture was delivered.
        await ctx.reply(ctx.t("fal.dry_run", id=reading["id"]))
        return
    ctx.runtime.logger.info("fal: sent reading #%s as an image (%d KB)", reading["id"], len(payload) // 1024)


def _fal_text(reading: dict[str, Any], ctx: CommandContext, *, reason: str = "") -> str:
    verses = "\n".join(str(verse) for verse in reading.get("verses", []))
    blocks = [
        ui.title("☾", ctx.t("fal.title")),
        ui.quote_card(verses, "حافظ", expandable=True),
        ui.section("✧", ctx.t("fal.meaning"), [escape(" ".join(str(reading.get("interpretation", "")).split()))]),
    ]
    if reason == "pillow":
        blocks.append(ui.footer(ctx.t("fal.missing_pillow")))
    elif reason == "arabic-reshaper":
        blocks.append(ui.footer(ctx.t("fal.missing_shaping")))
    elif reason:
        blocks.append(ui.footer(ctx.t("fal.render_failed")))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "poem",
                cmd_poem,
                category="fun",
                feature="poetry",
                requires_feature=True,
                aliases=("poetry", "verse"),
                usage="[poet|fa|en|search <word>]",
                subcommands=("poets", "list", "ls", "search", "find", "fa", "en", "persian", "english", "farsi"),
                parameters=(
                    "poet — draw only from this poet",
                    "search <word> — a random poem that contains that word",
                ),
            ),
            Command(
                "fal",
                cmd_fal,
                category="fun",
                feature="poetry",
                requires_feature=True,
                aliases=("hafez", "divination"),
                usage="[text] [number]",
                subcommands=("text",),
                parameters=("text — send the reading as a message instead of a card", "number — replay reading #n"),
            ),
        ]
    )
