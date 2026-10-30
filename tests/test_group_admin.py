"""Running a group: title, description, slow mode, and who stays.

Telegram is the authority on all of this, so the tests check two things: that the
right call reaches the client with the right arguments, and that a refusal comes
back as a sentence instead of a traceback. ``.promote`` is checked exactly — the
rights granted must be the ones he named and nothing else.
"""

from __future__ import annotations

import pytest
from conftest import invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


async def _raise_target(value):
    from havoc.services.reach import Target

    return Target(id=880, kind="user", title=str(value).lstrip("@"))


@pytest.fixture
def person(runtime):
    runtime.reach.resolve_or_raise = _raise_target
    return runtime


async def test_settitle_renames_the_chat(runtime) -> None:
    sent = await invoke(runtime, ".settitle Night Shift", chat_id=GROUP)
    assert "Night Shift" in _said(sent)
    request = runtime.client.raw_requests[-1]
    assert type(request).__name__ == "EditTitleRequest"
    assert request.title == "Night Shift"


async def test_settitle_without_text_asks_for_it(runtime) -> None:
    sent = await invoke(runtime, ".settitle", chat_id=GROUP)
    assert ".settitle" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_settitle_is_cut_to_telegrams_limit(runtime) -> None:
    await invoke(runtime, ".settitle " + "x" * 300, chat_id=GROUP)
    assert len(runtime.client.raw_requests[-1].title) == 128


async def test_setdesc_writes_the_description(runtime) -> None:
    sent = await invoke(runtime, ".setdesc quiet after midnight", chat_id=GROUP)
    assert "description changed" in _said(sent).lower()
    request = runtime.client.raw_requests[-1]
    # Telethon 1.4x uses the messages request for both groups and channels.
    assert type(request).__name__ == "EditChatAboutRequest"
    assert request.about == "quiet after midnight"


async def test_setdesc_refusal_is_a_sentence(runtime) -> None:
    """If the account may not change it, Telegram's reason is the answer."""

    async def refuse_about(self, request):
        self.raw_requests.append(request)
        raise RuntimeError("CHAT_ADMIN_REQUIRED")

    # Special methods are looked up on the type, so the fake is patched there.
    original = type(runtime.client).__call__
    type(runtime.client).__call__ = refuse_about
    try:
        sent = await invoke(runtime, ".setdesc hello there", chat_id=GROUP)
    finally:
        type(runtime.client).__call__ = original
    assert "CHAT_ADMIN_REQUIRED" in _said(sent)


async def test_slowmode_takes_a_human_duration(runtime) -> None:
    sent = await invoke(runtime, ".slowmode 5m", chat_id=GROUP)
    assert "300" in _said(sent)
    request = runtime.client.raw_requests[-1]
    assert type(request).__name__ == "ToggleSlowModeRequest"
    assert request.seconds == 300


async def test_slowmode_off_is_zero(runtime) -> None:
    sent = await invoke(runtime, ".slowmode off", chat_id=GROUP)
    assert "off" in _said(sent).lower()
    assert runtime.client.raw_requests[-1].seconds == 0


async def test_slowmode_rejects_nonsense(runtime) -> None:
    sent = await invoke(runtime, ".slowmode soon", chat_id=GROUP)
    assert "30s" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_slowmode_is_capped_at_a_day(runtime) -> None:
    await invoke(runtime, ".slowmode 99h", chat_id=GROUP)
    assert runtime.client.raw_requests[-1].seconds == 86400


async def test_kick_asks_first_then_removes(person) -> None:
    first = await invoke(person, ".kick @loud", chat_id=GROUP)
    assert "confirm" in _said(first).lower()
    assert person.client.kicked == []
    sent = await invoke(person, ".kick --yes @loud", chat_id=GROUP)
    assert "out" in _said(sent).lower()
    assert person.client.kicked[-1] == (GROUP, 880)


async def test_ban_lifts_the_right_to_be_seen(person) -> None:
    await invoke(person, ".ban --yes @loud", chat_id=GROUP)
    chat, who, kwargs = person.client.permissions[-1]
    assert (chat, who) == (GROUP, 880)
    assert kwargs.get("view_messages") is False


async def test_unban_gives_the_right_back(person) -> None:
    await invoke(person, ".unban --yes @loud", chat_id=GROUP)
    assert person.client.permissions[-1][2].get("view_messages") is True


async def test_promote_grants_only_the_named_rights(person) -> None:
    sent = await invoke(person, ".promote --yes @sara --delete --pin", chat_id=GROUP)
    assert "sara" in _said(sent).lower()
    chat, who, kwargs = person.client.admins[-1]
    assert (chat, who) == (GROUP, 880)
    assert kwargs.get("is_admin") is True
    assert kwargs.get("delete_messages") is True
    assert kwargs.get("pin_messages") is True
    # Nothing he did not ask for is granted.
    assert "ban_users" not in kwargs
    assert "invite_users" not in kwargs


async def test_promote_without_rights_refuses(person) -> None:
    sent = await invoke(person, ".promote --yes @sara", chat_id=GROUP)
    assert "--delete" in _said(sent)
    assert person.client.admins == []


async def test_demote_takes_the_rights_away(person) -> None:
    await invoke(person, ".demote --yes @sara", chat_id=GROUP)
    kwargs = person.client.admins[-1][2]
    assert kwargs.get("is_admin") is False


async def test_leave_leaves_the_chat_named(runtime) -> None:
    sent = await invoke(runtime, ".leave --yes @noise", chat_id=GROUP)
    assert "left" in _said(sent).lower()
    assert runtime.client.left, "the dialog was never deleted"
    assert runtime.client.input_peers[-1] == "@noise"


async def test_kick_telegram_refusal_is_a_sentence(person) -> None:
    async def refuse(chat, user, **kwargs):
        raise RuntimeError("USER_ADMIN_INVALID")

    person.client.kick_participant = refuse  # type: ignore[assignment]
    sent = await invoke(person, ".kick --yes @loud", chat_id=GROUP)
    assert "USER_ADMIN_INVALID" in _said(sent)


async def test_leave_uses_the_current_chat_when_none_is_named(runtime) -> None:
    await invoke(runtime, ".leave --yes", chat_id=GROUP)
    assert runtime.client.input_peers[-1] == GROUP


@pytest.mark.parametrize(
    "line",
    [".settitle T", ".setdesc d", ".slowmode 30s", ".kick --yes @x", ".ban --yes @x", ".unban --yes @x"],
)
async def test_admin_commands_always_answer(person, line) -> None:
    sent = await invoke(person, line, chat_id=GROUP)
    assert _said(sent).strip()
