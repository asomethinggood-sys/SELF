"""Yura's own voice as commands and one carefully gated automation.

``.yura`` never sends on its own: it shows a card, lists scenarios, or drafts a
reply the owner can send with ``.yura send``. Automatic replies live behind the
``persona`` feature switch and only ever happen in the owner's own chat with
the owner's own messages, so the bot can never start talking to someone else.

Feature areas: 12 (personality), 21 (messaging), 96 (feature switches).
"""

from __future__ import annotations

import time
from typing import Any

from .. import persona, ui
from ..command_registry import Command, CommandContext, CommandError
from ..event_engine import EventType, NormalizedEvent
from ..identity import tagline
from ..utils.formatting import bold, code, escape
from ..utils.security import redact_text
from ..utils.text import truncate

#: Never answer more often than this, whatever the owner types.
MIN_INTERVAL_SECONDS = 45
#: ...and never more than this many times in a rolling window.
MAX_PER_HOUR = 12

DRAFT_KEY = "persona.draft"


async def _card(ctx: CommandContext) -> str:
    enabled = await ctx.runtime.feature_manager.is_enabled("persona")
    state = ui.state(
        enabled, on=ctx.t("common.on"), off=ctx.t("common.off")
    )
    facts = [
        f"{ui.bold(ctx.t('yura.state'))} {state}",
        f"{ui.bold(ctx.t('yura.scenarios'))} {code(str(len(persona.SCENARIOS)))}",
        f"{ui.bold(ctx.t('yura.mode'))} {ui.note(escape(ctx.t('yura.mode_offline')))}",
    ]
    return "\n\n".join(
        [
            ui.title("♡", ctx.t("yura.card_title", name=escape(_name(ctx.language)))),
            ui.note(escape(tagline(ctx.language))),
            ui.panel(facts),
            ui.footer(ctx.t("yura.card_hint", prefix=ctx.runtime.prefix)),
        ]
    )


def _name(language: str) -> str:
    from ..identity import display

    return display(language)


def _scenario_list(ctx: CommandContext) -> str:
    labels = persona.labels(ctx.language)
    rows = [ui.item(scenario.key, labels[scenario.key]) for scenario in persona.SCENARIOS]
    return "\n\n".join(
        [
            ui.title("♡", ctx.t("yura.list_header")),
            ui.panel(rows),
            ui.footer(ctx.t("yura.list_hint", prefix=ctx.runtime.prefix)),
        ]
    )


async def _set_enabled(ctx: CommandContext, enabled: bool) -> None:
    await ctx.store.set_feature("persona", enabled)
    await ctx.runtime.feature_manager.refresh()
    await ctx.reply_ok(
        ctx.t("yura.switched", name=escape(_name(ctx.language)), state=ctx.t("common.on" if enabled else "common.off"))
    )
    if enabled:
        await ctx.reply(ctx.t("yura.enabled_hint", prefix=ctx.runtime.prefix))


async def cmd_yura(ctx: CommandContext) -> None:
    action = (ctx.sub or "").lower()
    runtime = ctx.runtime
    # ``.yura tired`` has no subcommand: the parser demotes an unknown first
    # token into the arguments, so that token is the scenario.
    if not action and ctx.args:
        action = " ".join(ctx.args).strip().lower()

    if action in {"", "status", "who", "card", "me"}:
        await ctx.reply(await _card(ctx))
        return
    if action in {"on", "off", "enable", "disable"}:
        await _set_enabled(ctx, action in {"on", "enable"})
        return
    if action in {"list", "scenarios", "ls"}:
        await ctx.reply(_scenario_list(ctx))
        return

    if action == "send":
        drafted = await ctx.store.get_chat_setting(ctx.chat_id, DRAFT_KEY, None)
        if not drafted:
            raise CommandError("yura.no_draft", prefix=runtime.prefix)
        text = str(drafted)
        await runtime.sender.send(ctx.chat_id, escape(text))
        await ctx.store.delete_chat_setting(ctx.chat_id, DRAFT_KEY)
        await ctx.reply_ok(ctx.t("yura.sent"))
        return

    if action == "reply":
        # Draft from a replied-to message, exactly like the automatic path but
        # without spending the rate budget.
        reply = await ctx.reply_message()
        if reply is None:
            raise CommandError("errors.reply_required", prefix=runtime.prefix, name="yura")
        source = getattr(reply, "raw_text", None) or getattr(reply, "message", None) or ""
        scenario = persona.detect(str(source), ctx.language)
        if scenario is None:
            raise CommandError("yura.no_scenario", prefix=runtime.prefix)
        line = persona.build_line(scenario, ctx.language)
        await ctx.store.set_chat_setting(ctx.chat_id, DRAFT_KEY, line)
        await ctx.reply(
            "\n".join(
                [
                    bold(ctx.t("yura.draft_header", scenario=escape(scenario))),
                    escape(line),
                    ctx.t("yura.draft_hint", prefix=runtime.prefix),
                ]
            )
        )
        return

    # An explicit scenario name wins over anything else in the arguments.
    scenario = persona.resolve_scenario(action) or persona.resolve_scenario(" ".join(ctx.args))
    if scenario is None:
        if action:
            raise CommandError(
                "errors.invalid_choice",
                field="scenario",
                choices=", ".join(s.key for s in persona.SCENARIOS),
            )
        await ctx.reply(await _card(ctx))
        return

    line = persona.build_line(scenario.key, ctx.language)
    await ctx.reply(escape(line))


class PersonaReplies:
    """The only automatic behaviour: a warm reply to the owner's own message.

    Gates, in order — every one of them must pass:

    1. the message is outgoing (the owner typed it),
    2. it is in the owner's own Saved Messages chat,
    3. the ``persona`` feature is enabled,
    4. :meth:`Permissions.can_act` allows it (emergency stop, quiet hours,
       private-chat policy all live there),
    5. the rate budget is intact,
    6. something in the text actually matches a scenario.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        # None, not 0.0: monotonic() has no defined origin, and on a freshly
        # started machine or container it can be smaller than the cooldown,
        # which would silence the first reply for no reason.
        self._last_at: float | None = None
        self._window: list[float] = []
        self.answered = 0
        self.skipped = 0

    @property
    def self_id(self) -> int | None:
        """Read live: this is created before login, when the id is still None."""
        return self.runtime.permissions.self_id

    def _budget_ok(self, now: float) -> bool:
        if self._last_at is not None and now - self._last_at < MIN_INTERVAL_SECONDS:
            return False
        cutoff = now - 3600
        self._window = [stamp for stamp in self._window if stamp >= cutoff]
        return len(self._window) < MAX_PER_HOUR

    async def on_self_message(self, event: NormalizedEvent) -> None:
        if not event.is_outgoing or not event.text.strip():
            return
        self_id = self.self_id
        if self_id is None or event.chat_id != self_id:
            self.skipped += 1
            return
        if persona.is_own_line(event.text) or self.runtime.prefix and event.text.strip().startswith(self.runtime.prefix):
            # Our own output, or a command the dispatcher already answered.
            self.skipped += 1
            return
        decision = await self.runtime.permissions.can_act(event.chat_id, "persona", is_private=True)
        if not decision.allowed:
            self.skipped += 1
            return
        scenario = persona.detect(event.text, event.language if event.language in {"en", "fa"} else None)
        if scenario is None:
            self.skipped += 1
            return
        now = time.monotonic()
        if not self._budget_ok(now):
            self.skipped += 1
            return

        line = persona.build_line(scenario, await self._language(event))
        self._last_at = now
        self._window.append(now)
        self.answered += 1
        self.logger.info(
            "persona: answering '%s' in Saved Messages", redact_text(truncate(event.text, 40))
        )
        await self.runtime.sender.send(event.chat_id, escape(line))

    async def _language(self, event: NormalizedEvent) -> str:
        if event.language in {"en", "fa"}:
            return event.language
        owner = self.runtime.permissions.self_id
        if owner:
            stored = await self.runtime.store.get_language("user", str(owner))
            if stored in {"en", "fa"}:
                return stored
        return self.runtime.config.default_language


def register(runtime: Any) -> None:
    runtime.registry.register(
        Command(
            "yura",
            cmd_yura,
            category="persona",
            aliases=("person", "self"),
            usage="[on|off|list|reply|send|<scenario>]",
            changes_state=True,
            subcommands=(
                "on", "off", "enable", "disable", "status", "list", "ls", "scenarios",
                "reply", "send", "who", "card",
                *persona.SCENARIO_BY_KEY.keys(),
            ),
        )
    )
    replies = PersonaReplies(runtime)
    runtime.persona_replies = replies
    runtime.event_engine.subscribe(
        "persona.self_message",
        replies.on_self_message,
        {EventType.OUTGOING},
        priority=40,
        description="warm replies to the owner's own messages",
    )
