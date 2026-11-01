"""Drawing: pictures she makes on his own GPU instead of asking an API.

``.draw a lighthouse in fog, pen and ink`` queues a job on the ComfyUI instance
the owner runs and sends the PNG back as a photo. Everything about the model —
checkpoint, sampler, resolution, whether it can draw hands at all — is his
choice, set once in the workflow file; this module only carries the words and
the file.

Why a command and not "she draws whenever she feels like it": a picture costs
seconds to minutes of someone's GPU, so it happens when asked. The ambient chat
path never calls it — a reply that silently occupies a graphics card is the kind
of surprise that makes an owner turn a bot off.

Feature areas: 51 (media), 44 (owner tools).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..services.imagegen import ImageError
from ..services.sender import SendError
from ..utils.formatting import escape
from ..utils.text import truncate

#: Finished pictures are kept for a while so a re-send costs nothing, but a
#: runner's disk is small: anything older than this is deleted on the next draw.
KEEP_SECONDS = 6 * 3600

#: Two draws in five seconds is a person testing a prompt, not a workload; but
#: the window is applied by hand rather than by the registry, because ``.draw
#: info`` used to burn the cooldown and leave the next real draw refused.
DRAW_COOLDOWN = 5.0


def _outdir(runtime: Any) -> Path:
    base = os.environ.get("HAVOC_DRAW_DIR") or ""
    if base:
        return Path(base)
    data_dir = getattr(getattr(runtime, "config", None), "data_dir", None)
    if data_dir:
        return Path(str(data_dir)) / "drawn"
    return Path("/tmp/havoc-drawn")


def _sweep(folder: Path) -> None:
    """Delete old pictures; a drawing a day for a month is a full disk."""
    cutoff = time.time() - KEEP_SECONDS
    try:
        for item in folder.glob("*.png"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                continue
    except OSError:
        return


async def cmd_draw(ctx: CommandContext) -> None:
    """``.draw <prompt>`` — one picture, made locally, sent back as a photo."""
    generator = getattr(ctx.runtime, "image_gen", None)
    action = (ctx.sub or "").lower()
    prompt = " ".join(ctx.args).strip()

    if action in {"info", "status"} or (not prompt and not action):
        await ctx.reply(_draw_info(ctx, generator))
        return
    if generator is None or not getattr(generator, "available", False):
        await ctx.reply(_draw_info(ctx, generator))
        return
    if not prompt:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())

    # Optional seeds: the same seed and the same words give the same picture,
    # which is the only way to say "that one again, but wider".
    seed: int | None = None
    raw_seed = ctx.flag("seed")
    if raw_seed:
        try:
            seed = int(raw_seed)
        except ValueError as exc:
            raise CommandError("errors.not_a_number", field="seed") from exc

    key = f"draw:{ctx.chat_id}"
    remaining = ctx.runtime.limiter.cooldown_remaining(key, DRAW_COOLDOWN)
    if remaining > 0:
        raise CommandError("errors.cooldown", seconds=f"{remaining:.0f}")

    folder = _outdir(ctx.runtime)
    _sweep(folder)
    ctx.runtime.limiter.touch(key)
    notice = await ctx.reply(
        "\n\n".join(
            [
                ui.title("✎", ctx.t("draw.title")),
                ui.note(ctx.t("draw.working", prompt=escape(truncate(prompt, 120)))),
            ]
        )
    )
    notice_message = getattr(notice, "message", None)

    try:
        path = await asyncio.wait_for(
            generator.generate(prompt, outdir=folder, seed=seed),
            timeout=float(getattr(generator, "timeout", 180.0)) + 30.0,
        )
    except TimeoutError:
        await _edit(ctx, notice_message, _draw_error(ctx, ctx.t("draw.too_slow")))
        return
    except ImageError as exc:
        await _edit(ctx, notice_message, _draw_error(ctx, escape(str(exc))))
        return

    caption = "\n\n".join(
        [
            ui.title("✎", ctx.t("draw.title")),
            ui.note(ctx.t("draw.caption", prompt=escape(truncate(prompt, 120)))),
        ]
    )
    try:
        await ctx.sender.send_media(
            ctx.chat_id,
            str(path),
            caption=caption,
            reply_to=getattr(ctx.message, "id", None),
        )
    except SendError as exc:
        await _edit(ctx, notice_message, _draw_error(ctx, escape(str(exc))))
        return

    # The placeholder did its job; leaving "drawing…" above the picture makes
    # the chat look stuck. A private chat gets the line removed quietly, a group
    # keeps a short "done" because a deleted message reads as a mistake there.
    if notice_message is not None:
        try:
            if ctx.is_private:
                client = getattr(ctx.runtime, "client", None)
                deleter = getattr(client, "delete_messages", None)
                if deleter is not None:
                    result = deleter(ctx.chat_id, [getattr(notice_message, "id", 0)])
                    if asyncio.iscoroutine(result):
                        await result
            else:
                await ctx.sender.edit(
                    ctx.chat_id, notice_message, ui.note(ctx.t("draw.done"))
                )
        except Exception:  # noqa: BLE001 - the picture already arrived
            pass


async def _edit(ctx: CommandContext, message: Any, text: str) -> None:
    if message is not None:
        try:
            await ctx.sender.edit(ctx.chat_id, message, text)
            return
        except SendError:
            pass
    await ctx.reply(text, reply=True)


def _draw_info(ctx: CommandContext, generator: Any) -> str:
    if generator is None or not getattr(generator, "available", False):
        return "\n\n".join(
            [
                ui.title("✎", ctx.t("draw.title")),
                ui.note(ctx.t("draw.not_configured")),
                ui.footer(ctx.t("draw.setup_hint")),
            ]
        )
    info = generator.describe() if hasattr(generator, "describe") else {}
    rows = [
        ctx.t("draw.row_server", url=escape(str(info.get("url", "")))),
        ctx.t("draw.row_workflow", name=escape(str(info.get("workflow", "built-in")))),
        ctx.t("draw.row_made", count=int(info.get("made", 0))),
    ]
    if info.get("checkpoint"):
        rows.append(ctx.t("draw.row_checkpoint", name=escape(str(info["checkpoint"]))))
    if info.get("last_error"):
        rows.append(ctx.t("draw.row_last_error", reason=escape(truncate(str(info["last_error"]), 160))))
    return "\n\n".join(
        [
            ui.title("✎", ctx.t("draw.title")),
            ui.panel(rows),
            ui.footer(ctx.t("draw.usage_hint", prefix=ctx.runtime.prefix)),
        ]
    )


def _draw_error(ctx: CommandContext, reason: str) -> str:
    return "\n\n".join(
        [ui.title("✎", ctx.t("draw.title")), ui.note(reason), ui.footer(ctx.t("draw.retry_hint"))]
    )


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "draw",
                cmd_draw,
                category="media",
                aliases=("paint", "imagine"),
                usage="<prompt> [--seed n] | info",
                subcommands=("info", "status"),
                changes_state=True,
            )
        ]
    )
