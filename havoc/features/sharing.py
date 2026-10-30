"""Things that are not a sentence: a poll and a place.

Files already have one path — ``.pic`` (also reachable as ``.sendfile``), which
goes through :mod:`havoc.services.media` and its rules. This module adds the two
shapes that path cannot express:

* ``.poll <question> | option | option`` — a real ``InputMediaPoll``, not a text
  imitation of one, so people can actually vote;
* ``.geo <lat>, <lon>`` — a location point, range-checked before it is sent.

If Telegram refuses, the refusal is the answer; nothing here invents a success.

Feature area: 45 (media handling).
"""

from __future__ import annotations

import re
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate

__all__ = ["register"]

_POINT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


async def cmd_poll(ctx: CommandContext) -> None:
    body = (ctx.rest_raw or " ".join(ctx.args)).strip()
    if "|" not in body:
        raise CommandError("share.usage_poll", prefix=ctx.runtime.prefix)
    parts = [part.strip() for part in body.split("|")]
    question = parts[0]
    options = [part for part in parts[1:] if part]
    if not question or len(options) < 2:
        raise CommandError("share.usage_poll", prefix=ctx.runtime.prefix)
    if len(options) > 10:
        options = options[:10]

    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("share.failed", detail="this build cannot send a poll")
    try:
        # The poll is built and serialised by the service, so a bad one is caught
        # before anything is queued.
        await extras.send_poll(ctx.chat_id, question, options)
    except ExtrasError as exc:
        raise CommandError("share.failed", detail=escape(str(exc)[:180])) from exc
    await ctx.store.add_outbox(
        ctx.chat_id, text=question[:200], kind="poll", status="sent"
    )
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("share.poll_sent")),
                ctx.t("share.poll_line", question=escape(truncate(question, 120)), count=len(options)),
            ]
        )
    )


async def cmd_geo(ctx: CommandContext) -> None:
    raw = " ".join(ctx.args).strip()
    match = _POINT.match(raw)
    if not match:
        raise CommandError("share.usage_geo", prefix=ctx.runtime.prefix)
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise CommandError("share.geo_range")

    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("share.failed", detail="this build cannot send a location")
    try:
        await extras.send_location(ctx.chat_id, lat, lon)
    except ExtrasError as exc:
        raise CommandError("share.failed", detail=escape(str(exc)[:180])) from exc
    await ctx.store.add_outbox(
        ctx.chat_id, text=f"{lat:.5f},{lon:.5f}", kind="location", status="sent"
    )
    await ctx.reply(ctx.t("share.geo_sent", point=code(f"{lat:.5f}, {lon:.5f}")))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "poll",
                cmd_poll,
                category="media",
                usage="<question> | <option> | <option>",
                changes_state=True,
                description="Post a real poll in this chat.",
                examples=(".poll tea or coffee | tea | coffee",),
            ),
            Command(
                "geo",
                cmd_geo,
                category="media",
                usage="<lat>, <lon>",
                changes_state=True,
                description="Send a location point.",
                examples=(".geo 35.6892, 51.3890",),
            ),
        ]
    )
