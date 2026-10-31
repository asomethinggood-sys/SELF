"""The abilities he described in his own words, run for real.

Every test here starts from the sentence he typed and ends at the Telegram client:
a reaction on the right message, a burst in the room he named, an auto-reply that
switches itself off after the four hours he asked for, and one message where he
used to get two. A passing test is the proof he asked for — he is not testing by
hand.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import invoke

from havoc.services import intent as intent_module
from havoc.services.actions import Turn
from havoc.services.plans import build_plan
from havoc.services.reach import MIN_GAP
from havoc.services.self_console import run as run_console
from havoc.services.tools import build_tools

OWNER = 42
GROUP = -1001234567890
FRIEND = 999


async def _reach_jobs(runtime) -> list:
    """The queued sends, straight from the jobs table the scheduler reads."""
    return await runtime.db.fetch_all(
        "SELECT * FROM jobs WHERE kind = 'reach_send' AND status = 'pending' ORDER BY run_at"
    )


def parse_iso(value: str):
    from datetime import datetime

    return datetime.fromisoformat(str(value))


def _tool(runtime, name: str):
    return {tool.name: tool for tool in build_tools(runtime)}[name]


async def _respond(runtime, line: str, *, chat_id: int = 100, reply_to: int | None = None):
    return await run_console(
        runtime,
        line,
        chat_id=chat_id,
        sender_id=OWNER,
        language="en",
        reply_to=reply_to,
    )


# ---------------------------------------------------------------------------
# Reactions, in plain language, on the message he pointed at
# ---------------------------------------------------------------------------
def test_the_persian_order_reads_as_a_reaction() -> None:
    found = intent_module.detect("به پیامش قلب واکنش بده")
    assert found is not None
    assert found.lines == ("emoji ❤️",)


def test_three_persian_shapes_are_one_instruction() -> None:
    for sentence in (
        "به این پیام ری‌اکشن بده ❤️",
        "بهش ری اکشن 🔥 بده",
        "به پیامش لایک واکنش بده",
    ):
        found = intent_module.detect(sentence)
        assert found is not None, sentence
        assert found.lines[0].startswith("emoji "), sentence


def test_a_question_about_reactions_is_not_an_order() -> None:
    assert intent_module.detect("چطوری یورا") is None
    assert intent_module.detect("امروز حالت چطوره") is None


async def test_the_console_reacts_to_the_message_behind_the_reply(runtime) -> None:
    """The bug: she had no message to point at, so nothing was reacted to."""
    await runtime.store.set_feature("reach", True)
    await runtime.feature_manager.refresh()
    before = len(runtime.client.reactions)
    reply = await _respond(runtime, "emoji ❤️", chat_id=GROUP, reply_to=600)
    assert reply.ok, reply.note
    assert len(runtime.client.reactions) > before
    chat_id, message_id, emoji = runtime.client.reactions[-1]
    assert chat_id == GROUP
    assert message_id == 600
    assert emoji == "❤️"


async def test_the_tool_reacts_to_the_pointed_message_not_his_own(runtime) -> None:
    turn = Turn(chat_id=GROUP, actor_id=OWNER, language="en", is_owner=True, reply_to=777)
    result = await _tool(runtime, "react").run(turn, {"emoji": "❤️"})
    assert result.ok, result.detail
    assert runtime.client.reactions[-1][:2] == (GROUP, 777)


async def test_the_tool_takes_a_spoken_emoji_name(runtime) -> None:
    turn = Turn(chat_id=GROUP, actor_id=OWNER, language="en", is_owner=True, reply_to=778)
    result = await _tool(runtime, "react").run(turn, {"emoji": "❤️ heart"})
    assert result.ok, result.detail
    assert runtime.client.reactions[-1] == (GROUP, 778, "❤️")


async def test_the_tool_refuses_when_there_is_nothing_to_react_to(runtime) -> None:
    turn = Turn(chat_id=GROUP, actor_id=OWNER, language="en", is_owner=True)
    result = await _tool(runtime, "react").run(turn, {"emoji": "❤️"})
    assert not result.ok


# ---------------------------------------------------------------------------
# The fortune, when he asks for it as an order
# ---------------------------------------------------------------------------
def test_fal_as_an_order_and_as_a_noun() -> None:
    for sentence in ("فال بگیر", "فال", "fal", "یورا فال بگیر", "فال حافظ بگیر"):
        found = intent_module.detect(sentence)
        assert found is not None, sentence
        assert found.lines == ("fal",), sentence


def test_poetry_as_an_order() -> None:
    for sentence in ("شعری بگو", "poem", "شعر"):
        found = intent_module.detect(sentence)
        assert found is not None, sentence
        assert found.lines == ("poem",), sentence


# ---------------------------------------------------------------------------
# "repeat 100 times" and "10 different messages in this group"
# ---------------------------------------------------------------------------
def test_repeat_after_a_said_line_targets_the_room_he_is_in() -> None:
    found = intent_module.detect("say I'm a good girl and repeat 100 times")
    assert found is not None
    assert found.lines == ("send --here I'm a good girl --repeat 100",)


def test_a_burst_becomes_one_send_with_a_count() -> None:
    found = intent_module.detect("send hi in 10 different messages in this group")
    assert found is not None
    assert found.lines == ("send --here hi --repeat 10",)


def test_a_named_recipient_survives_the_count() -> None:
    found = intent_module.detect("send @sara boo 3 times")
    assert found is not None
    assert found.lines == ("send @sara boo --repeat 3",)


async def test_here_sends_to_the_room_the_order_came_from(runtime) -> None:
    await runtime.store.set_feature("reach", True)
    await runtime.feature_manager.refresh()
    before = len(runtime.client.sent)
    reply = await _respond(runtime, "send --here hi --repeat 3", chat_id=GROUP)
    assert reply.ok, reply.note
    sent_to_here = [
        row for row in runtime.client.sent[before:] if int(row.chat_id) == GROUP and "hi" == row.text
    ]
    assert len(sent_to_here) == 1, "the first goes now, the rest are queued"
    assert len(await _reach_jobs(runtime)) >= 2


async def test_a_repeat_without_an_interval_is_paced(runtime) -> None:
    """Ten lines at the same instant is a flood; a small gap is not silence."""
    from havoc.services.reach import Target

    target = Target(id=GROUP, kind="group", title="the room")
    report = await runtime.reach.deliver(target, "hi", repeat=3)
    assert report.ok
    stamps = sorted(row["run_at"] for row in await _reach_jobs(runtime))
    assert len(stamps) == 2
    first, second = parse_iso(stamps[0]), parse_iso(stamps[1])
    # The job table stores whole seconds, so the check is "spaced, not stacked".
    assert second > first
    assert MIN_GAP >= 1.0


# ---------------------------------------------------------------------------
# "reply to every DM for 4 hours"
# ---------------------------------------------------------------------------
def test_an_auto_reply_sentence_reads_private_not_any() -> None:
    found = intent_module.detect("reply to every direct message i get with he is busy lil bro")
    assert found is not None
    assert found.lines[0] == "autoreply add --type any --private => he is busy lil bro"
    assert found.lines[1] == "autoreply on"


def test_four_hours_brings_the_switch_off_with_it() -> None:
    plan = build_plan(
        "reply to every direct message i get with he is busy lil bro for 4 hours",
        intent=intent_module,
    )
    actions = [step.action for step in plan.steps]
    assert actions[0] == "autoreply add --type any --private => he is busy lil bro"
    assert actions[-1] == "autoreply off"
    assert plan.steps[-1].at - plan.steps[0].at >= timedelta(hours=4)


# ---------------------------------------------------------------------------
# One message, not two
# ---------------------------------------------------------------------------
async def test_the_same_line_twice_is_sent_once(runtime) -> None:
    from havoc.services.reach import Target

    target = Target(id=GROUP, kind="group", title="the room")
    first = await runtime.reach.deliver(target, "on my way")
    second = await runtime.reach.deliver(target, "on my way")
    assert first.ok and second.ok
    assert second.duplicate, "the copy was not recognised as a copy"
    bodies = [row.text for row in runtime.client.sent if row.text == "on my way"]
    assert len(bodies) == 1


async def test_two_different_lines_both_arrive(runtime) -> None:
    from havoc.services.reach import Target

    target = Target(id=GROUP, kind="group", title="the room")
    await runtime.reach.deliver(target, "first")
    report = await runtime.reach.deliver(target, "second")
    assert report.ok and not report.duplicate


async def test_a_queued_repeat_is_not_swallowed_by_the_duplicate_guard(runtime) -> None:
    """The scheduled tail of a repeat is the same sentence again — on purpose."""
    from havoc.services.reach import Target

    target = Target(id=GROUP, kind="group", title="the room")
    await runtime.reach.deliver(target, "again", repeat=2)
    rows = await _reach_jobs(runtime)
    assert len(rows) == 1
    for row in rows:
        await runtime.reach.send_later(row)
    again = [row.text for row in runtime.client.sent if row.text == "again"]
    assert len(again) == 2


# ---------------------------------------------------------------------------
# Telling her to DM someone gives one message
# ---------------------------------------------------------------------------
async def test_saying_it_once_sends_it_once(runtime) -> None:
    await invoke(runtime, ".send @sara on my way", chat_id=GROUP)
    delivered = [
        row for row in runtime.client.sent if row.text == "on my way" and int(row.chat_id) != GROUP
    ]
    assert len(delivered) == 1
    assert not any(row.text == "on my way" for row in runtime.client.sent if int(row.chat_id) == GROUP)


async def test_the_send_tool_understands_this_group(runtime) -> None:
    """"send hi in 10 different messages in this group" — the model has no id."""
    turn = Turn(chat_id=GROUP, actor_id=OWNER, language="en", is_owner=True)
    result = await _tool(runtime, "send_message").run(
        turn, {"to": "this group", "text": "hi", "repeat": "10"}
    )
    assert result.ok, result.detail
    here = [row for row in runtime.client.sent if row.text == "hi" and int(row.chat_id) == GROUP]
    assert len(here) == 1, "one now, the other nine queued"
    assert len(await _reach_jobs(runtime)) == 9


def test_a_sticker_request_in_plain_words() -> None:
    assert intent_module.detect("send me a sticker with a cat") .lines == ("sticker cat",)
    assert intent_module.detect("یورا استیکر قلب بفرست").lines == ("sticker قلب",)


def test_a_sticker_with_no_subject_is_not_an_order() -> None:
    assert intent_module.detect("sticker") is None
    assert intent_module.detect("I like stickers") is None


def test_a_word_is_never_mistaken_for_a_reaction() -> None:
    from havoc.utils.text import emoji_in

    assert emoji_in("react with ❤️ please") == "❤️"
    assert emoji_in("قلب") == ""
    assert emoji_in("你好") == ""
    assert emoji_in("1️⃣") == "1️⃣"
