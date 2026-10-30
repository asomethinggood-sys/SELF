"""The corners of Telegram that are a raw request, not a friendly method.

Chats, groups, invite links, folders, notification settings, sessions. Telethon
exposes each of these as ``client(SomeRequest(...))`` and nothing else, so they
are collected here instead of being scattered through the command handlers:

* every call is **attempted, never assumed** — a version that renamed a request
  produces a sentence, not a traceback;
* the objects returned are plain dictionaries, so a command, a test and a future
  caller all read the same shape;
* nothing here writes without the caller deciding to: ``invite_link`` creates a
  link only when the chat has none, ``mute`` takes seconds, ``join`` takes a link
  he supplied.

The imports from ``telethon`` are inside the methods on purpose: this module is
imported by the command registry at boot, and a missing optional dependency must
not take the bot down.
"""

from __future__ import annotations

from typing import Any

from ..utils.security import redact_text

__all__ = ["TelegramExtras", "ExtrasError"]


class ExtrasError(RuntimeError):
    """A Telegram call this build cannot make, or one the account may not."""

    def __init__(self, message: str, *, kind: str = "unsupported") -> None:
        super().__init__(message)
        self.kind = kind


def _name(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    joined = " ".join(part for part in parts if part)
    if joined:
        return joined
    username = getattr(entity, "username", None)
    return f"@{username}" if username else str(getattr(entity, "id", "?"))


class TelegramExtras:
    """Raw-request helpers, one method per thing a person can actually do."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @property
    def client(self) -> Any:
        return self.runtime.client

    @property
    def logger(self) -> Any:
        return self.runtime.logger

    async def _call(self, request_factory: Any, *args: Any, **kwargs: Any) -> Any:
        """Build and send one raw request, with every failure named."""
        client = self.client
        if client is None:
            raise ExtrasError("this build has no Telegram connection", kind="offline")
        try:
            request = request_factory(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a missing class is a version problem
            raise ExtrasError(
                f"this Telegram library has no {request_factory.__name__}",
                kind="unsupported",
            ) from exc
        try:
            return await client(request)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            raise ExtrasError(redact_text(str(exc))[:300], kind="refused") from exc

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    async def groups(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every group and supergroup in the chat list, newest activity first."""
        client = self.client
        getter = getattr(client, "get_dialogs", None)
        if getter is None:
            raise ExtrasError("this build cannot read the chat list", kind="offline")
        try:
            dialogs = await getter(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise ExtrasError(redact_text(str(exc))[:200], kind="refused") from exc
        rows: list[dict[str, Any]] = []
        for dialog in dialogs or []:
            entity = getattr(dialog, "entity", None) or dialog
            is_group = bool(getattr(dialog, "is_group", None))
            is_channel = bool(getattr(dialog, "is_channel", None))
            # A channel is a broadcast, a "megagroup" is a group. Telethon says
            # which with an attribute; fall back to "has a title and no username
            # but members" only when it does not.
            megagroup = bool(
                getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
            )
            if not is_group and not (is_channel and megagroup):
                continue
            if is_channel and not megagroup:
                continue
            rows.append(
                {
                    "id": getattr(dialog, "id", None) or getattr(entity, "id", None),
                    "title": _name(entity),
                    "username": getattr(entity, "username", None) or "",
                    "members": int(getattr(entity, "participants_count", 0) or 0),
                    "date": getattr(dialog, "date", None),
                    "entity": entity,
                }
            )
        return rows

    async def participants(self, chat_id: Any, limit: int = 200) -> list[dict[str, Any]]:
        """Members, as name + id + admin flag. ``[]`` when the account may not ask."""
        client = self.client
        getter = getattr(client, "get_participants", None)
        if getter is not None:
            try:
                people = await getter(chat_id, limit=limit)
            except Exception as exc:  # noqa: BLE001
                raise ExtrasError(redact_text(str(exc))[:200], kind="refused") from exc
            return [self._member(row) for row in people or []]
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.types import ChannelParticipantsSearch

        result = await self._call(
            GetParticipantsRequest,
            channel=chat_id,
            filter=ChannelParticipantsSearch(""),
            offset=0,
            limit=limit,
            hash=0,
        )
        rows = getattr(result, "participants", None) or []
        users = {getattr(user, "id", None): user for user in (getattr(result, "users", None) or [])}
        return [self._member(users.get(getattr(row, "user_id", None)) or row) for row in rows]

    @staticmethod
    def _member(row: Any) -> dict[str, Any]:
        user = getattr(row, "user", None) or row
        return {
            "id": getattr(user, "id", None),
            "name": _name(user),
            "username": getattr(user, "username", None) or "",
            "admin": bool(
                getattr(row, "admin_rights", None)
                or getattr(row, "creator", False)
                or getattr(row, "is_admin", False)
            ),
        }

    # ------------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------------
    async def invite_link(self, chat_id: Any, *, create: bool = True) -> str:
        """The chat's invite link, or a fresh one. Never a guess about the chat."""
        from telethon.tl.functions.messages import ExportChatInviteRequest

        # ``create`` is kept in the signature because callers ask for it, but
        # Telegram has one request for both: exporting *is* creating when none
        # exists. A failure here is the account's rights, not our choice.
        invite = await self._call(ExportChatInviteRequest, peer=chat_id)
        link = getattr(invite, "link", None)
        if not link:
            raise ExtrasError("Telegram did not return a link", kind="refused")
        return str(link)

    async def join(self, link: str) -> str:
        """Join by the link he gave — a hash invite or a public handle, in one place."""
        import re

        raw = str(link or "").strip()
        if not raw:
            raise ExtrasError("there is no link to join", kind="bad_args")
        match = re.search(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)(?P<hash>[\w-]+)", raw)
        if match:
            from telethon.tl.functions.messages import ImportChatInviteRequest

            result = await self._call(ImportChatInviteRequest, match.group("hash"))
            chats = getattr(result, "chats", None) or []
            return _name(chats[0]) if chats else "the chat"
        match = re.search(r"(?:t\.me|telegram\.me)/(?P<name>@?[\w.+]+)", raw)
        if match:
            from telethon.tl.functions.channels import JoinChannelRequest

            await self._call(JoinChannelRequest, match.group("name").lstrip("@"))
            return match.group("name").lstrip("@")
        raise ExtrasError("that is not a Telegram link", kind="bad_args")

    # ------------------------------------------------------------------
    # Folders, muting, sessions
    # ------------------------------------------------------------------
    async def folders(self) -> list[dict[str, Any]]:
        from telethon.tl.functions.messages import GetDialogFiltersRequest

        result = await self._call(GetDialogFiltersRequest)
        rows = getattr(result, "filters", None) or result or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if getattr(row, "id", None) is None and not getattr(row, "title", None):
                continue
            peers = getattr(row, "include_peers", None) or []
            out.append(
                {
                    "id": getattr(row, "id", None),
                    "title": str(getattr(row, "title", "") or "—"),
                    "count": len(peers),
                }
            )
        return out

    async def mute(self, chat_id: Any, seconds: int) -> None:
        """Silence a chat for ``seconds``; ``0`` unmutes."""
        from datetime import timedelta

        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

        from ..utils.time import utc_now

        until = utc_now() + timedelta(seconds=max(0, int(seconds)))
        await self._call(
            UpdateNotifySettingsRequest,
            peer=InputNotifyPeer(peer=chat_id),
            settings=InputPeerNotifySettings(mute_until=until, show_previews=False),
        )

    async def sessions(self) -> list[dict[str, Any]]:
        """Every device signed into the account — the honest answer to "who is in me"."""
        from telethon.tl.functions.account import GetAuthorizationsRequest

        result = await self._call(GetAuthorizationsRequest)
        out: list[dict[str, Any]] = []
        for row in getattr(result, "authorizations", None) or []:
            out.append(
                {
                    "hash": getattr(row, "hash", 0),
                    "device": getattr(row, "device_model", "") or "—",
                    "platform": getattr(row, "platform", "") or "—",
                    "country": getattr(row, "country", "") or "—",
                    "current": bool(getattr(row, "current", False)),
                    "date": getattr(row, "date_created", None),
                }
            )
        return out

    async def terminate_session(self, session_hash: int) -> None:
        from telethon.tl.functions.account import ResetAuthorizationRequest

        await self._call(ResetAuthorizationRequest, hash=int(session_hash))
