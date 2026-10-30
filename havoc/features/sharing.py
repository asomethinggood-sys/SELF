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
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate

__all__ = ["register"]

_POINT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


def _poll_media(question: str, options: list[str], failed_key: str) -> Any:
    """Build a poll Telegram will actually accept — and prove it can be serialised.

    Telethon 1.4x serialises ``question``/``text`` as ``TextWithEntities`` and
    requires ``hash``; a plain string builds fine and then explodes inside the
    request, which would look like "Telegram refused" when it was our own bug.
    ``_bytes()`` here is the same call the client makes, so a bad poll is caught
    before anything is queued.
    """
    from telethon.tl.types import InputMediaPoll, Poll, PollAnswer, TextWithEntities

    try:
        poll = Poll(
            id=0,
            question=TextWithEntities(text=question[:255], entities=[]),
            answers=[
                PollAnswer(
                    text=TextWithEntities(text=option[:100], entities=[]),
                    option=bytes([index]),
                )
                for index, option in enumerate(options)
            ],
            hash=0,
            quiz=False,
            public_voters=False,
            multiple_choice=False,
        )
        media = InputMediaPoll(poll=poll)
        media._bytes()
    except Exception as exc:  # noqa: BLE001 - report the real reason, do not guess
        raise CommandError(failed_key, detail=escape(str(exc)[:180])) from exc
    return media


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

    sender = getattr(ctx.runtime.client, "send_file", None)
    if sender is None:
        raise CommandError("share.failed", detail="this build cannot send a poll")
    media = _poll_media(question, options, ctx.t("share.failed"))
    try:
        await sender(ctx.chat_id, media)
    except Exception as exc:  # noqa: BLE001
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

    from telethon.tl.types import InputGeoPoint, InputMediaGeoPoint

    sender = getattr(ctx.runtime.client, "send_file", None)
    if sender is None:
        raise CommandError("share.failed", detail="this build cannot send a location")
    try:
        await sender(
            ctx.chat_id,
            InputMediaGeoPoint(geo_point=InputGeoPoint(lat=lat, long=lon)),
        )
    except Exception as exc:  # noqa: BLE001
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
