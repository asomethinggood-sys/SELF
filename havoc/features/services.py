"""``.services`` — where the three hosted services actually are right now.

Three services run on their own GitHub runners: a search engine, a picture
server, and a model. Each publishes a small file when it comes up, and the bot
reads it. That works, but it works *silently* — and when a picture fails, "the
picture server is between runs" is the difference between a bot that is busy and
a bot that is broken.

This card is the answer to the only question an owner actually asks: **is it up,
where, and if not, how long has it been down?** One line per service, the age of
the last published address, the model it is serving, and the last error in
plain words.

It also probes: ``/healthz`` through the gate, with the token it computes
locally. That is a real round trip, because a published file proves that a
service *said* it was up, not that it still is.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext
from ..services.service_directory import (
    STATE_BETWEEN_RUNS,
    STATE_EXPIRED,
    STATE_KEYS,
    describe_age,
)
from ..utils.formatting import escape

#: What each service is for, so a card can say "search" and not "search (?)".
LABELS = {
    "search": ("⌕", "web search"),
    "comfy": ("✎", "pictures"),
    "ollama": ("◈", "the model"),
}

#: One probe must not make the whole card wait for a dead service three times.
PROBE_TIMEOUT = 5.0


async def cmd_services(ctx: CommandContext) -> None:
    directory = getattr(ctx.runtime, "services", None)
    if directory is None:  # pragma: no cover - always built by Runtime.create
        await ctx.reply(
            "\n\n".join([ui.title("◇", ctx.t("services.title")), ui.note(ctx.t("services.none"))])
        )
        return

    # Fresh read, then one real probe each, in parallel: the card is meant to be
    # the truth at the moment it is asked for.
    endpoints = directory.card()
    probes = await asyncio.gather(
        *(directory.probe(endpoint.name, timeout=PROBE_TIMEOUT) for endpoint in endpoints),
        return_exceptions=True,
    )

    rows: list[str] = []
    for endpoint, probe in zip(endpoints, probes, strict=False):
        icon, what = LABELS.get(endpoint.name, ("◇", endpoint.name))
        if isinstance(probe, BaseException):
            reachable, detail = False, str(probe)[:120]
        else:
            reachable, detail = probe
        state = STATE_KEYS.get(endpoint.state, "services.state_unknown")
        marker = "✓" if reachable else ("◌" if endpoint.state in {STATE_BETWEEN_RUNS, STATE_EXPIRED} else "✕")
        lines = [
            f"{ui.code(icon)} {ui.bold(escape(what))} {ui.note(escape('·'))} {escape(ctx.t(state))}"
            f" {ui.note(marker)}"
        ]
        if endpoint.url:
            host = endpoint.url.split("//", 1)[-1].split("/", 1)[0]
            lines.append(f"  {ui.code(escape(host))}")
        if endpoint.age is not None:
            lines.append(f"  {ui.note(escape(ctx.t('services.age', age=describe_age(endpoint.age, ctx.language))))}")
        if endpoint.model:
            lines.append(f"  {ui.note(escape(ctx.t('services.model', model=endpoint.model)))}")
        if not reachable and detail:
            lines.append(f"  {ui.note(escape(detail))}")
        rows.append("\n".join(lines))

    footer_key = "services.footer_live" if any(
        (probe[0] if isinstance(probe, tuple) else False) for probe in probes
    ) else "services.footer_down"
    blocks = [
        ui.title("◇", ctx.t("services.title")),
        ui.panel(rows),
        ui.footer(ctx.t(footer_key, prefix=ctx.runtime.prefix)),
    ]
    if not getattr(directory, "secret", "") and any(not endpoint.ok for endpoint in endpoints):
        # The one configuration that makes three healthy runners invisible: with
        # no gate secret they run as self-tests on loopback and publish nothing.
        # Saying it here is the difference between "broken" and "one secret".
        blocks.insert(2, ui.note(ctx.t("services.no_secret")))
    text = "\n\n".join(blocks)
    await ctx.reply(text)


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "services",
                cmd_services,
                category="information",
                aliases=("service", "hosted"),
                usage="",
                cooldown=3.0,
            )
        ]
    )
