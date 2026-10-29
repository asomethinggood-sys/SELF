"""The `.settings` menu: a card, digits, and state that actually changes.

Every test here goes through the same path Telegram does — the router
(`handlers.on_outgoing`), which is what decides that a bare digit belongs to the
open menu rather than to chat.
"""

from __future__ import annotations

from tests.conftest import FakeEvent, FakeMessage, invoke


async def _reply(runtime, text: str, *, chat_id: int = 100) -> list[str]:
    """Send ``text`` as the owner, the way the router sees it."""
    client = runtime.client
    before = len(client.sent)
    message = FakeMessage(id=700, chat_id=chat_id, sender_id=42, text=text, out=True)
    event = FakeEvent(message, chat_id=chat_id, out=True)
    assert runtime.handlers._bare_command(text) is None, (
        "the settings reply must not be read as a bare command"
    )
    await runtime.handlers.on_outgoing(event)
    return [sent.text for sent in client.sent[before:]]


async def test_settings_renders_a_numbered_card(runtime) -> None:
    sent = await invoke(runtime, ".settings")
    body = sent[0].text
    assert "<b>⚙</b>" in body
    for number in ("1", "2", "3", "4"):
        assert f"<code>{number}</code>" in body
    assert "<blockquote>" in body
    assert "0 back" in body and "x close" in body


async def test_number_opens_the_item_and_shows_its_value(runtime) -> None:
    await invoke(runtime, ".settings")
    replies = await _reply(runtime, "4")
    assert len(replies) == 1
    body = replies[0]
    assert "Dry run" in body
    assert "Now: off" in body
    assert "<code>1</code> turn on" in body


async def test_dry_run_can_be_turned_on_and_off_from_the_menu(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "4")
    replies = await _reply(runtime, "1")
    assert await runtime.permissions.dry_run_active() is True
    assert any("Dry run is on" in text for text in replies)

    await _reply(runtime, "4")
    await _reply(runtime, "2")
    assert await runtime.permissions.dry_run_active() is False


async def test_prefix_is_typed_and_applied(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "3")
    replies = await _reply(runtime, "!")
    assert runtime.prefix == "!"
    assert any("Saved" in text for text in replies)
    # The root card comes back with the new prefix already in the footer.
    assert any("Settings" in text for text in replies)


async def test_prefix_rejects_a_bad_value_without_closing(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "3")
    replies = await _reply(runtime, "toolongprefix")
    assert runtime.prefix == "."
    assert any("prefix" in text.lower() for text in replies)


async def test_quiet_hours_accepts_a_range(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "5")
    replies = await _reply(runtime, "23:00-07:00")
    config = await runtime.permissions.quiet_hours_config(None)
    assert config["start"] == "23:00" and config["end"] == "07:00"
    assert any("23:00" in text for text in replies)


async def test_quiet_hours_can_be_switched_off(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "5")
    await _reply(runtime, "22:00-06:00")
    await _reply(runtime, "5")
    await _reply(runtime, "off")
    assert await runtime.permissions.quiet_hours_config(None) is None


async def test_quiet_hours_rejects_garbage(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "5")
    replies = await _reply(runtime, "whenever")
    assert any("23:00-07:00" in text for text in replies)
    assert await runtime.permissions.quiet_hours_config(None) is None


async def test_features_toggle_from_the_menu(runtime) -> None:
    await runtime.feature_manager.set_enabled("poetry", False)
    await invoke(runtime, ".settings")
    await _reply(runtime, "7")
    await _reply(runtime, "1")
    assert await runtime.store.feature_enabled("poetry") is True


async def test_back_and_close(runtime) -> None:
    await invoke(runtime, ".settings")
    await _reply(runtime, "4")
    back = await _reply(runtime, "0")
    assert "Settings" in back[0]
    closed = await _reply(runtime, "x")
    assert closed[0] == "Menu closed."
    assert await runtime.store.get_interactive(42) is None


async def test_bad_number_keeps_the_menu_open(runtime) -> None:
    await invoke(runtime, ".settings")
    replies = await _reply(runtime, "4")
    assert "Dry run" in replies[0]
    replies = await _reply(runtime, "9")
    assert "not one of the numbers" in replies[0]
    assert await runtime.store.get_interactive(42) is not None


async def test_a_normal_message_is_not_swallowed_by_an_open_menu(runtime) -> None:
    await invoke(runtime, ".settings")
    replies = await _reply(runtime, "hello there, just chatting")
    assert replies == [], "chat text must pass through, not be answered as a menu"
    assert await runtime.store.get_interactive(42) is not None


async def test_a_command_still_runs_while_the_menu_is_open(runtime) -> None:
    await invoke(runtime, ".settings")
    sent = await invoke(runtime, ".status")
    assert "uptime" in sent[0].text.lower() or "<blockquote>" in sent[0].text


async def test_menu_expires_on_its_own(runtime) -> None:
    await invoke(runtime, ".settings")
    session = await runtime.store.get_interactive(42)
    assert session is not None
    # Ten minutes later the row is stale and get_interactive clears it.
    assert await runtime.store.get_interactive(42, max_age_seconds=0) is None


async def test_the_card_is_localized(runtime) -> None:
    sent = await invoke(runtime, ".settings", language="fa")
    body = sent[0].text
    assert "تنظیمات" in body
    assert "بازگشت" in body


async def test_persian_alias_opens_the_menu(runtime) -> None:
    command = runtime.registry.resolve("تنظیمات")
    assert command is not None and command.name == "settings"
    sent = await invoke(runtime, ".تنظیمات")
    assert "Settings" in sent[0].text


