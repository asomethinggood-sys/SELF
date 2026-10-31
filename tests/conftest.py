"""Shared fixtures: a fully wired HAVOC runtime with fake Telegram objects.

No network, no Telethon client, no real credentials. The fakes implement only
the handful of attributes HAVOC reads, which is exactly the surface the
handlers are supposed to use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from havoc.config import Config
from havoc.event_engine import EventType
from havoc.runtime import Runtime


# ---------------------------------------------------------------------------
# Fake Telegram model
# ---------------------------------------------------------------------------
class FakeUser:
    def __init__(self, user_id: int = 42, first_name: str = "Owner", last_name: str = "", username: str | None = "owner") -> None:
        self.id = user_id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.bot = False
        self.phone = "+100000000"


class FakeChat:
    def __init__(self, chat_id: int = 100, title: str = "Test Chat", username: str | None = None, chat_type: str = "group") -> None:
        self.id = chat_id
        self.title = title
        self.username = username
        self.chat_type = chat_type


class FakeDocument:
    def __init__(self, size: int = 1024, file_name: str = "file.bin", duration: int | None = None) -> None:
        self.size = size
        self.attributes = [_DocAttribute(file_name=file_name, duration=duration)]


class _DocAttribute:
    def __init__(
        self,
        file_name: str | None = None,
        duration: int | None = None,
        w: int | None = None,
        h: int | None = None,
    ) -> None:
        self.file_name = file_name
        self.duration = duration
        self.w = w
        self.h = h


class FakeMedia:
    """Mimics ``MessageMediaDocument`` / ``MessageMediaPhoto`` containers."""

    def __init__(self, document: FakeDocument | None = None, photo: Any = None) -> None:
        self.document = document
        self.photo = photo
        self.ttl_seconds = None


class FakeMessage:
    def __init__(
        self,
        *,
        id: int = 1,
        chat_id: int = 100,
        sender_id: int = 7,
        text: str = "",
        reply: FakeMessage | None = None,
        out: bool = False,
        is_private: bool = False,
        is_group: bool = True,
        is_channel: bool = False,
        media: Any = None,
        sender: Any = None,
        date: datetime | None = None,
        edit_date: datetime | None = None,
        mentioned: bool = False,
    ) -> None:
        self.id = id
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = text
        self.message = text
        self.text = text
        self.reply_to_msg_id = getattr(reply, "id", None)
        self.out = out
        self.is_private = is_private
        self.is_group = is_group
        self.is_channel = is_channel
        self.media = media if media is None or isinstance(media, FakeMedia) else FakeMedia(document=media)
        self.document = getattr(self.media, "document", None) if self.media is not None else None
        self.sender = sender
        self.date = date or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.edit_date = edit_date
        self.mentioned = mentioned
        self._reply = reply
        self.fwd_from = None

    async def get_reply_message(self) -> FakeMessage | None:
        return self._reply


class FakeEvent:
    def __init__(
        self,
        message: FakeMessage,
        *,
        chat: Any = None,
        chat_id: int | None = None,
        out: bool | None = None,
    ) -> None:
        self.message = message
        self.chat = chat if chat is not None else FakeChat(chat_id or message.chat_id)
        self.chat_id = chat_id if chat_id is not None else message.chat_id
        self.id = message.id
        self.sender_id = message.sender_id
        self.out = message.out if out is None else out
        self.text = message.text


@dataclass
class SentMessage:
    chat_id: Any
    text: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    message_id: int = 0


class FakeClient:
    """Records outgoing messages and answers the few read calls HAVOC uses."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.edits: list[tuple[Any, Any, str]] = []
        self.deleted: list[tuple[Any, Any]] = []
        self.forwarded: list[Any] = []
        self.pinned: list[tuple[Any, Any, bool]] = []
        self.unpinned: list[tuple[Any, Any]] = []
        self.raw_requests: list[Any] = []
        self.uploads: list[Any] = []
        #: Canned answers for raw requests, keyed by request class name. A test
        #: that needs Telegram to *return* something fills this in; anything not
        #: listed comes back as ``None``, which is what a bare fake should say.
        self.responses: dict[str, Any] = {}
        self.kicked: list[tuple[Any, Any]] = []
        self.permissions: list[tuple[Any, Any, dict[str, Any]]] = []
        self.admins: list[tuple[Any, Any, dict[str, Any]]] = []
        self.left: list[Any] = []
        self.input_peers: list[Any] = []
        self.dialogs: list[Any] = []
        self.history: list[FakeMessage] = []
        #: Every reaction the bot asked for, so a test can assert it reached the
        #: wire — the whole bug was that it never did.
        self.reactions: list[tuple[Any, int, str]] = []
        self._next_id = 1000
        self.fail_with: Exception | None = None

    async def send_reaction(self, chat_id: Any, message_id: int, emoji: str) -> bool:
        self.reactions.append((chat_id, int(message_id), emoji))
        return True

    async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> FakeMessage:
        if self.fail_with is not None:
            raise self.fail_with
        self._next_id += 1
        self.sent.append(SentMessage(chat_id, text, kwargs, message_id=self._next_id))
        return FakeMessage(id=self._next_id, chat_id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else 0, text=text, out=True, sender_id=42)

    async def send_file(self, chat_id: Any, file: Any, **kwargs: Any) -> FakeMessage:
        self._next_id += 1
        self.sent.append(
            SentMessage(
                chat_id,
                kwargs.get("caption") or "",
                {**kwargs, "file": file},
                message_id=self._next_id,
            )
        )
        return FakeMessage(id=self._next_id, text=kwargs.get("caption", ""), out=True)

    async def edit_message(self, entity: Any, message: Any, text: str, **kwargs: Any) -> Any:
        """Edit in place — and update the record, because Telegram would.

        Yura types "…" and then fills it in, so a fake that kept both would make
        every "did she answer?" assertion read the placeholder.
        """
        self.edits.append((entity, message, text))
        target = message if isinstance(message, int) else getattr(message, "id", None)
        for sent in reversed(self.sent):
            if sent.message_id == target:
                sent.text = text
                sent.kwargs = {**sent.kwargs, **kwargs}
                break
        return FakeMessage(id=int(target or 0), chat_id=int(entity) if str(entity).lstrip("-").isdigit() else 0, text=text, out=True)

    async def delete_messages(self, entity: Any, ids: Any) -> None:
        self.deleted.append((entity, ids))

    async def forward_messages(self, to_chat: Any, message: Any, from_chat: Any) -> Any:
        self.forwarded.append((to_chat, message, from_chat))
        return message

    async def pin_message(self, entity: Any, message: Any, notify: bool = False) -> None:
        self.pinned.append((entity, message, notify))

    async def unpin_message(self, entity: Any, message: Any = None) -> None:
        self.unpinned.append((entity, message))

    async def get_messages(self, entity: Any = None, **kwargs: Any) -> list[FakeMessage]:
        if kwargs.get("ids") is not None:
            return []
        limit = kwargs.get("limit")
        return list(self.history[: int(limit)] if limit else self.history)

    def iter_messages(self, entity: Any = None, **kwargs: Any) -> Any:
        async def generator():
            for message in self.history:
                yield message

        return generator()

    async def get_dialogs(self, limit: int = 100) -> list[Any]:
        return self.dialogs

    async def get_entity(self, value: Any) -> Any:
        return FakeChat(chat_id=int(value) if str(value).lstrip("-").isdigit() else 999, title=str(value))

    async def get_me(self) -> FakeUser:
        return FakeUser()

    async def download_media(self, message: Any, file: Any = None, **kwargs: Any) -> str:
        Path(str(file)).write_bytes(b"fake-bytes")
        return str(file)

    async def upload_file(self, file: Any, **kwargs: Any) -> Any:
        self.uploads.append(file)
        return FakeUpload(str(file))

    async def get_input_entity(self, value: Any) -> Any:
        self.input_peers.append(value)
        return FakeInput(value)

    async def kick_participant(self, chat: Any, user: Any, **kwargs: Any) -> None:
        self.kicked.append((chat, user))

    async def edit_permissions(self, chat: Any, user: Any = None, **kwargs: Any) -> None:
        self.permissions.append((chat, user, kwargs))

    async def edit_admin(self, chat: Any, user: Any, **kwargs: Any) -> None:
        self.admins.append((chat, user, kwargs))

    async def delete_dialog(self, chat: Any) -> None:
        self.left.append(chat)

    async def __call__(self, request: Any) -> Any:
        self.raw_requests.append(request)
        return self.responses.get(type(request).__name__)

    @property
    def is_connected(self) -> bool:
        return True


class FakeInput:
    """Stands in for an ``InputPeer`` — enough for a request to be built."""

    def __init__(self, value: Any) -> None:
        self.value = value


class FakeUpload:
    """Stands in for Telethon's ``InputFile``."""

    def __init__(self, name: str = "file") -> None:
        self.name = name


class FakeTelegram:
    """Stands in for :class:`havoc.client.TelegramService`."""

    def __init__(self, me: FakeUser | None = None) -> None:
        self.me = me or FakeUser()
        self.connected = True
        self.client = FakeClient()

    async def get_reply_message(self, message: Any) -> Any:
        return await message.get_reply_message()

    async def resolve_entity(self, value: Any) -> Any:
        return await self.client.get_entity(value)

    async def get_messages(self, entity: Any, **kwargs: Any) -> Any:
        return await self.client.get_messages(entity, **kwargs)

    async def download_media(self, message: Any, file: Any = None, **kwargs: Any) -> str:
        return await self.client.download_media(message, file, **kwargs)

    async def disconnect(self) -> None:
        self.connected = False


# ---------------------------------------------------------------------------
# Config / runtime fixtures
# ---------------------------------------------------------------------------
def make_config(tmp_path: Path, **overrides: Any) -> Config:
    defaults = dict(
        api_id=123456,
        api_hash="0123456789abcdef0123456789abcdef",
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "test.db",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
        session_path=tmp_path / "data" / "session",
        plugin_dir=tmp_path / "plugins",
        default_language="en",
        timezone="UTC",
        command_prefix=".",
        outbound_rate_limit=1000,
        owner_ids=(42,),
        # Never read the repository's real credentials from a test: a stale key
        # in .auth would make the suite depend on the network.
        ai_keys_path=str(tmp_path / "no-keys-here.json"),
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def quiet_logger() -> logging.Logger:
    logger = logging.getLogger("havoc.test")
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
async def runtime(config: Config, quiet_logger: logging.Logger) -> Runtime:
    rt = await Runtime.create(config, quiet_logger)
    rt.telegram = FakeTelegram()
    rt.client = rt.telegram.client
    rt.sender.client = rt.client
    rt.permissions.set_self_id(42)
    rt.health.mark_connected()
    rt.load_features()
    # Fun-family switches default to off; the fixture turns them on so command
    # tests exercise behaviour instead of the feature gate. Tests about the
    # gate itself flip them back explicitly.
    for name in ("poetry", "roasts"):
        await rt.feature_manager.set_enabled(name, True)
    # The persona is on by default (it only speaks in the owner's own chat);
    # tests that exercise it turn it back on explicitly.
    await rt.feature_manager.set_enabled("persona", False)
    await rt.feature_manager.refresh()
    yield rt
    await rt.db.close()


# ---------------------------------------------------------------------------
# Helpers used by tests
# ---------------------------------------------------------------------------
from havoc.utils.parsing import parse_command  # noqa: E402


async def invoke(
    runtime: Runtime,
    text: str,
    *,
    reply: FakeMessage | None = None,
    chat_id: int = 100,
    sender_id: int = 42,
    language: str | None = None,
    is_private: bool = False,
    out: bool = True,
    client: FakeClient | None = None,
) -> list[SentMessage]:
    """Run a command exactly like the router would, returning sent messages."""
    fake_client = client or runtime.client
    before = len(fake_client.sent)
    message = FakeMessage(id=500, chat_id=chat_id, sender_id=sender_id, text=text, out=out, reply=reply, is_private=is_private, is_group=not is_private)
    event = FakeEvent(message, chat_id=chat_id, out=out)
    parsed = parse_command(text, runtime.prefix)
    assert parsed is not None, f"not a command: {text}"
    if language is None:
        language = await runtime.resolve_language(text, chat_id=chat_id, sender_id=sender_id)
    await runtime.dispatcher.handle(event, message, parsed, language)
    return fake_client.sent[before:]


async def incoming(
    runtime: Runtime,
    message: FakeMessage,
    *,
    event_type: EventType | None = None,
) -> Any:
    """Push a message through the event engine the way the router does."""
    from havoc.event_engine import normalize_message

    normalized = normalize_message(
        message,
        self_id=runtime.permissions.self_id,
        prefix=runtime.prefix,
        event_type=event_type or EventType.MESSAGE,
    )
    await runtime.event_engine.emit(normalized)
    return normalized


def last_text(runtime: Runtime) -> str:
    assert runtime.client.sent, "nothing was sent"
    return runtime.client.sent[-1].text


def sent_text(runtime: Runtime) -> str:
    """Everything the bot sent so far, joined — long replies are split."""
    return "\n".join(message.text for message in runtime.client.sent)
