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

    # ------------------------------------------------------------------
    # Contacts and blocking
    # ------------------------------------------------------------------
    async def _input_peer(self, chat_id: Any) -> Any:
        client = self.client
        getter = getattr(client, "get_input_entity", None)
        if getter is None:
            raise ExtrasError("this build cannot address a chat", kind="offline")
        try:
            return await getter(chat_id)
        except Exception as exc:  # noqa: BLE001
            raise ExtrasError(redact_text(str(exc))[:200], kind="refused") from exc

    async def contacts(self, limit: int = 500) -> list[dict[str, Any]]:
        """The address book, as Telegram has it."""
        from telethon.tl.functions.contacts import GetContactsRequest

        result = await self._call(GetContactsRequest, hash=0)
        users = getattr(result, "users", None) or []
        rows: list[dict[str, Any]] = []
        for user in users:
            rows.append(
                {
                    "id": getattr(user, "id", None),
                    "name": _name(user),
                    "username": getattr(user, "username", None) or "",
                    "phone": getattr(user, "phone", None) or "",
                }
            )
        return rows[: max(1, int(limit))]

    async def import_contact(
        self, phone: str, first_name: str, last_name: str = ""
    ) -> list[dict[str, Any]]:
        """Add a number to the address book, the way a phone does it."""
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        result = await self._call(
            ImportContactsRequest,
            contacts=[
                InputPhoneContact(
                    client_id=0, phone=str(phone), first_name=str(first_name), last_name=str(last_name)
                )
            ],
        )
        users = getattr(result, "users", None) or []
        return [
            {
                "id": getattr(user, "id", None),
                "name": _name(user),
                "username": getattr(user, "username", None) or "",
            }
            for user in users
        ]

    async def block(self, chat_id: Any, *, blocked: bool = True) -> None:
        """Block or unblock one peer; reversible, and named in the answer."""
        from telethon.tl.functions.contacts import BlockRequest, UnblockRequest

        peer = await self._input_peer(chat_id)
        if blocked:
            await self._call(BlockRequest, id=peer)
        else:
            await self._call(UnblockRequest, id=peer)

    async def blocked(self, limit: int = 100) -> list[dict[str, Any]]:
        from telethon.tl.functions.contacts import GetBlockedRequest

        result = await self._call(GetBlockedRequest, offset=0, limit=max(1, int(limit)))
        users = getattr(result, "users", None) or []
        return [
            {
                "id": getattr(user, "id", None),
                "name": _name(user),
                "username": getattr(user, "username", None) or "",
            }
            for user in users
        ]

    # ------------------------------------------------------------------
    # Group administration
    # ------------------------------------------------------------------
    async def set_title(self, chat_id: Any, title: str) -> None:
        from telethon.tl.functions.channels import EditTitleRequest

        peer = await self._input_peer(chat_id)
        await self._call(EditTitleRequest, channel=peer, title=str(title)[:128])

    async def set_about(self, chat_id: Any, about: str) -> None:
        """The description.

        Current Telethon uses ``messages.EditChatAboutRequest`` for both groups
        and channels; older layers had a separate ``channels.EditAboutRequest``,
        so that is tried when the first class is missing.
        """
        peer = await self._input_peer(chat_id)
        text = str(about)[:255]
        try:
            from telethon.tl.functions.messages import EditChatAboutRequest
        except ImportError:
            EditChatAboutRequest = None  # noqa: N806 - older Telethon
        if EditChatAboutRequest is not None:
            await self._call(EditChatAboutRequest, peer=peer, about=text)
            return
        try:
            from telethon.tl.functions.channels import EditAboutRequest
        except ImportError as exc:
            raise ExtrasError("this Telegram library cannot change a description", kind="unsupported") from exc
        await self._call(EditAboutRequest, channel=peer, about=text)

    async def slowmode(self, chat_id: Any, seconds: int) -> int:
        from telethon.tl.functions.channels import ToggleSlowModeRequest

        peer = await self._input_peer(chat_id)
        wanted = max(0, min(int(seconds), 86400))
        await self._call(ToggleSlowModeRequest, channel=peer, seconds=wanted)
        return wanted

    async def leave(self, chat_id: Any) -> None:
        client = self.client
        deleter = getattr(client, "delete_dialog", None)
        if deleter is None:
            raise ExtrasError("this build cannot leave a chat", kind="offline")
        peer = await self._input_peer(chat_id)
        try:
            await deleter(peer)
        except Exception as exc:  # noqa: BLE001
            raise ExtrasError(redact_text(str(exc))[:200], kind="refused") from exc

    # ------------------------------------------------------------------
    # Things that are not a sentence: a poll, a place
    # ------------------------------------------------------------------
    @staticmethod
    def poll_media(
        question: str,
        options: list[str],
        *,
        quiz: bool = False,
        correct: int | None = None,
        multiple: bool = False,
        public: bool = False,
        closed: bool = False,
    ) -> Any:
        """A poll Telegram will actually accept, serialised once to prove it.

        Telethon 1.4x wants ``TextWithEntities`` and a ``hash``; a plain string
        builds and then explodes inside the request. ``_bytes()`` is the same
        call the client makes, so a bad poll is caught here, before it is queued.

        A quiz needs exactly one correct answer, counted from 1 the way a person
        counts; anything else is refused before it reaches Telegram.
        """
        from telethon.tl.types import InputMediaPoll, Poll, PollAnswer, TextWithEntities

        if len(options) < 2:
            raise ExtrasError("a poll needs at least two options", kind="bad_args")
        if quiz:
            if len(options) > 10:
                options = options[:10]
            if correct is None:
                raise ExtrasError("a quiz needs the number of the right answer", kind="bad_args")
            if not 1 <= int(correct) <= len(options):
                raise ExtrasError(
                    f"there is no option {correct} — the quiz has {len(options)}", kind="bad_args"
                )
            multiple = False  # Telegram refuses a quiz with several answers
        poll = Poll(
            id=0,
            question=TextWithEntities(text=str(question)[:255], entities=[]),
            answers=[
                PollAnswer(
                    text=TextWithEntities(text=str(option)[:100], entities=[]),
                    option=bytes([index]),
                )
                for index, option in enumerate(options[:10])
            ],
            hash=0,
            quiz=bool(quiz),
            public_voters=bool(public),
            multiple_choice=bool(multiple),
            closed=bool(closed),
        )
        media = InputMediaPoll(poll=poll, correct_answers=[int(correct) - 1] if quiz else None)
        media._bytes()
        return media

    @staticmethod
    def location_media(lat: float, lon: float) -> Any:
        from telethon.tl.types import InputGeoPoint, InputMediaGeoPoint

        if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
            raise ExtrasError("that is not a point on Earth", kind="bad_args")
        media = InputMediaGeoPoint(geo_point=InputGeoPoint(lat=float(lat), long=float(lon)))
        media._bytes()
        return media

    async def _send_media(self, chat_id: Any, media: Any, caption: str = "") -> Any:
        client = self.client
        sender = getattr(client, "send_file", None)
        if sender is None:
            raise ExtrasError("this build cannot send that", kind="offline")
        try:
            return await sender(chat_id, media, caption=caption or None)
        except Exception as exc:  # noqa: BLE001
            raise ExtrasError(redact_text(str(exc))[:300], kind="refused") from exc

    async def send_poll(
        self,
        chat_id: Any,
        question: str,
        options: list[str],
        *,
        quiz: bool = False,
        correct: int | None = None,
        multiple: bool = False,
        public: bool = False,
        closed: bool = False,
    ) -> Any:
        media = self.poll_media(
            question,
            options,
            quiz=quiz,
            correct=correct,
            multiple=multiple,
            public=public,
            closed=closed,
        )
        return await self._send_media(chat_id, media)

    async def send_location(self, chat_id: Any, lat: float, lon: float) -> Any:
        return await self._send_media(chat_id, self.location_media(lat, lon))

    # ------------------------------------------------------------------
    # Privacy and the two-step lock (read-only)
    # ------------------------------------------------------------------
    #: The settings that actually mean something to a person, in the order they
    #: appear in Telegram's own Privacy screen.
    PRIVACY_KEYS = (
        ("privacy.last_seen", "InputPrivacyKeyStatusTimestamp"),
        ("privacy.photo", "InputPrivacyKeyProfilePhoto"),
        ("privacy.phone", "InputPrivacyKeyPhoneNumber"),
        ("privacy.calls", "InputPrivacyKeyPhoneCall"),
        ("privacy.invites", "InputPrivacyKeyChatInvite"),
        ("privacy.added_by", "InputPrivacyKeyAddedByPhone"),
        ("privacy.birthday", "InputPrivacyKeyBirthday"),
    )

    @staticmethod
    def rule_word(rules: Any) -> str:
        """Telegram's rule list as one word: everyone, contacts, nobody, some."""
        names = [type(rule).__name__ for rule in rules or []]
        if "PrivacyValueDisallowAll" in names:
            return "nobody"
        if "PrivacyValueAllowAll" in names:
            return "everyone"
        if "PrivacyValueAllowCloseFriends" in names:
            return "close_friends"
        if "PrivacyValueAllowContacts" in names:
            return "contacts"
        if "PrivacyValueDisallowContacts" in names:
            return "everyone_except_contacts"
        if "PrivacyValueAllowUsers" in names:
            return "selected"
        return "unknown"

    async def privacy(self) -> list[dict[str, str]]:
        """Each of his privacy settings and what it currently allows."""
        import telethon.tl.types as types
        from telethon.tl.functions.account import GetPrivacyRequest

        rows: list[dict[str, str]] = []
        for label, class_name in self.PRIVACY_KEYS:
            key_class = getattr(types, class_name, None)
            if key_class is None:  # a layer without this setting
                continue
            result = await self._call(GetPrivacyRequest, key=key_class())
            rows.append(
                {
                    "label": label,
                    "value": self.rule_word(getattr(result, "rules", None)),
                }
            )
        return rows

    async def security(self) -> dict[str, Any]:
        """Whether the two-step lock is on, and its hint — never the password."""
        from telethon.tl.functions.account import GetPasswordRequest

        result = await self._call(GetPasswordRequest)
        if result is None:
            raise ExtrasError("Telegram returned no password state", kind="refused")
        return {
            "two_factor": bool(getattr(result, "has_password", False)),
            "hint": str(getattr(result, "hint", "") or ""),
            "recovery": bool(getattr(result, "has_recovery", False)),
            "pending_reset": bool(getattr(result, "pending_reset_date", None)),
        }

    # ------------------------------------------------------------------
    # Drafts and the archive
    # ------------------------------------------------------------------
    async def save_draft(self, chat_id: Any, text: str) -> None:
        """Put words in a chat's input box without sending them.

        A draft is the safest way to hand him a sentence he may want to edit
        first: nothing leaves the account, and Telegram shows it where he types.
        """
        from telethon.tl.functions.messages import SaveDraftRequest

        peer = await self._input_peer(chat_id)
        await self._call(SaveDraftRequest, peer=peer, message=str(text)[:4096])

    async def clear_draft(self, chat_id: Any) -> None:
        await self.save_draft(chat_id, "")

    async def drafts(self) -> list[dict[str, Any]]:
        """Every chat Telegram is holding a draft for."""
        from telethon.tl.functions.messages import GetAllDraftsRequest

        result = await self._call(GetAllDraftsRequest)
        rows: list[dict[str, Any]] = []
        for update in getattr(result, "updates", None) or []:
            peer = getattr(update, "peer", None)
            message = getattr(update, "message", None)
            text = getattr(message, "message", "") or ""
            if not text:
                continue
            rows.append(
                {
                    "id": getattr(peer, "user_id", None)
                    or getattr(peer, "chat_id", None)
                    or getattr(peer, "channel_id", None),
                    "text": str(text),
                    "date": getattr(message, "date", None),
                }
            )
        return rows

    async def set_archived(self, chat_id: Any, *, archived: bool = True) -> None:
        """Move a chat into the Archive folder (1) or back to the main list (0)."""
        from telethon.tl.functions.folders import EditPeerFoldersRequest
        from telethon.tl.types import InputFolderPeer

        peer = await self._input_peer(chat_id)
        await self._call(
            EditPeerFoldersRequest,
            folder_peers=[InputFolderPeer(peer=peer, folder_id=1 if archived else 0)],
        )

    # ------------------------------------------------------------------
    # Invite links that need a request — and clearing a history
    # ------------------------------------------------------------------
    @staticmethod
    def invite_hash(link: str) -> str:
        """``https://t.me/+AbCdEf`` or ``t.me/joinchat/AbCdEf`` → ``AbCdEf``."""
        text = (link or "").strip()
        marker = "+"
        if "/joinchat/" in text:
            return text.split("/joinchat/", 1)[1].strip("/").split("?")[0]
        if marker in text:
            return text.split(marker, 1)[1].strip("/").split("?")[0]
        raise ExtrasError("that is not an invite link", kind="bad_args")

    async def invite_info(self, link: str) -> dict[str, Any]:
        """What is behind a private invite — name, members, whether he is in."""
        from telethon.tl.functions.messages import CheckChatInviteRequest

        result = await self._call(CheckChatInviteRequest, self.invite_hash(link))
        chat = getattr(result, "chat", None) or result
        return {
            "already": type(result).__name__ == "ChatInviteAlready",
            "title": _name(chat) if chat is not None else "",
            "id": getattr(chat, "id", None),
            "members": int(getattr(result, "participants_count", 0) or getattr(chat, "participants_count", 0) or 0),
            "request": type(result).__name__ == "ChatInvitePeek",
        }

    async def clear_history(self, chat_id: Any, *, revoke: bool = False) -> None:
        """Delete the history of one chat — ``revoke`` deletes it for everyone."""
        from telethon.tl.functions.messages import DeleteHistoryRequest

        peer = await self._input_peer(chat_id)
        await self._call(
            DeleteHistoryRequest, peer=peer, max_id=0, just_clear=False, revoke=bool(revoke)
        )

    # ------------------------------------------------------------------
    # Forum topics
    # ------------------------------------------------------------------
    async def topics(self, chat_id: Any, limit: int = 50) -> list[dict[str, Any]]:
        """The topics of a forum, newest activity first."""
        from telethon.tl.functions.messages import GetForumTopicsRequest

        peer = await self._input_peer(chat_id)
        result = await self._call(
            GetForumTopicsRequest,
            peer=peer,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=max(1, min(int(limit), 100)),
        )
        rows: list[dict[str, Any]] = []
        for topic in getattr(result, "topics", None) or []:
            title = getattr(topic, "title", None)
            if not title:
                continue  # the "General" topic carries no title of its own
            rows.append(
                {
                    "id": getattr(topic, "id", None),
                    "title": str(title),
                    "closed": bool(getattr(topic, "closed", False)),
                    "pinned": bool(getattr(topic, "pinned", False)),
                    "unread": int(getattr(topic, "unread_count", 0) or 0),
                }
            )
        return rows

    async def create_topic(self, chat_id: Any, title: str) -> dict[str, Any]:
        from telethon.tl.functions.messages import CreateForumTopicRequest

        peer = await self._input_peer(chat_id)
        result = await self._call(
            CreateForumTopicRequest,
            peer=peer,
            title=str(title)[:119],
            random_id=int.from_bytes(__import__("os").urandom(8), "big", signed=True),
        )
        # Telegram answers with updates; the service message that created the
        # topic carries its id. Anything else is not guessed at.
        topic_id = None
        for update in getattr(result, "updates", None) or []:
            message = getattr(update, "message", None)
            action = getattr(message, "action", None)
            if action is not None and type(action).__name__ == "MessageActionTopicCreate":
                topic_id = getattr(message, "id", None)
                break
        return {"title": str(title), "id": topic_id}

    async def edit_topic(
        self, chat_id: Any, topic_id: int, *, title: str | None = None, closed: bool | None = None
    ) -> None:
        from telethon.tl.functions.messages import EditForumTopicRequest

        peer = await self._input_peer(chat_id)
        await self._call(
            EditForumTopicRequest, peer=peer, topic_id=int(topic_id), title=title, closed=closed
        )

    async def pin_topic(self, chat_id: Any, topic_id: int, *, pinned: bool = True) -> None:
        from telethon.tl.functions.messages import UpdatePinnedForumTopicRequest

        peer = await self._input_peer(chat_id)
        await self._call(
            UpdatePinnedForumTopicRequest, peer=peer, topic_id=int(topic_id), pinned=bool(pinned)
        )
