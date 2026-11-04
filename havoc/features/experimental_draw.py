"""A temporary, owner-only draw command against the opt-in SDXL experiment.

``.fastdraw`` is deliberately separate from ``.draw``: the normal generator keeps
its checkpoint, resolution and scheduler, and this command refuses to run when
the experiment is not published. If the experiment is offline, expired or
starting, the reply says so and points back at ``.draw``.

Feature areas: 51 (media).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..services.experimental_sdxl import ExperimentalError, generate
from ..services.sender import SendError
from ..utils.formatting import escape
from ..utils.text import truncate

KEEP_SECONDS = 6 * 3600
#: The experiment holds at most two jobs, and the render can take a few minutes.
GENERATE_TIMEOUT = 330.0


def _outdir(runtime: Any) -> Path:
    base = os.environ.get("HAVOC_EXPERIMENTAL_DRAW_DIR") or ""
    if base:
        return Path(base)
    data_dir = getattr(getattr(runtime, "config", None), "data_dir", None)
    if data_dir:
        return Path(str(data_dir)) / "experimental-drawn"
    return Path("/tmp/havoc-experimental-drawn")


def _sweep(folder: Path) -> None:
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


async def cmd_fastdraw(ctx: CommandContext) -> None:
    directory = getattr(ctx.runtime, "services", None)
    action = (ctx.sub or "").lower()
    prompt = " ".join(ctx.args).strip()

    if action in {"info", "status"} or (not prompt and not action):
        await ctx.reply(_info(ctx, directory))
        return
    if directory is None:
        raise CommandError("errors.not_available")
    if not prompt:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())

    seed: int | None = None
    raw_seed = ctx.flag("seed")
    if raw_seed:
        try:
            seed = int(raw_seed)
        except ValueError as exc:
            raise CommandError("errors.not_a_number", field="seed") from exc
        if not 0 <= seed < 2**64:
            raise CommandError("errors.too_large", field="seed", maximum="2⁶⁴−1")

    folder = _outdir(ctx.runtime)
    try:
        _sweep(folder)
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path(os.environ.get("TMPDIR", "/tmp")) / "havoc-experimental-drawn"
        folder.mkdir(parents=True, exist_ok=True)

    notice = await ctx.reply(
        "\n\n".join(
            [
                ui.title("⚡", ctx.t("fastdraw.title")),
                ui.note(ctx.t("fastdraw.working", prompt=escape(truncate(prompt, 120)))),
            ]
        )
    )
    notice_message = getattr(notice, "message", None)

    try:
        path = await asyncio.wait_for(
            generate(directory, prompt, seed=seed, outdir=folder),
            timeout=GENERATE_TIMEOUT,
        )
    except TimeoutError:
        await _edit(ctx, notice_message, _error(ctx, ctx.t("fastdraw.too_slow")))
        return
    except ExperimentalError as exc:
        await _edit(ctx, notice_message, _error(ctx, escape(str(exc))))
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - one experiment must not take the bot down
        await _edit(ctx, notice_message, _error(ctx, escape(str(type(exc).__name__))))
        return

    caption = "\n\n".join(
        [
            ui.title("⚡", ctx.t("fastdraw.title")),
            ui.note(ctx.t("fastdraw.caption", prompt=escape(truncate(prompt, 120)))),
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
        await _edit(ctx, notice_message, _error(ctx, escape(str(exc))))
        return

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
                await ctx.sender.edit(ctx.chat_id, notice_message, ui.note(ctx.t("fastdraw.done")))
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


def _error(ctx: CommandContext, reason: str) -> str:
    return "\n\n".join(
        [ui.title("⚡", ctx.t("fastdraw.title")), ui.note(reason), ui.footer(ctx.t("fastdraw.retry_hint"))]
    )


def _info(ctx: CommandContext, directory: Any) -> str:
    rows: list[tuple[str, str]] = []
    if directory is None:
        rows.append((ui.inline("state", "off"), ui.inline("detail", ctx.t("fastdraw.not_configured"))))
        return "\n\n".join(
            [
                ui.title("⚡", ctx.t("fastdraw.title")),
                ui.panel("\n".join(rows)),
                ui.footer(ctx.t("fastdraw.usage_hint", prefix=ctx.runtime.prefix)),
            ]
        )
    try:
        endpoint = directory.resolve("sdxl-experimental", force=True)
    except Exception:  # noqa: BLE001 - an unreachable discovery file is a status, not a crash
        endpoint = None
    if endpoint is None or not getattr(endpoint, "ok", False):
        reason = getattr(endpoint, "state", "unavailable")
        rows.append((ui.inline("state", str(reason)), ui.inline("detail", ctx.t("fastdraw.between_runs"))))
    else:
        rows.append((ui.inline("state", "online"), ui.inline("detail", str(getattr(endpoint, "model", "SDXL Lightning")))))
        rows.append((ui.inline("steps", "4"), ui.inline("detail", "768×768 · owner-only")))
    return "\n\n".join(
        [
            ui.title("⚡", ctx.t("fastdraw.title")),
            ui.panel("\n".join(rows)),
            ui.footer(ctx.t("fastdraw.usage_hint", prefix=ctx.runtime.prefix)),
        ]
    )


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "fastdraw",
                cmd_fastdraw,
                category="media",
                aliases=("xdraw",),
                usage="<prompt> [--seed n] | info",
                subcommands=("info", "status"),
                changes_state=True,
                owner_only=True,
                hidden=True,
            )
        ]
    )
