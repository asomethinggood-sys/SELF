"""Facts: remember, forget, and read back what she keeps.

The memory table already existed; what was missing was a way to *use* it without
going through ``.yura``. These are the three sentences people actually say —
"remember that…", "forget what I said about…", "what do you remember" — and one
recall that shows the ids, so a specific fact can be removed by number when the
words are too vague.

Rules:

* a duplicate is not stored twice (the store already refuses it; the reply says
  so rather than pretending);
* ``.forget`` without a match **says** nothing matched, instead of reporting a
  success that removed nothing;
* a number removes one row, words remove every row that mentions them — both are
  named in the answer, because "I forgot something" is not an answer.
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate

__all__ = ["register"]

#: One fact, in his words. Long enough for a real sentence, short enough that
#: recall does not become a wall.
MAX_FACT = 400


def _text(ctx: CommandContext) -> str:
    return (ctx.rest_raw or " ".join(ctx.args)).strip().strip(" .,")


async def cmd_remember(ctx: CommandContext) -> None:
    body = _text(ctx)
    if not body:
        raise CommandError("facts.usage_remember", prefix=ctx.runtime.prefix)
    body = truncate(" ".join(body.split()), MAX_FACT)
    made = await ctx.runtime.memory.remember(ctx.chat_id, body)
    if not made:
        await ctx.reply(ctx.t("facts.already_known", fact=escape(body)))
        return
    total = await ctx.runtime.memory.count(ctx.chat_id)
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("facts.stored")),
                ctx.t("facts.stored_line", fact=escape(body), count=total),
            ]
        )
    )


async def cmd_forget(ctx: CommandContext) -> None:
    raw = _text(ctx)
    if not raw:
        raise CommandError("facts.usage_forget", prefix=ctx.runtime.prefix)
    if raw.lstrip("#").isdigit():
        removed = await ctx.runtime.memory.forget(ctx.chat_id, int(raw.lstrip("#")))
        if not removed:
            await ctx.reply(ctx.t("facts.no_such_id", id=raw))
            return
        await ctx.reply(ctx.t("facts.forgot_one", id=code(raw.lstrip("#"))))
        return
    removed = await ctx.runtime.memory.forget_matching(ctx.chat_id, raw)
    if not removed:
        await ctx.reply(ctx.t("facts.nothing_matched", query=escape(raw)))
        return
    left = await ctx.runtime.memory.count(ctx.chat_id)
    await ctx.reply(
        ctx.t("facts.forgot_many", count=removed, query=escape(raw), left=left)
    )


async def cmd_memories(ctx: CommandContext) -> None:
    try:
        wanted = int(ctx.arg(0) or 15)
    except ValueError:
        wanted = 15
    rows = await ctx.runtime.memory.list(ctx.chat_id, limit=max(1, min(wanted, 50)))
    if not rows:
        await ctx.reply(ctx.t("facts.empty"))
        return
    total = await ctx.runtime.memory.count(ctx.chat_id)
    lines = [bold(ctx.t("facts.header", count=total))]
    for row in rows:
        lines.append(f"{code('#' + str(row.id))} {escape(truncate(row.content, 160))}")
    lines.append(ctx.t("facts.hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


async def cmd_recall(ctx: CommandContext) -> None:
    """``.recall <words>`` — what she remembers that mentions them."""
    query = _text(ctx)
    if not query:
        raise CommandError("facts.usage_recall", prefix=ctx.runtime.prefix)
    words = [word for word in query.lower().split() if len(word) > 2]
    rows = await ctx.runtime.memory.recall(ctx.chat_id, limit=400)
    hits = [
        row
        for row in rows
        if any(word in " ".join(row.content.lower().split()) for word in words)
    ]
    if not hits:
        await ctx.reply(ctx.t("facts.nothing_about", query=escape(query)))
        return
    lines = [bold(ctx.t("facts.recall_header", query=escape(query), count=len(hits)))]
    for row in hits[:10]:
        lines.append(f"{code('#' + str(row.id))} {escape(truncate(row.content, 200))}")
    await ctx.reply("\n".join(lines))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "remember",
                cmd_remember,
                category="memory",
                aliases=("keep",),
                usage="<fact>",
                changes_state=True,
                description="Keep one sentence about you, and use it later.",
                examples=(".remember Sara's birthday is 3 May",),
            ),
            Command(
                "forget",
                cmd_forget,
                category="memory",
                usage="<words|#id>",
                changes_state=True,
                description="Drop what she remembers about something, or one fact by number.",
                examples=(".forget the flat", ".forget #12"),
            ),
            Command(
                "memories",
                cmd_memories,
                category="memory",
                aliases=("memory", "facts"),
                usage="[count]",
                description="Everything she remembers, with the numbers .forget wants.",
            ),
            Command(
                "recall",
                cmd_recall,
                category="memory",
                usage="<words>",
                description="What she remembers that mentions those words.",
            ),
        ]
    )
