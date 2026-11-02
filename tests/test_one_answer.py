"""One trigger, one answer — and nothing that reads like a database row.

Two bugs kept coming back in different clothes, so they are pinned here as
observable behaviour rather than as internal calls:

* **two answers to one message.** In Saved Messages both the self-chat handler
  and the DM handler were subscribed to his outgoing messages, so one sentence
  produced two cards and two sentences, interleaved. That is the "she repeats
  herself" complaint, and it is a routing bug, not a model bug.
* **raw storage formats in a sentence.** A weekly reminder said "and again every
  weekly:4"; a card whose translated title already carried a glyph printed it
  twice next to the icon that was added for it.
"""

from __future__ import annotations

import html

from conftest import FakeMessage, invoke, sent_text

from havoc import persona, ui
from havoc.event_engine import EventType, normalize_message
from havoc.utils.time import humanize_recurrence

OWNER = 42


async def _own_message(runtime, text: str, *, message_id: int = 700) -> None:
    runtime.permissions.set_self_id(OWNER)
    # Saved Messages is a private chat with nobody: not a group, which the fake
    # defaults to — and a fake that lies about the chat shape tests nothing.
    message = FakeMessage(
        id=message_id,
        chat_id=OWNER,
        sender_id=OWNER,
        text=text,
        out=True,
        is_private=True,
        is_group=False,
    )
    await runtime.event_engine.emit(
        normalize_message(message, self_id=OWNER, prefix=".", event_type=EventType.OUTGOING)
    )


async def test_his_own_chat_answers_exactly_once(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    before = len(runtime.client.sent)

    await _own_message(runtime, "hello there")

    sent = runtime.client.sent[before:]
    assert len(sent) == 1, f"one message produced {len(sent)} answers: {[m.text[:40] for m in sent]}"


async def test_an_instruction_in_his_own_chat_runs_once(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    before = len(runtime.client.sent)

    await _own_message(runtime, "how many messages have i sent")

    sent = runtime.client.sent[before:]
    assert len(sent) == 1, [m.text[:120] for m in sent]
    text = html.unescape(sent[0].text)
    # The receipt appears once. It used to appear twice, because the whole answer
    # was produced twice. Counted by its *body*, not by a glyph: the drawn rule
    # rotates through twelve marks, so `text.count("◈")` was measuring which mark
    # happened to be spent this time and went red whenever the pool reached ◈.
    assert text.count("Nothing has left this account yet.") == 1
    assert text.count("asked at") == 1


def test_a_weekly_spec_reads_as_a_weekday() -> None:
    assert humanize_recurrence("weekly:4", "en") == "Friday"
    assert humanize_recurrence("weekly:4", "fa") == "هر جمعه"
    assert humanize_recurrence("daily", "en") == "day"
    assert humanize_recurrence("interval:3600", "en") == "1 hour"
    assert humanize_recurrence("interval:7200", "en") == "2 hours"
    assert humanize_recurrence(None, "en") == ""
    # Nothing may leak the storage format itself.
    for spec in ("weekly:0", "monthly", "weekdays", "interval:1800"):
        assert ":" not in humanize_recurrence(spec, "en")


async def test_a_weekly_reminder_never_shows_the_raw_spec(runtime) -> None:
    await invoke(runtime, ".remind 18:00 stretch --repeat fri")
    text = html.unescape(sent_text(runtime))
    assert "again every Friday" in text
    assert "weekly:" not in text


def test_a_card_does_not_print_its_icon_twice() -> None:
    card = ui.card("⟡ outbox", ["0 recorded"], icon="⟡")
    first_line = card.splitlines()[0]
    assert first_line.count("⟡") == 1, first_line
    assert "outbox" in first_line

    # An icon and a title that do not clash are left exactly as written.
    plain = ui.card("Reminders", ["none"], icon="◷")
    assert plain.splitlines()[0].count("◷") == 1


def test_the_two_voices_are_still_one_creature() -> None:
    """A sanity rail: the mode switch must not have removed the old default."""
    assert persona.DEFAULT_MODE == "egirl"
    assert "e-kitten" in persona.system_prompt("en")
