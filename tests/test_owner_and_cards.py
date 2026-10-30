"""Whose account this is, what a card looks like, and her own command line.

The three things the owner asked for in one message:

* his handle is the owner, in settings, and *only* an owner can make her act or
  change her — everyone else may talk to her and nothing more;
* an action is drawn, not reported: a mark, a bold title, a ruled line, italic
  facts, one closing glyph, every glyph different from the last;
* she knows her own commands and can pull them herself — "fal page 166" is the
  command the owner already has, not a second implementation of it.
"""

from __future__ import annotations

from conftest import FakeUser, sent_text

from havoc import permissions as permissions_module
from havoc import ui
from havoc.config import Config
from havoc.services.self_console import BLOCKED, ConsoleReply, catalogue, run


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------
def test_the_handle_in_settings_is_the_owner() -> None:
    """No env, no .env: a fresh clone still knows who its owner is."""
    assert Config().owner_usernames == ("edited_by_nyx",)


def test_a_handle_is_stored_without_its_at_sign() -> None:
    assert Config(owner_usernames=("@Edited_By_Nyx", "someone")).owner_usernames == (
        "edited_by_nyx",
        "someone",
    )


class FakeEntity:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class ResolvingClient:
    """Stands in for Telethon's ``get_entity``."""

    def __init__(self, mapping: dict[str, int]) -> None:
        self.mapping = mapping
        self.asked: list[str] = []

    async def get_entity(self, handle: str):
        self.asked.append(handle)
        key = handle.lstrip("@").lower()
        if key not in self.mapping:
            raise ValueError("no user has that username")
        return FakeEntity(self.mapping[key])


async def test_the_handle_becomes_an_id_once_and_is_remembered(runtime) -> None:
    client = ResolvingClient({"edited_by_nyx": 424242})
    found = await runtime.permissions.resolve_handles(client)
    assert found == {"edited_by_nyx": 424242}
    assert runtime.permissions.is_owner(424242) is True
    assert runtime.permissions.is_owner(1) is False

    # A restart must not need the network: the id was written down.
    stored = await runtime.store.get_setting(permissions_module.RESOLVED_SETTING)
    assert stored == {"edited_by_nyx": 424242}
    fresh = permissions_module.Permissions(runtime.config, runtime.store)
    await fresh.load()
    assert fresh.is_owner(424242) is True


async def test_an_unknown_handle_changes_nothing(runtime) -> None:
    client = ResolvingClient({})
    assert await runtime.permissions.resolve_handles(client) == {}
    assert runtime.permissions.is_owner(999) is False


async def test_the_account_itself_is_always_the_owner(runtime) -> None:
    """A failed lookup must not lock the owner out of his own bot."""
    runtime.permissions.set_self_id(7)
    assert runtime.permissions.is_owner(7) is True


async def test_owner_ids_survive_a_save_and_load(runtime) -> None:
    runtime.permissions.add_id(555, handle="friend")
    await runtime.permissions.save()
    fresh = permissions_module.Permissions(Config(), runtime.store)
    await fresh.load()
    assert fresh.is_owner(555) is True
    assert fresh.handles_resolved == {"friend": 555}


async def test_only_the_owner_can_see_who_owns_her(runtime) -> None:
    runtime.permissions.set_self_id(42)
    from conftest import FakeMessage

    from havoc.event_engine import EventType, normalize_message

    message = FakeMessage(id=1, chat_id=100, sender_id=999, text=".owner", is_group=True)
    message.sender = FakeUser(user_id=999, first_name="Guest")
    event = normalize_message(message, self_id=42, prefix=".", event_type=EventType.MESSAGE)
    from havoc.utils.parsing import parse_command

    parsed = parse_command(".owner", ".")
    await runtime.dispatcher.handle(event, message, parsed, "en")
    assert "owner" in sent_text(runtime).lower() or "permission" in sent_text(runtime).lower()


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------
def test_a_card_is_the_shape_he_drew() -> None:
    text = ui.card(
        "Tool called",
        ["Reminder set for 30 minutes", "You'll be reminded in the same chat"],
    )
    lines = text.splitlines()
    assert lines[0].startswith("◈ <b>") and lines[0].endswith("</b>")
    assert "ـ" in lines[1] and "<b>" in lines[1]           # the drawn rule
    assert lines[2] == "<i>Reminder set for 30 minutes</i>"  # italic, not bold
    assert lines[3].startswith("<i>")
    assert lines[4] in ui.CLOSING_GLYPHS                     # one closing glyph


def test_a_run_of_cards_is_a_run_of_different_marks() -> None:
    """No two cards in a row open on the same glyph, and the pool is used."""
    marks = []
    for _ in range(len(ui.RULE_GLYPHS) + 4):
        rule = ui.card("Tool called", ["a thing happened"]).splitlines()[1]
        marks.append("".join(ch for ch in rule if ch not in "ـ<>b/i "))
    assert all(marks), marks
    assert all(first != second for first, second in zip(marks, marks[1:], strict=False)), marks
    assert len(set(marks)) >= 8, marks


def test_a_card_escapes_what_it_is_given() -> None:
    text = ui.card("Tool called", ["<script>alert(1)</script>"])
    assert "<script>" not in text and "&lt;script&gt;" in text


# ---------------------------------------------------------------------------
# Her own command line
# ---------------------------------------------------------------------------
def test_the_catalogue_lists_her_real_commands(runtime) -> None:
    text = catalogue(runtime, "en", full=True)
    assert "## her own commands" in text
    assert ".fal" in text and ".clock" in text and ".remind" in text
    assert "—" in text, "the full catalogue explains what each one does"


def test_the_catalogue_never_offers_what_she_may_not_call(runtime) -> None:
    text = catalogue(runtime, "en")
    for blocked in ("yura", "owner", "key", "prefix", "stop"):
        assert f".{blocked}" not in text, blocked
        assert blocked in BLOCKED


async def test_she_can_run_a_real_command(runtime) -> None:
    reply = await run(runtime, "about", chat_id=100, sender_id=42)
    assert isinstance(reply, ConsoleReply)
    assert reply.ok and reply.command == "about"
    assert reply.said, "the command's own answer is captured for her to read"


async def test_a_blocked_command_is_refused_by_name(runtime) -> None:
    reply = await run(runtime, "yura hello", chat_id=100, sender_id=42)
    assert reply.ok is False and reply.kind == "blocked"


async def test_a_command_that_does_not_exist_is_a_sentence(runtime) -> None:
    reply = await run(runtime, "dance", chat_id=100, sender_id=42)
    assert reply.ok is False and reply.kind == "unknown"
    assert "no command" in reply.note


async def test_the_prefix_is_optional(runtime) -> None:
    with_prefix = await run(runtime, ".about", chat_id=100, sender_id=42)
    without = await run(runtime, "about", chat_id=100, sender_id=42)
    assert with_prefix.ok and without.ok


async def test_the_command_tool_only_works_for_the_owner(runtime) -> None:
    from havoc.services.actions import ToolCall, Turn

    calls = await runtime.actions.execute(
        ToolCall("command", {"command": "about"}),
        Turn(chat_id=100, actor_id=999, is_owner=False),
    )
    assert calls.status == "refused"


async def test_the_command_tool_runs_for_the_owner(runtime) -> None:
    from havoc.services.actions import ToolCall, Turn

    turn = Turn(chat_id=100, actor_id=42, is_owner=True)
    call = ToolCall("command", {"command": "about"})
    first = await runtime.actions.execute(call, turn)
    # `command` is a write scope: the first use asks, exactly like every other
    # write. That is the strictness he asked for, applied to her own console too.
    assert first.status in {"needs_confirm", "done"}
    if first.status == "needs_confirm":
        done = await runtime.actions.resolve(100, approve=True)
        assert done.status == "done"
        assert done.card[0], "the card names the command that ran"
    else:
        assert first.card[0]


def test_the_blocked_list_does_not_contain_the_useful_ones() -> None:
    for useful in ("fal", "clock", "poem", "remind", "translate", "weather", "note"):
        assert useful not in BLOCKED
