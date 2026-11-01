"""Search, chat info, bookmarks, notes, media and exports.

Privacy rules baked in: search only touches chats the account can already read;
bookmarks/notes are stored locally in SQLite (never as Telegram messages);
media is downloaded only on explicit request, with a size limit and sanitized
filenames; exports contain application data, never private conversations.

Feature areas: 41-50, 44, 46, 47, 48, 77.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..services.service_directory import STATE_BETWEEN_RUNS, STATE_EXPIRED, describe_age
from ..services.websearch import SearchError
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..utils.parsing import paginate
from ..utils.security import ensure_within, safe_filename
from ..utils.text import truncate
from ..utils.time import TimeParseError, iso, parse_when, utc_now
from .helpers import (
    chat_link,
    display_name,
    fmt_time,
    human_duration,
    human_size,
    media_label,
    parse_page_arg,
    reply_text,
    require_reply,
    resolve_target,
    snippet,
    tz_for,
)

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB default ceiling


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
async def cmd_search(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    if runtime.client is None:
        raise CommandError("errors.not_connected")
    raw_query = " ".join(arg for arg in ctx.args if not arg.startswith("--")).strip()
    # ``.search rent in @sara`` reads the way people write it; ``--chat`` stays
    # for scripts. Either way the words before ``in`` are the query.
    query, _, inline_chat = raw_query.partition(" in ")
    query = query.strip()
    # "what did sara say yesterday" has no words to search for: the *person* and
    # the *day* are the query. Both are filters, and either one alone is enough
    # to be worth running — refusing it because there is no keyword turned a
    # perfectly clear question into a syntax error.
    from_arg = (ctx.flag("from") or "").strip()
    since_arg = (ctx.flag("since") or "").strip()
    if not query and not from_arg:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    limit = min(50, max(1, int(ctx.flag("limit") or 10)))
    chat_arg = ctx.flag("chat") or inline_chat.strip()
    entity: Any = None
    if chat_arg:
        chat_id, entity, error = await resolve_target(ctx, chat_arg)
        if error or chat_id is None:
            raise CommandError(error or "errors.entity_not_found")
        entity = entity or chat_id

    sender: Any = None
    if from_arg:
        sender_id, sender_entity, error = await resolve_target(ctx, from_arg)
        if error or sender_id is None:
            raise CommandError(error or "errors.entity_not_found")
        sender = getattr(sender_entity, "id", None) or sender_id
    since: datetime | None = None
    if since_arg:
        try:
            since = parse_when(since_arg, tz_for(ctx))
        except TimeParseError as exc:
            raise CommandError(
                "errors.invalid_time", detail=escape(since_arg), example="yesterday | 2 hours ago"
            ) from exc

    try:
        if sender is None and since is None:
            messages = await runtime.client.get_messages(entity, search=query, limit=limit)
        else:
            # Filtered reads need a window to look at, not "the ten most recent":
            # the person's last ten messages may all be older than yesterday.
            fetched = await _filtered_messages(
                runtime.client, entity, query=query, sender=sender, since=since, limit=max(limit * 6, 60)
            )
            messages = list(fetched)[:limit]
    except Exception as exc:  # noqa: BLE001
        raise CommandError("errors.search_failed", detail=escape(str(exc)[:150])) from exc

    if not messages:
        await ctx.reply(ctx.t("search.empty", query=escape(query or from_arg or since_arg)))
        return
    if query:
        header = ctx.t("search.header", count=len(messages), query=escape(query))
    else:
        header = ctx.t("search.header_from", count=len(messages), who=escape(from_arg or "—"))
    lines = [bold(header)]
    for message in messages:
        text = reply_text(message)
        link = chat_link(getattr(message, "chat", None))
        when = getattr(message, "date", None)
        stamp = escape(when.strftime("%Y-%m-%d %H:%M")) if when else "—"
        prefix = "⚯ " if link else ""
        lines.append(f"{prefix}{code(stamp)} — {snippet(text, 100) or '[media]'}")
    await ctx.reply("\n".join(lines))


async def _filtered_messages(
    client: Any,
    entity: Any,
    *,
    query: str,
    sender: Any,
    since: datetime | None,
    limit: int,
) -> list[Any]:
    """Messages that match a keyword, a person and a day — whichever were asked for.

    ``from_user`` and ``offset_date`` are Telethon arguments, and the chat is the
    one being read: a filtered search never wanders into another conversation.
    """
    kwargs: dict[str, Any] = {"limit": limit}
    if query:
        kwargs["search"] = query
    if sender is not None:
        kwargs["from_user"] = sender
    rows = await client.get_messages(entity, **kwargs)
    out: list[Any] = []
    for message in rows or []:
        if sender is not None:
            who = getattr(message, "sender_id", None) or getattr(message, "from_id", None)
            who = getattr(who, "user_id", who)
            if who is not None and int(who) != int(sender):
                continue
        if since is not None:
            when = getattr(message, "date", None)
            if when is not None and when < since:
                continue
        out.append(message)
    return out


async def cmd_chats(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    if runtime.client is None:
        raise CommandError("errors.not_connected")
    query = " ".join(ctx.args).strip().lower()
    try:
        dialogs = await runtime.client.get_dialogs(limit=200)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("errors.search_failed", detail=escape(str(exc)[:150])) from exc
    matches = []
    for dialog in dialogs or []:
        name = display_name(dialog.entity, "")
        username = getattr(dialog.entity, "username", "") or ""
        if not query or query in name.lower() or query in str(username).lower():
            matches.append((name or username or str(dialog.id), dialog.id, username))
    if not matches:
        # An unfiltered list that came back empty has no query to put in quotes:
        # "no chats matched «»" is the sentence that produced.
        line = ctx.t("chats.empty", query=query) if query else ctx.t("chats.empty_all")
        await ctx.reply(ui.card(ctx.t("chats.title"), [line], icon="✉"))
        return
    # A list is a card: a bold title, a drawn rule, the rows, and a footer that
    # says what the number means. "No chats" is the same card with one line.
    rows = []
    for name, chat_id, username in matches[:25]:
        label = name + (f" (@{username})" if username else "")
        rows.append(f"{label} — {chat_id}")
    await ctx.reply(
        ui.card(
            ctx.t("chats.title"),
            rows,
            icon="✉",
            footer=ctx.t("chats.header", count=len(matches)),
        )
    )


#: What a Telegram entity *is*, in words a person reads.
def _entity_kind(entity: Any, self_id: int | None) -> str:
    """``self`` / ``bot`` / ``user`` / ``group`` / ``channel`` / ``unknown``.

    The card used to print ``type(entity).__name__`` — the Python class. His own
    account came out as "Type: User", which reads as "you are a stranger to me".
    Telegram has exactly three kinds of thing here, and the one that matters most
    is "this is you".
    """
    if entity is None:
        return "unknown"
    identifier = getattr(entity, "id", None)
    if self_id and identifier and int(identifier) == int(self_id):
        return "self"
    if getattr(entity, "bot", False):
        return "bot"
    name = type(entity).__name__
    if name == "User" or getattr(entity, "first_name", None):
        return "user"
    if getattr(entity, "megagroup", False) or name.endswith("Chat") or name == "Chat":
        return "group"
    if name == "Channel" or getattr(entity, "title", None):
        return "channel"
    return "unknown"


async def cmd_chatinfo(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    entity = getattr(ctx.event, "chat", None)
    reply = await ctx.reply_message()
    if reply is not None:
        sender = getattr(reply, "sender", None) or getattr(reply, "from_id", None)
        if sender is not None:
            entity = sender
    if entity is None and runtime.client is not None:
        try:
            entity = await runtime.client.get_entity(ctx.chat_id)
        except Exception:  # noqa: BLE001
            entity = None
    self_id = int(getattr(runtime.permissions, "self_id", 0) or 0) or None
    identifier = getattr(entity, "id", None)
    kind = _entity_kind(entity, self_id)
    is_me = kind == "self"
    # A phone number is somebody's private business. It is shown for his own
    # account (it is his card, he knows it) and for nobody else, because this
    # command answers whatever is replied to.
    phone = getattr(entity, "phone", None) if is_me else None
    # Plain words, not pre-built markup: ``ui.card`` italicises and escapes each
    # line itself, so a ``ui.fact_line`` here arrived twice-escaped as
    # ``<i>&lt;i&gt;◇ ·&lt;/i&gt;`` — the machinery he was never supposed to see.
    rows = [
        f"◇ {ctx.t('chatinfo.id')} · {identifier or ctx.chat_id}",
        f"✦ {ctx.t('chatinfo.who')} {ctx.t(f'chatinfo.kind_{kind}')}",
        f"✎ {display_name(entity)}",
    ]
    username = getattr(entity, "username", None)
    rows.append(f"⌕ {'@' + str(username) if username else '—'}")
    if phone:
        rows.append(f"☎ {phone}")
    if getattr(entity, "bot", False):
        rows.append(f"⚙ {ctx.t('chatinfo.bot_yes')}")
    labels = await ctx.store.chat_labels(ctx.chat_id)
    if labels:
        rows.append(f"❐ {', '.join(labels)}")
    link = chat_link(entity)
    if link:
        rows.append(f"↗ {link}")
    await ctx.reply(
        ui.card(
            ctx.t("chatinfo.header"),
            rows,
            icon="ℹ",
            footer=ctx.t("chatinfo.is_you") if is_me else "",
        )
    )


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------
async def cmd_bookmark(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    store = ctx.store
    if action in {"save", "add"}:
        reply = await ctx.reply_message()
        title = " ".join(ctx.args).strip() or None
        if reply is None:
            body = title
            if not body:
                raise CommandError("bookmark.usage_hint", prefix=ctx.runtime.prefix)
            bookmark_id = await store.add_bookmark(ctx.chat_id, kind="text", title=None, content=body, sender_id=getattr(ctx.event, "sender_id", None))
            await ctx.reply(ctx.t("bookmark.saved_text", id=bookmark_id))
            return
        text = reply_text(reply)
        bookmark_id = await store.add_bookmark(
            ctx.chat_id,
            message_id=getattr(reply, "id", None),
            kind="message" if text else "media",
            title=title or snippet(text, 60) or None,
            content=text or f"[{media_label(reply)}]",
            sender_id=getattr(reply, "sender_id", None),
            labels=[],
        )
        await ctx.reply(ctx.t("bookmark.saved", id=bookmark_id))
        return
    if action in {"list", "ls"}:
        page = parse_page_arg(ctx.arg(0))
        rows = await store.list_bookmarks(limit=100)
        items, pages = paginate(rows, page, 10)
        lines = [bold(ctx.t("bookmark.header", count=len(rows), page=page, pages=pages))]
        if not rows:
            lines.append(ctx.t("bookmark.empty", prefix=ctx.runtime.prefix))
        for row in items:
            lines.append(
                f"#{row['id']} {code(escape(truncate(row['title'] or row['content'] or '', 50)))} "
                f"— {escape(str(row['created_at'])[:16])}"
            )
        await ctx.reply("\n".join(lines))
        return
    if action in {"search", "find"}:
        query = " ".join(ctx.args).strip()
        if not query:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        rows = await store.search_bookmarks(query)
        if not rows:
            await ctx.reply(ctx.t("bookmark.no_results", query=escape(query)))
            return
        lines = [bold(ctx.t("bookmark.search_header", count=len(rows), query=escape(query)))]
        for row in rows:
            lines.append(f"#{row['id']} {escape(truncate(row['title'] or row['content'] or '', 80))}")
        await ctx.reply("\n".join(lines))
        return
    if action in {"show", "view"}:
        bookmark_id = _int_arg(ctx, 0)
        row = await store.get_bookmark(bookmark_id)
        if row is None:
            raise CommandError("errors.not_found", what="bookmark", id=bookmark_id)
        await ctx.reply(
            bold(ctx.t("bookmark.show_header", id=bookmark_id))
            + "\n"
            + key_value_block(
                [
                    (ctx.t("bookmark.title"), row["title"] or "—"),
                    (ctx.t("bookmark.chat"), row["chat_id"]),
                    (ctx.t("bookmark.message"), row["message_id"] or "—"),
                    (ctx.t("bookmark.created"), row["created_at"]),
                ]
            )
            + "\n\n"
            + pre(truncate(row["content"] or "", 2000))
        )
        return
    if action in {"delete", "rm", "remove"}:
        bookmark_id = _int_arg(ctx, 0)
        if not await store.delete_bookmark(bookmark_id):
            raise CommandError("errors.not_found", what="bookmark", id=bookmark_id)
        await ctx.reply(ctx.t("bookmark.deleted", id=bookmark_id))
        return
    if action in {"export", "import"}:
        await _export_table(ctx, "bookmarks")
        return
    raise CommandError("errors.invalid_choice", field="action", choices="save, list, search, show, delete, export")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
async def cmd_note(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    store = ctx.store
    if action in {"add", "new"}:
        raw = ctx.rest_raw.strip()
        if not raw:
            reply = await ctx.reply_message()
            raw = reply_text(reply).strip()
        if not raw:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        title, body = (raw.split("|", 1) + [""])[:2] if "|" in raw else (None, raw)
        if title is not None:
            title = title.strip() or None
            body = body.strip()
        if not body:
            raise CommandError("errors.empty_value", field="note")
        note_id = await store.add_note(ctx.chat_id, body, title=title)
        if title:
            await ctx.reply(ctx.t("note.added", id=note_id, title=escape(title)))
        else:
            # "Note #1 saved (—)" is a parenthesis around nothing. An untitled
            # note is the normal case; say what was saved instead.
            await ctx.reply(
                ctx.t("note.added_untitled", id=note_id, text=escape(truncate(body, 60)))
            )
        return
    if action in {"list", "ls"}:
        rows = await store.list_notes(limit=100)
        if not rows:
            await ctx.reply(
                ui.card(ctx.t("note.title"), [ctx.t("note.empty", prefix=ctx.runtime.prefix)], icon="✎")
            )
            return
        await ctx.reply(
            ui.card(
                ctx.t("note.title"),
                [f"#{row['id']} {truncate(row['title'] or row['body'], 60)}" for row in rows[:25]],
                icon="✎",
                footer=ctx.t("note.header", count=len(rows)),
            )
        )
        return
    if action in {"search", "find"}:
        query = " ".join(ctx.args).strip()
        if not query:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        rows = await store.search_notes(query)
        if not rows:
            await ctx.reply(ctx.t("note.no_results", query=escape(query)))
            return
        lines = [bold(ctx.t("note.search_header", count=len(rows)))]
        for row in rows:
            lines.append(f"#{row['id']} {escape(truncate(row['title'] or row['body'], 80))}")
        await ctx.reply("\n".join(lines))
        return
    if action in {"show", "view"}:
        note_id = _int_arg(ctx, 0)
        row = await store.db.fetch_one("SELECT * FROM notes WHERE id = ?", (note_id,))
        if row is None:
            raise CommandError("errors.not_found", what="note", id=note_id)
        await ctx.reply(
            bold(ctx.t("note.show_header", id=note_id, title=escape(row["title"] or "—")))
            + "\n"
            + pre(truncate(row["body"], 3000))
        )
        return
    if action in {"delete", "rm"}:
        note_id = _int_arg(ctx, 0)
        if not await store.delete_note(note_id):
            raise CommandError("errors.not_found", what="note", id=note_id)
        await ctx.reply(ctx.t("note.deleted", id=note_id))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="add, list, search, show, delete")


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
async def cmd_media(ctx: CommandContext) -> None:
    action = (ctx.sub or "info").lower()
    reply = await require_reply(ctx)
    media = getattr(reply, "media", None)
    if media is None:
        raise CommandError("media.no_media")
    label = media_label(reply)
    document = getattr(media, "document", None)
    size = getattr(document, "size", None)
    filename = getattr(document, "attributes", None)
    file_name = None
    duration = None
    width = height = None
    for attribute in filename or []:
        file_name = getattr(attribute, "file_name", None) or file_name
        duration = getattr(attribute, "duration", None) or duration
        width = getattr(attribute, "w", None) or width
        height = getattr(attribute, "h", None) or height

    pairs = [
        (ctx.t("media.type"), label),
        (ctx.t("media.message"), getattr(reply, "id", "—")),
        (ctx.t("media.size"), human_size(size)),
        (ctx.t("media.filename"), file_name or "—"),
        (ctx.t("media.dimensions"), f"{width}×{height}" if width and height else "—"),
        (ctx.t("media.duration"), human_duration(duration)),
        (ctx.t("media.date"), fmt_time(iso(getattr(reply, "date", utc_now())), tz_for(ctx), ctx.language)),
    ]
    if action in {"info", "show"}:
        await ctx.reply(bold(ctx.t("media.header")) + "\n" + key_value_block(pairs))
        return
    if action in {"download", "save"}:
        if ctx.runtime.client is None:
            raise CommandError("errors.not_connected")
        if size and size > MAX_DOWNLOAD_BYTES:
            raise CommandError("media.too_large", size=human_size(size), limit=human_size(MAX_DOWNLOAD_BYTES))
        downloads = ctx.config.data_dir / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        proposed = file_name or f"{label}-{getattr(reply, 'id', 'file')}"
        safe_name = safe_filename(proposed, fallback=f"{label}-{getattr(reply, 'id', 'file')}")
        target = ensure_within(downloads, downloads / safe_name)
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            target = ensure_within(downloads, downloads / f"{stem}-{int(datetime.now().timestamp())}{suffix}")
        try:
            await ctx.runtime.client.download_media(reply, file=str(target))
        except Exception as exc:  # noqa: BLE001
            raise CommandError("media.download_failed", detail=escape(str(exc)[:150])) from exc
        if not target.exists():
            raise CommandError("media.download_failed", detail="file was not written")
        await ctx.runtime.sender.send(ctx.chat_id, ctx.t("media.downloaded", name=escape(target.name), size=human_size(target.stat().st_size)), file=str(target))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="info, download")


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
EXPORT_TABLES = {
    "bookmarks": ("bookmarks",),
    "notes": ("notes",),
    "settings": ("settings", "chat_settings", "language_prefs", "features"),
    "reminders": ("reminders", "schedules"),
    "templates": ("templates", "autoreplies"),
    "rules": ("triggers", "watches", "reaction_rules"),
    "all": None,
}


async def _export_table(ctx: CommandContext, kind: str) -> None:
    tables = EXPORT_TABLES.get(kind)
    payload = await ctx.store.export_tables(tables)
    exports = ctx.config.data_dir / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    path = ensure_within(exports, exports / f"havoc-{kind}-{stamp}.json")
    body = {
        "version": 1,
        "kind": kind,
        "exported_at": iso(utc_now()),
        "tables": payload,
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = sum(len(items) for items in payload.values())
    await ctx.runtime.sender.send(
        ctx.chat_id,
        ctx.t("export.saved", kind=escape(kind), rows=rows),
        file=str(path),
    )


async def cmd_export(ctx: CommandContext) -> None:
    kind = (ctx.sub or ctx.arg(0) or "").lower()
    if kind not in EXPORT_TABLES:
        raise CommandError("errors.invalid_choice", field="kind", choices=", ".join(EXPORT_TABLES))
    await _export_table(ctx, kind)


async def cmd_saved(ctx: CommandContext) -> None:
    bookmarks = len(await ctx.store.list_bookmarks(limit=500))
    notes = len(await ctx.store.list_notes(limit=500))
    reminders = len(await ctx.store.list_reminders(status="pending", limit=500))
    await ctx.reply(
        bold(ctx.t("saved.header"))
        + "\n"
        + key_value_block(
            [
                (ctx.t("saved.bookmarks"), bookmarks),
                (ctx.t("saved.notes"), notes),
                (ctx.t("saved.reminders"), reminders),
            ]
        )
        + "\n\n"
        + ctx.t("saved.hint", prefix=ctx.runtime.prefix)
    )


async def cmd_labels(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls"}:
        labels = await ctx.store.chat_labels(ctx.chat_id)
        await ctx.reply(
            bold(ctx.t("labels.header", chat=ctx.chat_id))
            + "\n"
            + (", ".join(labels) if labels else ctx.t("labels.empty"))
        )
        return
    if action in {"add", "remove", "rm"}:
        label = (ctx.arg(0) or "").strip().lower()
        if not label:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        if action == "add":
            await ctx.store.add_chat_label(ctx.chat_id, label)
            await ctx.reply(ctx.t("labels.added", label=escape(label)))
            return
        if not await ctx.store.remove_chat_label(ctx.chat_id, label):
            raise CommandError("errors.not_found", what="label", id=escape(label))
        await ctx.reply(ctx.t("labels.removed", label=escape(label)))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, remove")


async def cmd_web(ctx: CommandContext) -> None:
    """``.web <query>`` — the open web, through the engine he runs himself.

    Separate from ``.search`` on purpose: that one searches *his* Telegram
    history and needs no network at all. This one is the internet, and it says
    plainly when there is no engine configured instead of answering with an
    empty card that looks like "no results exist".

    One message per trigger: the lookup is expected to be fast and the answer is
    the card. A "looking it up" placeholder that has to be edited is two API
    calls for a search that usually takes a second.
    """
    service = getattr(ctx.runtime, "web_search", None)
    action = (ctx.sub or "").lower()
    # `args` already excludes the subcommand and the `--flags`.
    query = " ".join(ctx.args).strip()
    if action in {"info", "status", "help"}:
        await ctx.reply(_web_info(ctx, service))
        return
    if not query:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    if service is None or not getattr(service, "available", False):
        await ctx.reply(_web_info(ctx, service))
        return
    try:
        limit = max(1, min(int(ctx.flag("limit") or 5), 10))
    except ValueError:
        limit = 5
    try:
        results = await service.search(query, limit=limit)
    except SearchError as exc:
        await ctx.reply(_web_error(ctx, exc))
        return
    await ctx.reply(_web_card(ctx, query, results))


def _web_info(ctx: CommandContext, service: Any) -> str:
    if service is None or not getattr(service, "available", False):
        directory = getattr(ctx.runtime, "services", None)
        if directory is not None:
            endpoint = directory.resolve("search")
            never_published = endpoint.state == "not_found" and directory.has_workflow("search")
            if endpoint.state in {STATE_BETWEEN_RUNS, STATE_EXPIRED} or never_published:
                return "\n\n".join(
                    [
                        ui.title("◎", ctx.t("web.title")),
                        ui.note(
                            ctx.t(
                                "web.never_published" if never_published else "web.between_runs",
                                age=describe_age(endpoint.age, ctx.language),
                            )
                        ),
                        ui.footer(
                            ctx.t("web.hosted_hint" if never_published else "web.usage_hint", prefix=ctx.runtime.prefix)
                        ),
                    ]
                )
        return "\n\n".join(
            [
                ui.title("◎", ctx.t("web.title")),
                ui.note(ctx.t("web.not_configured")),
                ui.footer(ctx.t("web.setup_hint", prefix=ctx.runtime.prefix)),
            ]
        )
    rows = [
        ctx.t("web.row_engine", url=escape(str(service.url))),
        ctx.t("web.row_requests", count=getattr(service, "requests", 0)),
    ]
    engines = getattr(service, "engines", ())
    if engines:
        rows.append(ctx.t("web.row_engines", names=escape(", ".join(engines))))
    return "\n\n".join(
        [ui.title("◎", ctx.t("web.title")), ui.panel(rows), ui.footer(ctx.t("web.usage_hint", prefix=ctx.runtime.prefix))]
    )


def _web_card(ctx: CommandContext, query: str, results: list[Any]) -> str:
    if not results:
        return "\n\n".join(
            [
                ui.title("◎", ctx.t("web.title_for", query=escape(truncate(query, 60)))),
                ui.note(ctx.t("web.no_results")),
            ]
        )
    lines = []
    for index, row in enumerate(results, start=1):
        title = escape(truncate(row.title, 90))
        link = escape(row.url)
        line = f"{ui.code(str(index))} {ui.bold(title)}\n  {link}"
        if row.snippet:
            line += f"\n  {ui.note(escape(truncate(row.snippet, 160)))}"
        lines.append(line)
    return "\n\n".join(
        [
            ui.title("◎", ctx.t("web.title_for", query=escape(truncate(query, 60)))),
            ui.panel(lines),
            ui.footer(ctx.t("web.footer", count=len(results))),
        ]
    )


def _web_error(ctx: CommandContext, exc: SearchError) -> str:
    return "\n\n".join(
        [
            ui.title("◎", ctx.t("web.title")),
            ui.note(escape(str(exc))),
            ui.footer(ctx.t("web.usage_hint", prefix=ctx.runtime.prefix)),
        ]
    )


def _int_arg(ctx: CommandContext, index: int) -> int:
    try:
        return int(ctx.arg(index))
    except (TypeError, ValueError) as exc:
        raise CommandError("errors.not_a_number", field="id") from exc


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command("search", cmd_search, category="information", usage="<query> [--chat id] [--limit n]"),
            # The internet, kept apart from `.search` (which searches his own
            # history and never leaves the machine). An alias would blur two very
            # different answers to "search".
            Command(
                "web",
                cmd_web,
                category="information",
                aliases=("google", "internet"),
                usage="<query> [--limit n] | info",
                subcommands=("info", "status", "help"),
            ),
            Command("chats", cmd_chats, category="information", usage="[query]"),
            # Owner-only: a card with ids, links and a phone number is not
            # something a member of a group gets to ask of another member
            # through his account.
            Command(
                "chatinfo",
                cmd_chatinfo,
                category="information",
                aliases=("chatinfo", "info"),
                owner_only=True,
            ),
            Command("bookmark", cmd_bookmark, category="information", aliases=("bm",), usage="[save|list|search|show|delete|export]", changes_state=True, subcommands=('save', 'add', 'list', 'ls', 'search', 'find', 'show', 'view', 'delete', 'rm', 'remove', 'export', 'import')),
            Command("note", cmd_note, category="information", aliases=("notes",), usage="[add|list|search|show|delete]", changes_state=True, subcommands=('add', 'new', 'list', 'ls', 'search', 'find', 'show', 'view', 'delete', 'rm')),
            Command("media", cmd_media, category="information", usage="[info|download]", requires_reply=False, subcommands=('info', 'show', 'download', 'save')),
            Command("export", cmd_export, category="management", usage="<bookmarks|notes|settings|reminders|templates|rules|all>", changes_state=True, subcommands=('bookmarks', 'notes', 'settings', 'reminders', 'templates', 'rules', 'all')),
            Command("saved", cmd_saved, category="information", aliases=("savedmessages",)),
            Command("labels", cmd_labels, category="management", aliases=("label",), usage="[list|add|remove]", changes_state=True, subcommands=('list', 'ls', 'add', 'remove', 'rm')),
        ]
    )
