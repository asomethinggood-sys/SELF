"""Verbs on a message: reply, forward, copy, edit, delete, pin.

Everything here acts on **the message he replied to**, because that is the only
message a chat command can be unambiguous about. "delete that" with no reply is a
guess, and a guess that deletes the wrong line is unforgivable — so it is refused
with the one sentence that says how to aim it.

The rules that matter:

* **Ours only, where Telegram requires it.** An edit can only touch a message this
  account sent; a delete can remove anything in a chat it can moderate; a pin is
  allowed wherever he has the right. Each is reported in its own words, so a
  refusal from Telegram is never dressed up as success.
* **Destructive is confirmed.** ``.delete`` and ``.unpin`` ask first (the registry
  owns that flow), and ``.delete`` records the row in the outbox as ``deleted`` so
  the history matches the chat.
* **A copy is not a forward.** ``.copy`` sends the words again, with no header
  pointing at the person they came from; ``.forward`` keeps the header, which is
  the whole reason to forward instead of copy.

Feature areas: 21 (messaging), 45 (media), 96 (feature switches).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape
from .helpers import display_name, media_label, reply_text

__all__ = ["register"]

async def _aim(ctx: CommandContext) -> Any:
    """The message this command is aimed at, or a refusal that says how to aim."""
    reply = await ctx.reply_message()
    if reply is None:
        raise CommandError("msg.need_reply", prefix=ctx.runtime.prefix, name=ctx.command.name)
    return reply


def _body_text(ctx: CommandContext) -> str:
    return (ctx.rest_raw or " ".join(ctx.args)).strip()


def _is_ours(ctx: CommandContext, message: Any) -> bool:
    if getattr(message, "out", False):
        return True
    sender = getattr(message, "sender_id", None)
    self_id = ctx.runtime.permissions.self_id
    return bool(sender is not None and self_id is not None and int(sender) == int(self_id))


# ---------------------------------------------------------------------------
# .reply
# ---------------------------------------------------------------------------
async def cmd_reply(ctx: CommandContext) -> None:
    """Answer the message he pointed at, in the chat he is standing in."""
    reply = await _aim(ctx)
    text = _body_text(ctx)
    if not text:
        # No words: quote the message back as a card, which is a legitimate way
        # to "reply" to a photo or a sticker without inventing a caption.
        label = media_label(reply) if getattr(reply, "media", None) is not None else "text"
        await ctx.reply(
            bold(ctx.t("msg.quoting", name=escape(display_name(getattr(reply, "sender", None), "—"))))
            + "\n"
            + ctx.t("msg.media_line", kind=escape(label), id=code(getattr(reply, "id", "?")))
        )
        return
    result = await ctx.sender.send(
        ctx.chat_id, escape(text), reply_to=getattr(reply, "id", None)
    )
    if not getattr(result, "ok", False):
        raise CommandError("msg.failed", detail=escape(str(getattr(result, "error", "") or "")))
    await ctx.reply(ctx.t("msg.replied", id=code(getattr(reply, "id", "?"))))


# ---------------------------------------------------------------------------
# .forward / .copy
# ---------------------------------------------------------------------------
async def cmd_forward(ctx: CommandContext) -> None:
    reply = await _aim(ctx)
    who = " ".join(ctx.args).strip()
    if not who:
        raise CommandError("msg.usage_forward", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(who)
    report = await ctx.runtime.reach.forward(
        target, int(getattr(reply, "id", 0) or 0), ctx.chat_id
    )
    if not report.ok:
        raise CommandError("msg.failed", detail=escape(report.error or "the forward failed"))
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("msg.forwarded")),
                ctx.t(
                    "msg.forwarded_line",
                    name=escape(target.label),
                    id=code(target.id),
                ),
            ]
        )
    )


async def cmd_copy(ctx: CommandContext) -> None:
    """The words, without the forward header: his words now, not theirs."""
    reply = await _aim(ctx)
    who = " ".join(ctx.args).strip()
    if not who:
        raise CommandError("msg.usage_copy", prefix=ctx.runtime.prefix)
    text = reply_text(reply)
    if not text.strip():
        raise CommandError("msg.copy_nothing")
    target = await ctx.runtime.reach.resolve_or_raise(who)
    report = await ctx.runtime.reach.deliver(target, text)
    if not report.ok:
        raise CommandError("msg.failed", detail=escape(report.error or "the copy failed"))
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("msg.copied")),
                ctx.t("msg.copied_line", name=escape(target.label), id=code(target.id)),
            ]
        )
    )


# ---------------------------------------------------------------------------
# .edit
# ---------------------------------------------------------------------------
async def cmd_edit(ctx: CommandContext) -> None:
    reply = await _aim(ctx)
    text = _body_text(ctx)
    if not text:
        raise CommandError("msg.usage_edit", prefix=ctx.runtime.prefix)
    if not _is_ours(ctx, reply):
        raise CommandError("msg.not_ours")
    ok = await ctx.runtime.reach.edit(ctx.chat_id, int(getattr(reply, "id", 0) or 0), escape(text))
    if not ok:
        raise CommandError("msg.edit_failed")
    await ctx.reply(ctx.t("msg.edited", id=code(getattr(reply, "id", "?"))))


# ---------------------------------------------------------------------------
# .delete / .pin / .unpin
# ---------------------------------------------------------------------------
async def cmd_delete(ctx: CommandContext) -> None:
    reply = await _aim(ctx)
    message_id = int(getattr(reply, "id", 0) or 0)
    if not message_id:
        raise CommandError("msg.failed", detail="telegram did not give the message an id")
    ok = await ctx.runtime.reach.delete(ctx.chat_id, message_id)
    if not ok:
        raise CommandError("msg.delete_failed")
    # The outbox is the record of what this account did; a delete belongs there
    # too, so `.outbox` and the chat agree.
    row = await ctx.store.outbox_for_message(ctx.chat_id, message_id)
    if row is not None:
        await ctx.store.mark_deleted(int(row["id"]), note="deleted by hand")
    else:
        await ctx.store.add_outbox(
            ctx.chat_id, message_id, "delete", "", "deleted", note=f"deleted by hand in {ctx.chat_id}"
        )
    await ctx.reply(ctx.t("msg.deleted", id=code(message_id)))


async def cmd_pin(ctx: CommandContext) -> None:
    reply = await _aim(ctx)
    quiet = ctx.has_flag("silent")
    ok = await ctx.runtime.reach.pin(ctx.chat_id, int(getattr(reply, "id", 0) or 0), notify=not quiet)
    if not ok:
        raise CommandError("msg.pin_failed")
    await ctx.reply(ctx.t("msg.pinned", id=code(getattr(reply, "id", "?"))))


async def cmd_unpin(ctx: CommandContext) -> None:
    reply = await ctx.reply_message()
    message_id = int(getattr(reply, "id", 0) or 0) if reply is not None else None
    ok = await ctx.runtime.reach.unpin(ctx.chat_id, message_id or None)
    if not ok:
        raise CommandError("msg.unpin_failed")
    await ctx.reply(ctx.t("msg.unpinned"))


# ---------------------------------------------------------------------------
# .clearhistory — the whole conversation, not one line
# ---------------------------------------------------------------------------
async def cmd_clearhistory(ctx: CommandContext) -> None:
    """Drop a chat's history.

    ``--for-everyone`` is the Telegram ``revoke`` flag: without it the lines go
    from this account only. Both are confirmed by the registry before they run,
    because neither can be undone by ``.undo``.
    """
    where = " ".join(ctx.args).strip()
    target = await ctx.runtime.reach.resolve_or_raise(where) if where else None
    chat_id = getattr(target, "id", None) or ctx.chat_id
    label = escape(getattr(target, "label", "") or str(chat_id))
    # The parser lowercases and turns dashes into underscores, so the flag he
    # types as ``--for-everyone`` arrives as ``for_everyone``.
    revoke = ctx.has_flag("for_everyone") or ctx.has_flag("everyone")
    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("errors.not_connected")
    from ..services.tg_extra import ExtrasError

    try:
        await extras.clear_history(chat_id, revoke=revoke)
    except ExtrasError as exc:
        raise CommandError("msg.clear_failed", name=label, detail=escape(str(exc)[:180])) from exc
    key = "msg.cleared_everyone" if revoke else "msg.cleared"
    await ctx.reply(ctx.t(key, name=label))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "reply",
                cmd_reply,
                category="messages",
                aliases=("ans",),
                usage="<text>  (reply to a message)",
                requires_reply=True,
                changes_state=True,
                description="Answer the message you replied to, in this chat.",
                examples=(".reply on my way",),
            ),
            Command(
                "forward",
                cmd_forward,
                category="messages",
                aliases=("fwd",),
                usage="<to>  (reply to a message)",
                requires_reply=True,
                changes_state=True,
                description="Forward the message you replied to, header and all.",
                examples=(".forward @sara",),
            ),
            Command(
                "copy",
                cmd_copy,
                category="messages",
                usage="<to>  (reply to a message)",
                requires_reply=True,
                changes_state=True,
                description="Send the same words somewhere else, with no forward header.",
                examples=(".copy saved",),
            ),
            Command(
                "edit",
                cmd_edit,
                category="messages",
                usage="<new text>  (reply to one of her messages)",
                requires_reply=True,
                changes_state=True,
                description="Change a message she sent.",
                examples=(".edit nine o'clock",),
            ),
            Command(
                "delete",
                cmd_delete,
                category="messages",
                aliases=("del", "rm"),
                usage="(reply to a message) [--yes]",
                requires_reply=True,
                changes_state=True,
                needs_confirmation=True,
                description="Delete the message you replied to.",
            ),
            Command(
                "pin",
                cmd_pin,
                category="messages",
                usage="(reply to a message) [--silent]",
                requires_reply=True,
                changes_state=True,
                description="Pin the message you replied to.",
            ),
            Command(
                "clearhistory",
                cmd_clearhistory,
                category="messages",
                aliases=("wipe", "purge"),
                usage="[chat] [--for-everyone]",
                changes_state=True,
                needs_confirmation=True,
                description="Delete a whole conversation's history, here or in a named chat.",
                examples=(".clearhistory", ".clearhistory @noise --for-everyone"),
            ),
            Command(
                "unpin",
                cmd_unpin,
                category="messages",
                usage="[--all]",
                changes_state=True,
                description="Unpin the message you replied to, or the chat's pin.",
            ),
        ]
    )
