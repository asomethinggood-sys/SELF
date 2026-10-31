"""``.peek`` and ``.clearhistory`` — the two requests Telegram only offers raw.

``.peek`` must never be a guess about a link: the hash is extracted, checked and
sent as ``CheckChatInviteRequest``. ``.clearhistory`` must be confirmed first, and
``--for-everyone`` must set ``revoke`` — the difference between clearing his copy
and deleting the other person's.
"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _last(runtime) -> object:
    return runtime.client.raw_requests[-1]


async def test_peek_reads_a_private_invite(runtime) -> None:
    runtime.client.responses["CheckChatInviteRequest"] = SimpleNamespace(
        title="Night Shift", participants_count=12
    )
    sent = await invoke(runtime, ".peek https://t.me/+AbCdEf123")
    text = _said(sent)
    assert "Night Shift" in text
    assert "12" in text
    request = _last(runtime)
    assert request.__class__.__name__ == "CheckChatInviteRequest"
    assert request.hash == "AbCdEf123"


class ChatInviteAlready:
    """Telegram's own shape for "you are in this one already"."""

    def __init__(self, chat) -> None:
        self.chat = chat


async def test_peek_says_when_he_is_already_in(runtime) -> None:
    runtime.client.responses["CheckChatInviteRequest"] = ChatInviteAlready(
        SimpleNamespace(id=1, title="Home", participants_count=3)
    )
    sent = await invoke(runtime, ".peek t.me/joinchat/AbC-d_1")
    assert "already" in _said(sent).lower()
    assert _last(runtime).hash == "AbC-d_1"


async def test_peek_refuses_something_that_is_not_a_link(runtime) -> None:
    sent = await invoke(runtime, ".peek hello")
    assert "invite link" in _said(sent).lower()
    assert runtime.client.raw_requests == []


async def test_peek_needs_a_link(runtime) -> None:
    sent = await invoke(runtime, ".peek")
    assert ".peek" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_clearhistory_asks_first(runtime) -> None:
    first = await invoke(runtime, ".clearhistory", chat_id=GROUP)
    assert "confirm" in _said(first).lower()
    assert runtime.client.raw_requests == []


async def test_clearhistory_clears_only_his_copy_by_default(runtime) -> None:
    await invoke(runtime, ".clearhistory --yes", chat_id=GROUP)
    request = _last(runtime)
    assert request.__class__.__name__ == "DeleteHistoryRequest"
    assert request.revoke is False
    assert request.just_clear is False


async def test_clearhistory_for_everyone_sets_revoke(runtime) -> None:
    sent = await invoke(runtime, ".clearhistory --yes --for-everyone", chat_id=GROUP)
    assert "both sides" in _said(sent).lower()
    assert _last(runtime).revoke is True


async def test_clearhistory_named_chat_resolves_the_peer(runtime) -> None:
    await invoke(runtime, ".clearhistory --yes 880")
    assert runtime.client.input_peers[-1] == 880


async def test_clearhistory_refusal_is_a_sentence(runtime) -> None:
    async def refuse(self, request):
        self.raw_requests.append(request)
        raise RuntimeError("CHAT_ADMIN_REQUIRED")

    original = type(runtime.client).__call__
    type(runtime.client).__call__ = refuse
    try:
        sent = await invoke(runtime, ".clearhistory --yes", chat_id=GROUP)
    finally:
        type(runtime.client).__call__ = original
    assert "CHAT_ADMIN_REQUIRED" in _said(sent)
