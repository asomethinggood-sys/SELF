"""The toolbox: send, repeat, hide, self-destruct, broadcast, react, plan.

Every ability in here is one command with one implementation, and every other
route to it — a sentence, a tool call from the model, a step inside a plan — goes
through that same command. That is the difference between a bot that has features
and a bot that has *hands*: there is exactly one way to send a spoiler that
deletes itself in ten seconds, and everything that wants to do it uses it.

The shapes his messages actually take:

    .send @name hey, I'm late                    one message, one person
    .send @name hey --repeat 10 --every 30s      ten of them, paced
    .send @name,sara,amir the same thing         several people, one sentence
    .send @name secret --spoiler                 under a tap
    .send @name boo --delete-after 10s           and gone again
    .send @name good morning --at 08:00          later
    .broadcast groups --limit 100 --dry          a list, a count, a rehearsal
    .emoji ❤                                      react to what is replied to
    .plan list / .plan stop [word]                the timeline
    .outbox                                       what actually left the account
    .ignore @name / .unignore @name               do not answer them
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..services import reach as reach_module
from ..utils.formatting import bold, code, escape, strip_html
from ..utils.security import redact_text
from ..utils.text import truncate

#: A broadcast is not a conversation. Past this many targets it needs `--yes`.
BROADCAST_CONFIRM_OVER = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#: "don't answer him" — the person is whoever he replied to, not a word.
_HIM_WORDS = {
    "him", "her", "them", "this person", "that person", "this guy", "that guy",
    "این", "اون", "این شخص", "اون شخص", "همین شخص", "ایشان", "این یارو",
}

#: The words that mean "the chat I am standing in" rather than a name.
_HERE_WORDS = {
    "this", "this chat", "this group", "this room", "this channel", "the chat",
    "the group", "here", "current chat", "current group",
    "این چت", "این گروه", "همین گروه", "همین چت", "اینجا",
}


def _targets(ctx: CommandContext, raw: str) -> list[str]:
    """``@name,sara, amir`` → three names. Commas and "and" both separate."""
    text = str(raw or "").replace(" and ", ",").replace("\n", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _flag_seconds(ctx: CommandContext, name: str) -> float | None:
    # ``ctx.flag`` answers "" for a flag that was not given, not None: treating
    # the empty string as a value made every plain `.send` fail with "repeat must
    # be a number".
    raw = ctx.flag(name)
    if raw is None or raw is True or str(raw).strip() == "":
        return None
    seconds = reach_module.parse_duration(str(raw))
    if seconds is None:
        raise CommandError("errors.not_a_number", field=name)
    return seconds


def _flag_int(ctx: CommandContext, name: str, default: int) -> int:
    raw = ctx.flag(name)
    if raw is None or raw is True or str(raw).strip() == "":
        return default
    try:
        return int(str(raw))
    except ValueError as exc:
        raise CommandError("errors.not_a_number", field=name) from exc


def _split_targets_and_text(ctx: CommandContext) -> tuple[list[str], str]:
    """The targets are the first argument; everything after it is the message.

    ``.send @name hello there`` keeps "hello there" whole — the text is not a
    list of words to be re-joined wrongly.
    """
    args = [part for part in ctx.args if part.strip()]
    # ``--here``: the room the instruction was given in. Yura has to be able to
    # say "send hi in 10 messages *in this group*" without knowing its id, and
    # the chat she was called in is the only honest answer to "this group".
    here = bool(ctx.has_flag("here")) or bool(ctx.has_flag("this_chat"))
    if here:
        text = " ".join(args).strip()
        if not text:
            raise CommandError("reach.usage_send", prefix=ctx.runtime.prefix)
        return [str(ctx.chat_id)], text
    if not args:
        raise CommandError("reach.usage_send", prefix=ctx.runtime.prefix)
    # The positional arguments are already free of flags, so the body survives
    # exactly as typed — including words that look like options in a sentence.
    targets = _targets(ctx, args[0])
    if all(item.lower().strip(" .,!") in _HERE_WORDS for item in targets):
        # "`.send this hi`" and "send hi in this group" are the same address.
        targets = [str(ctx.chat_id)]
    text = " ".join(args[1:]).strip()
    return targets, text


async def _resolve_or_explain(ctx: CommandContext, raw: str) -> Any:
    """One name, resolved — and if it is not a chat, the nearest ones instead.

    ``Reach.resolve_or_raise`` owns the "no such chat" error; the suggestion list
    lives here because it is a *command* nicety, not something the send path
    should spend two lookups on for every message.
    """
    try:
        target = await ctx.runtime.reach.resolve_or_raise(raw)
    except reach_module.ReachError:
        target = None
    if target is not None:
        if str(raw).strip() == str(ctx.chat_id) and target.title.strip() in {"", str(target.id)}:
            # "this chat" read as a bare id in the receipt: "42 · sent" is a
            # database row, not an answer. `Target` is frozen, so it is copied,
            # not poked — the assignment was the FrozenInstanceError that turned
            # a working send into an "unexpected error" card.
            return replace(target, title="this chat")
        return target
    near = await ctx.runtime.reach.candidates(raw, limit=5)
    if near:
        names = ", ".join(f"{escape(item.label)} ({item.id})" for item in near)
        raise CommandError("reach.ambiguous", name=escape(raw), options=names)
    raise CommandError("reach.unknown_target", name=escape(raw))


def _report_line(report: Any) -> str:
    if not report.ok:
        return f"{escape(str(report.target.label if report.target else '—'))} · {escape(report.error)}"
    label = escape(report.target.label if report.target else "—")
    detail = escape(report.summary)
    extra = ""
    if report.deletes_at:
        extra = f" · {escape(report.deletes_at)}"
    return f"{label} · {detail}{extra}"


# ---------------------------------------------------------------------------
# .send
# ---------------------------------------------------------------------------
async def cmd_send(ctx: CommandContext) -> None:
    if (ctx.sub or "").lower() in {"list", "ls"}:
        await cmd_outbox(ctx)
        return
    targets, text = _split_targets_and_text(ctx)
    if not text.strip():
        raise CommandError("reach.usage_send", prefix=ctx.runtime.prefix)
    repeat = max(1, min(_flag_int(ctx, "repeat", 1), reach_module.MAX_REPEAT))
    every = _flag_seconds(ctx, "every") or 0.0
    delete_after = _flag_seconds(ctx, "delete_after") or _flag_seconds(ctx, "ttl")
    at = (ctx.flag("at") or "").strip() or None
    dry = bool(ctx.has_flag("dry"))
    compose = bool(ctx.has_flag("compose"))
    if len(targets) == 1:
        resolved = [await _resolve_or_explain(ctx, targets[0])]
    else:
        resolved, missing = await ctx.runtime.reach.resolve_many(targets)
        if missing:
            raise CommandError("reach.unknown_target", name=escape(", ".join(missing)))
    lines: list[str] = []
    for target in resolved:
        # `--compose`: he gave her a job, not a script ("wish him a happy
        # birthday"), and a repeat is a *different* sentence each time — nobody
        # wants the same wish ten times in a row.
        bodies = [text]
        if compose:
            written = await _compose(ctx, text, target, count=repeat if repeat > 1 else 1)
            if not written:
                lines.append(f"{escape(target.label)} · {ctx.t('reach.compose_failed')}")
                continue
            bodies = written
        try:
            report = await ctx.runtime.reach.deliver(
                target,
                bodies[0],
                spoiler_hidden=bool(ctx.has_flag("spoiler")),
                silent=bool(ctx.has_flag("silent")),
                repeat=1 if compose else repeat,
                every=every,
                delete_after=delete_after,
                at=at,
                dry_run=True if dry else None,
            )
        except reach_module.ReachError as exc:
            lines.append(f"{escape(target.label)} · {escape(str(exc))}")
            continue
        if compose and len(bodies) > 1 and report.ok and not dry:
            queued = await _queue_lines(ctx, target, bodies[1:], every=every, start=at)
            if queued:
                report.note = (report.note + " " if report.note else "") + ctx.t(
                    "reach.queued_more", count=queued
                )
        lines.append(_report_line(report))
    await ctx.reply("\n".join([bold(ctx.t("reach.sent_header", count=len(lines))), *lines]))


async def _compose(ctx: CommandContext, instruction: str, target: Any, *, count: int = 1) -> list[str]:
    """Write the message(s) he asked for, in her voice, one per line.

    The model is asked for the *body only* — no quotes, no preamble — and the
    result is cleaned here rather than trusted: a model that answers with
    'Sure! Here are three messages:' must not get that sent to a person.
    """
    provider = getattr(ctx.runtime, "ai", None)
    if provider is None or not getattr(provider, "available", False):
        return []
    wanted = max(1, min(int(count), reach_module.MAX_REPEAT))
    who = getattr(target, "label", "") or str(getattr(target, "id", ""))
    ask = [
        {
            "role": "system",
            "content": (
                "You write short Telegram messages on behalf of a person's assistant. "
                "Answer with the message text only — no quotes, no numbering, no "
                "explanation. Plain words, no emoji unless asked. "
                + (
                    f"Write {wanted} different messages, one per line."
                    if wanted > 1
                    else "Write one message."
                )
            ),
        },
        {
            "role": "user",
            "content": f"Recipient: {who}. What it must do: {instruction}",
        },
    ]
    try:
        reply = await provider.converse(ask, max_tokens=120 * wanted + 80)
    except Exception as exc:  # noqa: BLE001 - a failed write must not lose the command
        ctx.runtime.logger.warning("reach: compose failed (%s)", redact_text(str(exc)))
        return []
    return _clean_written(str(reply or ""), wanted)


def _clean_written(reply: str, wanted: int) -> list[str]:
    """The message bodies out of a model answer, stripped of model habits."""
    rows: list[str] = []
    for raw in reply.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^(?:[-*•>]|\d+[.)])\s*", "", line).strip()
        line = line.strip('"\'“”«»')
        if not line or line.endswith(":") and len(line) < 60:
            continue
        rows.append(line)
        if len(rows) >= max(1, wanted):
            break
    return rows


async def _queue_lines(
    ctx: CommandContext, target: Any, bodies: list[str], *, every: float, start: str | None
) -> int:
    """Queue the rest of a composed sequence, spaced out, one job per line."""
    from datetime import timedelta

    from ..utils.time import parse_when, utc_now

    base = utc_now()
    if start:
        try:
            base = parse_when(start, ctx.runtime.config.timezone)
        except Exception:  # noqa: BLE001 - a bad --at is caught by deliver()
            base = utc_now()
    gap = max(float(every or 0.0), 0.0)
    queued = 0
    for index, body in enumerate(bodies, start=1):
        when = base + timedelta(seconds=gap * index if gap else 2.5 * index)
        await ctx.runtime.scheduler.enqueue(
            "reach_send",
            when,
            {"chat_id": target.id, "text": body, "delete_after": None, "plan_id": None},
        )
        queued += 1
    return queued

# ---------------------------------------------------------------------------
# .broadcast
# ---------------------------------------------------------------------------
async def cmd_broadcast(ctx: CommandContext) -> None:
    targets, text = _split_targets_and_text(ctx)
    if not targets:
        raise CommandError("reach.usage_broadcast", prefix=ctx.runtime.prefix)
    limit = _flag_int(ctx, "limit", 0)
    dry = bool(ctx.has_flag("dry"))
    # A list of chats, resolved one by one, with the ones that do not exist
    # named rather than silently missing.
    found: list[Any] = []
    missing: list[str] = []
    for raw_target in targets:
        target = await ctx.runtime.reach.resolve(raw_target)
        if target is None:
            # A category word is a request for a list: "groups", "dms".
            listed = await _category(ctx, raw_target)
            if listed:
                found.extend(listed)
                continue
            missing.append(raw_target)
            continue
        found.append(target)
    if limit:
        found = found[:limit]
    if not found:
        raise CommandError("reach.unknown_target", name=escape(", ".join(missing) or "those chats"))
    if len(found) > BROADCAST_CONFIRM_OVER and not ctx.has_flag("yes"):
        raise CommandError(
            "reach.broadcast_confirm", count=len(found), prefix=ctx.runtime.prefix
        )
    if len(found) > 2 and not dry:
        # More than two chats is a queue, not a loop: holding his reply for a
        # minute of pacing is how a broadcast looks broken. Each send is its own
        # job, so a restart in the middle does not lose the rest.
        from datetime import timedelta

        from ..utils.time import utc_now

        base = utc_now()
        for index, target in enumerate(found):
            await ctx.runtime.scheduler.enqueue(
                "reach_send",
                base + timedelta(seconds=1.2 * index),
                {"chat_id": target.id, "text": text, "plan_id": None},
            )
        header = ctx.t("reach.broadcast_queued", total=len(found))
        await ctx.reply(
            bold(header) + ("\n" + escape(", ".join(missing)) if missing else "")
        )
        return
    sent = 0
    failed = 0
    for target in found:
        try:
            report = await ctx.runtime.reach.deliver(target, text, dry_run=True if dry else None)
        except reach_module.ReachError:
            failed += 1
            continue
        if report.ok and not report.dry_run:
            sent += 1
        elif not report.ok:
            failed += 1
        await ctx.runtime.reach.pause(0.7)
    header = ctx.t(
        "reach.broadcast_done",
        total=len(found),
        sent=0 if dry else sent,
        failed=failed,
        dry=ctx.t("common.on") if dry else ctx.t("common.off"),
    )
    await ctx.reply(bold(header) + ("\n" + escape(", ".join(missing)) if missing else ""))


async def _category(ctx: CommandContext, word: str) -> list[Any]:
    """``groups`` / ``dms`` / ``channels`` → the chats of that kind."""
    wanted = str(word or "").lower().strip().rstrip(":")
    if wanted not in {"groups", "group", "dms", "dm", "privates", "channels", "channel", "chats"}:
        return []
    client = ctx.runtime.client
    dialogs = getattr(client, "get_dialogs", None)
    if dialogs is None:
        return []
    try:
        rows = await dialogs(limit=400)
    except Exception as exc:  # noqa: BLE001 - no dialogs is not a crash
        ctx.runtime.logger.warning("broadcast: could not read dialogs: %s", exc)
        return []
    out: list[Any] = []
    for dialog in rows or []:
        entity = getattr(dialog, "entity", None) or dialog
        target = ctx.runtime.reach._target_from(entity)
        if wanted.startswith("group") and target.kind != "group":
            continue
        if wanted.startswith(("dm", "private")) and target.kind != "user":
            continue
        if wanted.startswith("channel") and target.kind != "channel":
            continue
        if not getattr(dialog, "is_user", None) and wanted.startswith(("dm", "private")):
            continue
        out.append(target)
    return out


# ---------------------------------------------------------------------------
# .outbox / .unsend / .emoji
# ---------------------------------------------------------------------------
async def cmd_outbox(ctx: CommandContext) -> None:
    try:
        wanted = int(ctx.arg(0) or 10)
    except ValueError:
        wanted = 10
    rows = await ctx.runtime.store.outbox(limit=max(1, min(wanted, 30)))
    total = await ctx.runtime.store.count_outbox()
    if not rows:
        await ctx.reply(ctx.t("reach.outbox_empty"))
        return
    lines = [bold(ctx.t("reach.outbox_header", count=len(rows)))]
    if total > len(rows):
        lines.append(ctx.t("reach.outbox_total", total=total))
    for row in rows:
        mark = {"sent": "●", "deleted": "○", "failed": "✕", "dry": "◌"}.get(str(row["status"]), "·")
        when = str(row["sent_at"] or "")[11:16]
        body = truncate(" ".join(strip_html(str(row["text"] or "")).split()), 40)
        lines.append(
            f"{mark} #{row['id']} {code(str(row['chat_id']))} {escape(when)} · {escape(body)}"
        )
    await ctx.reply("\n".join(lines))


async def cmd_unsend(ctx: CommandContext) -> None:
    raw = (ctx.args[0] if ctx.args else "").strip()
    if not raw:
        raise CommandError("reach.usage_unsend", prefix=ctx.runtime.prefix)
    if raw.lower() in {"last", "latest", "آخرین", "اخرین"} or ctx.has_flag("last"):
        # "delete your message" — hers, in the chat it was sent in, with no id
        # to give. A plan or an order typed from memory never has one.
        # A plan's own send first: "… DM محمد و بعد ده دقیقه پیامتو پاک کن" means
        # *that* message, not whatever this account last said anywhere.
        plan_id = getattr(ctx.runtime, "current_plan_id", None)
        rows = (
            await ctx.runtime.store.outbox_for_plan(int(plan_id))
            if plan_id
            else await ctx.runtime.store.outbox(
                20, chat_id=int(ctx.chat_id) if not ctx.has_flag("any") else None
            )
        )
        row = next(
            (
                item
                for item in rows
                if str(item["kind"] or "") in {"text", "media", "file"}
                and str(item["status"] or "") != "deleted"
            ),
            None,
        )
        if row is None:
            raise CommandError("reach.nothing_to_unsend")
        ok = await ctx.runtime.reach.delete(int(row["chat_id"]), int(row["message_id"] or 0))
        if ok:
            await ctx.runtime.store.mark_deleted(int(row["id"]), note="unsent (last)")
        await ctx.reply(
            ctx.t(
                "reach.unsent_last" if ok else "reach.unsend_failed",
                id=row["id"],
            )
        )
        return
    row = await ctx.runtime.store.outbox_row(int(raw)) if raw.isdigit() else None
    if row is None:
        raise CommandError("reach.unknown_outbox", id=escape(raw))
    ok = await ctx.runtime.reach.delete(int(row["chat_id"]), int(row["message_id"] or 0))
    if ok:
        await ctx.runtime.store.mark_deleted(int(row["id"]), note="unsent")
    await ctx.reply(ctx.t("reach.unsent" if ok else "reach.unsend_failed", id=row["id"]))


async def cmd_emoji(ctx: CommandContext) -> None:
    emoji = (ctx.args[0] if ctx.args else "").strip()
    if not emoji:
        raise CommandError("reach.usage_emoji", prefix=ctx.runtime.prefix)
    message = getattr(ctx, "message", None)
    target_id = getattr(message, "reply_to_msg_id", None)
    # ``--on <id>``: she can point at a message without being a Telegram reply,
    # which is what a spoken order ("به پیامش قلب واکنش بده") actually is.
    explicit = (ctx.flag("on") or ctx.flag("message") or "").strip()
    if explicit:
        try:
            target_id = int(explicit)
        except ValueError:
            raise CommandError("reach.usage_emoji", prefix=ctx.runtime.prefix) from None
    if not target_id:
        raise CommandError("reach.usage_emoji", prefix=ctx.runtime.prefix)
    ok = await ctx.runtime.reach.react(ctx.chat_id, int(target_id), emoji)
    await ctx.reply(ctx.t("reach.reacted" if ok else "reach.react_failed", emoji=escape(emoji)))


# ---------------------------------------------------------------------------
# .plan
# ---------------------------------------------------------------------------
async def cmd_plan(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls", "show"}:
        plans = await ctx.runtime.plans.show(limit=10)
        if not plans:
            await ctx.reply(ctx.t("plan.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("plan.header", count=len(plans)))]
        for plan in plans:
            lines.append(
                f"⟡ #{plan['id']} {escape(plan['label'])} · {len(plan['steps'])} "
                f"{ctx.t('plan.steps_word')}"
            )
            for step in plan["steps"][:6]:
                lines.append(f"   {escape(str(step['at'])[11:16])} {code(escape(step['action']))}")
        await ctx.reply("\n".join(lines))
        return
    if action in {"stop", "cancel", "off", "kill"}:
        needle = " ".join(ctx.args).strip() if ctx.args else ""
        result = await ctx.runtime.plans.cancel(needle or None)
        if not result.get("ok"):
            raise CommandError("plan.nothing_to_stop")
        await ctx.reply(
            ctx.t("plan.stopped", id=result["id"], label=escape(str(result["label"])))
        )
        return
    if action in {"add", "make", "do", "run"}:
        # `.plan do <sentence>`: the same reader the sentence floor uses.
        sentence = ctx.raw_args or " ".join(ctx.args)
        sentence = sentence.split(" ", 1)[1] if sentence.lower().startswith(("do ", "make ")) else sentence
        from ..services import intent as intent_module
        from ..services.plans import build_plan

        plan = build_plan(sentence, intent=intent_module)
        if plan.empty:
            raise CommandError("plan.unreadable")
        result = await ctx.runtime.plans.create(
            plan,
            said=sentence,
            owner_id=getattr(ctx.event, "sender_id", None),
            # The room he is standing in, so a step that speaks "here" speaks here.
            chat_id=int(ctx.chat_id or 0) or None,
        )
        if ctx.has_flag("json") and result.get("ok"):
            await ctx.reply(code(json.dumps(result["plan"], ensure_ascii=False)))
            return
        await ctx.reply(
            ctx.t(
                "plan.created",
                id=result["id"],
                steps=result["steps"],
                first=escape(str(result["first_at"])[11:16]),
                last=escape(str(result["last_at"])[11:16]),
                label=escape(result["label"]),
            )
        )
        return
    raise CommandError("plan.usage", prefix=ctx.runtime.prefix)


# ---------------------------------------------------------------------------
# .ignore / .unignore / .ignored
# ---------------------------------------------------------------------------
async def cmd_ignore(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls", "show"}:
        rows = await ctx.runtime.store.ignored_people()
        if not rows:
            await ctx.reply(ctx.t("ignore.empty"))
            return
        lines = [bold(ctx.t("ignore.header", count=len(rows)))]
        for row in rows:
            lines.append(f"⨯ {escape(str(row['name'] or '—'))} {code(str(row['user_id']))}")
        await ctx.reply("\n".join(lines))
        return
    if action in {"off", "remove", "unignore", "forgive", "restore"}:
        raw = " ".join(ctx.args).strip()
        found = await _ignorable(ctx, raw)
        if found is None:
            raise CommandError("ignore.unknown", name=escape(raw))
        await ctx.runtime.store.unignore_person(found[0])
        await ctx.reply(ctx.t("ignore.removed", name=escape(found[1])))
        return
    if action in {"on", "add", "mute", "block"}:
        raw = " ".join(ctx.args).strip()
        found = await _ignorable(ctx, raw)
        if found is None:
            raise CommandError("ignore.unknown", name=escape(raw))
        await ctx.runtime.store.ignore_person(found[0], found[1])
        await ctx.reply(ctx.t("ignore.added", name=escape(found[1]), id=found[0]))
        return
    raise CommandError("ignore.usage", prefix=ctx.runtime.prefix)


# ---------------------------------------------------------------------------
# .rules — who she answers, and how
# ---------------------------------------------------------------------------
_RULE_MODES = {
    "ignore": "ignore", "off": "ignore", "mute": "ignore", "never": "ignore", "silent": "ignore",
    "react": "react", "heart": "react", "emoji": "react", "reaction": "react",
    "sticker": "sticker", "stickers": "sticker",
    "answer": "answer", "on": "answer", "allow": "answer", "normal": "answer", "back": "answer",
}
_MODE_GLYPH = {"ignore": "⨯", "react": "❤", "sticker": "☺", "answer": "✓"}


async def cmd_rules(ctx: CommandContext) -> None:
    """One instruction per person: answer, ignore, or react.

    This is the reins. Before it, "never answer him" was the only way to shape
    who she talks to, it applied to every room he shared with her, and it was
    invisible — nothing listed it and nothing undid it but memory.
    """
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls", "show", ""}:
        await _rules_list(ctx)
        return
    if action in {"clear", "remove", "delete", "forget", "reset"}:
        found = await _rule_person(ctx, " ".join(ctx.args))
        if found is None:
            raise CommandError("policy.unknown", name=escape(" ".join(ctx.args)))
        scope = _scope_of(ctx)
        removed = await ctx.runtime.store.clear_person_rule(found[0], scope)
        if not removed and not scope:
            # A rule written for one room is still a rule about this person:
            # "undo the rule about Sara" must find it without him remembering
            # that he said it while standing in that room.
            removed = await ctx.runtime.store.clear_person_rule(found[0], None)
        if not removed:
            raise CommandError("policy.none", name=escape(found[1]))
        await ctx.reply(
            ui.card(
                ctx.t("policy.cleared", name=escape(found[1])),
                (ctx.t("policy.cleared_note"),),
                icon="⌫",
            )
        )
        return
    mode = _RULE_MODES.get(action)
    if mode is None:
        raise CommandError("policy.usage", prefix=ctx.runtime.prefix)
    raw = " ".join(ctx.args)
    emoji = ""
    if mode in {"react", "sticker"}:
        # ".policy react sara heart" — the glyph is the last word, not part of
        # the name. Only the tail is peeled, so a person actually called "Heart"
        # is still reachable as the whole argument.
        from ..services.intent import reaction_glyph

        words = raw.split()
        while words:
            glyph = reaction_glyph(words[-1])
            if not glyph:
                break
            emoji = emoji or glyph
            words.pop()
        raw = " ".join(words) or raw
        emoji = emoji or str(ctx.flag("emoji") or "").strip() or "❤️"
    found = await _rule_person(ctx, raw)
    if found is None:
        raise CommandError("policy.unknown", name=escape(" ".join(ctx.args)))
    owner = int(getattr(ctx.runtime.permissions, "self_id", 0) or 0)
    if found[0] == owner:
        raise CommandError("policy.self")
    scope = _scope_of(ctx)
    seconds = _window_of(ctx)
    await ctx.runtime.store.set_person_rule(
        found[0], mode, chat_id=scope, emoji=emoji, name=found[1], seconds=seconds
    )
    where = ctx.t("policy.everywhere") if not scope else ctx.t("policy.this_chat")
    if seconds:
        where = f"{where} · {ctx.t('policy.for_a_while', when=escape(_window_words(seconds)))}"
    if mode == "ignore":
        title = ctx.t("policy.ignored", name=escape(found[1]))
    elif mode == "react":
        title = ctx.t("policy.reacted", name=escape(found[1]), emoji=escape(emoji))
    elif mode == "sticker":
        title = ctx.t("policy.stickered", name=escape(found[1]), emoji=escape(emoji))
    else:
        title = ctx.t("policy.answered", name=escape(found[1]))
    await ctx.reply(
        ui.card(
            title,
            (where, ctx.t("policy.id_note", id=found[0], prefix=ctx.runtime.prefix)),
            icon=_MODE_GLYPH[mode],
            footer=ctx.t("policy.undo", prefix=ctx.runtime.prefix),
        )
    )


def _window_words(seconds: int) -> str:
    """``7200`` → "2h": the same units the command was typed in."""
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _window_of(ctx: CommandContext) -> int | None:
    """``--for 2h``: a rule that ends on the clock instead of on his memory."""
    raw = (ctx.flag("for") or "").strip()
    if not raw:
        return None
    seconds = reach_module.parse_duration(raw)
    if not seconds:
        raise CommandError("errors.not_a_number", field="for")
    return max(30, int(seconds))


def _scope_of(ctx: CommandContext) -> int:
    """``--here`` ties the rule to this room; otherwise it follows him anywhere."""
    if ctx.has_flag("here") or ctx.has_flag("this_chat"):
        return int(ctx.chat_id)
    return 0


async def _rule_person(ctx: CommandContext, raw: str) -> tuple[int, str] | None:
    """``(user_id, name)`` from a name, a handle, an id — or the message he replied to."""
    text = str(raw or "").strip()
    # "don't answer him": the person is the sender of the message being answered.
    if not text or text.lower().strip(" .,!") in _HIM_WORDS or ctx.has_flag("reply"):
        replied = await ctx.reply_message()
        if replied is not None:
            sender = getattr(replied, "sender_id", None) or getattr(replied, "from_id", None)
            if sender:
                person = await ctx.runtime.people.get(int(sender))
                name = str(getattr(person, "label", "") or sender) if person else str(sender)
                return int(sender), name
        if not text:
            return None
    return await _ignorable(ctx, text)


async def _rules_list(ctx: CommandContext) -> None:
    rows = await ctx.runtime.store.person_rules()
    if not rows:
        await ctx.reply(
            ui.card(
                ctx.t("policy.empty"),
                (ctx.t("policy.empty_note", prefix=ctx.runtime.prefix),),
                icon="⌘",
            )
        )
        return
    lines: list[str] = []
    for row in rows:
        mode = str(row["mode"] or "answer")
        glyph = _MODE_GLYPH.get(mode, "•")
        who = str(row["name"] or row["user_id"])
        scope = ctx.t("policy.everywhere") if not int(row["chat_id"] or 0) else f"#{row['chat_id']}"
        emoji = f" {row['emoji']}" if str(row["emoji"] or "") else ""
        expires = row["expires_at"] if "expires_at" in row.keys() else None
        if expires:
            scope = f"{scope} · {ctx.t('policy.until', when=escape(str(expires)[:16].replace('T', ' ')))}"
        lines.append(f"{glyph} {who}{emoji} · {scope}")
    await ctx.reply(
        ui.card(
            ctx.t("policy.header", count=len(rows)),
            lines,
            icon="⌘",
            footer=ctx.t("policy.footer", prefix=ctx.runtime.prefix),
        )
    )


async def _ignorable(ctx: CommandContext, raw: str) -> tuple[int, str] | None:
    """``(user_id, name)`` for a person he named, or ``None``.

    A :class:`Person` row carries ``user_id``; a resolved :class:`Target` carries
    ``id``; a bare number has neither. All three must reach the ignore table, so
    this is the one place that knows the difference.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        # A number is a person, whether or not she has a row for them yet: "don't
        # talk to 483920" must work on the first day, before they ever message.
        person = await ctx.runtime.people.get(int(text))
        if person is not None:
            return int(person.user_id), str(person.label or person.user_id)
        found = await ctx.runtime.reach.resolve(text)
        if found is not None:
            return int(found.id), str(found.label or found.id)
        return int(text), text
    matches = await ctx.runtime.people.search(text, limit=1)
    if matches:
        person = matches[0]
        return int(person.user_id), str(person.label or person.user_id)
    found = await ctx.runtime.reach.resolve(text)
    if found is not None:
        return int(found.id), str(found.label or found.id)
    return None

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    async def _plan_step(row: Any) -> None:
        """The scheduler's door into a plan step.

        A closure over this runtime, not a module global: a test may build two
        runtimes in one process, and the second one must not inherit the first
        one's scheduler.
        """
        import json

        payload = row["payload"]
        data = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
        await runtime.plans.run_step(data)

    runtime.scheduler.register_job("reach_send", runtime.reach.send_later)
    runtime.scheduler.register_job("reach_unsend", runtime.reach.unsend)
    runtime.scheduler.register_job("plan_step", _plan_step)

    runtime.registry.register(
        Command(
            "send",
            cmd_send,
            category="reach",
            aliases=("say", "msg", "message"),
            usage="<to> <text> [--here] [--repeat N] [--every 30s] [--spoiler] [--silent] "
                  "[--delete-after 10s] [--at 21:00] [--compose] [--dry]",
            description=(
                "Send a message — repeated, hidden behind a spoiler, self-destructing, "
                "later, or written by her."
            ),
            owner_only=True,
            changes_state=True,
            examples=(
                ".send @sara running late",
                ".send @sara,amir the meeting moved",
                ".send @sara boo --spoiler --delete-after 10s",
                ".send @sara still here? --repeat 3 --every 1m",
                ".send --here hi --repeat 10",
                ".send @sara --compose wish her luck with the exam",
            ),
            subcommands=("list",),
        )
    )
    runtime.registry.register(
        Command(
            "policy",
            cmd_rules,
            category="reach",
            aliases=("rules-say", "replies", "whom"),
            usage=(
                "[list|ignore ‹who›|react ‹who› [emoji]|sticker ‹who› [emoji]|answer ‹who›"
                "|clear ‹who›] [--here] [--for 2h]"
            ),
            description="Who she answers, and how: silence, answer, or a reaction instead.",
            owner_only=True,
            changes_state=True,
            subcommands=("list", "ignore", "react", "sticker", "answer", "clear", "remove"),
        )
    )
    runtime.registry.register(
        Command(
            "broadcast",
            cmd_broadcast,
            category="reach",
            usage="<to…|groups|dms> <text> [--limit N] [--dry] [--yes]",
            description="Send the same message to a list of chats, paced.",
            owner_only=True,
            changes_state=True,
            needs_confirmation=False,
            examples=(
                ".broadcast groups good morning --limit 20 --dry",
                ".broadcast @a,@b,@c dinner is ready",
            ),
        )
    )
    runtime.registry.register(
        Command(
            "outbox",
            cmd_outbox,
            category="reach",
            aliases=("sent",),
            usage="[limit]",
            description="The last messages this account sent, from the database.",
            owner_only=True,
        )
    )
    runtime.registry.register(
        Command(
            "unsend",
            cmd_unsend,
            category="reach",
            usage="<outbox id> or last",
            description=(
                "Delete a message by its outbox number, or ``last`` for the one she "
                "sent here most recently (a plan's own send when a plan is running)."
            ),
            owner_only=True,
            changes_state=True,
        )
    )
    runtime.registry.register(
        Command(
            "emoji",
            cmd_emoji,
            category="reach",
            aliases=("reaction", "reactto"),
            usage="<emoji> [--on <message id>]  (or reply to the message)",
            description="React to the message you replied to.",
            owner_only=True,
            changes_state=True,
        )
    )
    runtime.registry.register(
        Command(
            "plan",
            cmd_plan,
            category="reach",
            aliases=("timeline",),
            usage="[list|stop [word]|add <sentence>]",
            description="Timelines: one sentence, many moments.",
            owner_only=True,
            changes_state=True,
            subcommands=("list", "show", "stop", "cancel", "add", "do", "make"),
        )
    )
    runtime.registry.register(
        Command(
            "ignore",
            cmd_ignore,
            category="reach",
            aliases=("mute-person",),
            usage="[list|on <who>|off <who>]",
            description="Never answer this person again.",
            owner_only=True,
            changes_state=True,
            subcommands=("list", "on", "add", "off", "remove", "unignore"),
        )
    )


__all__ = [
    "cmd_broadcast",
    "cmd_rules",
    "cmd_emoji",
    "cmd_ignore",
    "cmd_outbox",
    "cmd_plan",
    "cmd_send",
    "cmd_unsend",
    "register",
]
