"""The new surface: message verbs, the inbox, groups, and her own health.

Each test answers the question "does the sentence he types actually reach
Telegram, and does the answer say what really happened" — not "does the function
exist". The fakes are the ones the rest of the suite uses, so a passing test here
means the same code path the live bot takes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeChat, FakeMessage, FakeUser, invoke

from havoc.services import intent as intent_module
from havoc.services.tg_extra import ExtrasError
from havoc.utils.time import iso, utc_now

OWNER = 42
FRIEND = 999
GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


class _Dialog:
    """The handful of attributes the inbox reads, exactly as Telethon gives them."""

    def __init__(self, entity, *, unread=0, date=None, is_user=True, is_group=False):
        self.entity = entity
        self.id = getattr(entity, "id", 0)
        self.unread_count = unread
        self.date = date or datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        self.is_user = is_user
        self.is_group = is_group
        self.is_channel = not is_user and not is_group


# ---------------------------------------------------------------------------
# Message verbs
# ---------------------------------------------------------------------------
async def test_reply_answers_the_message_he_pointed_at(runtime) -> None:
    target = FakeMessage(id=555, chat_id=GROUP, sender_id=FRIEND, text="when?")
    sent = await invoke(runtime, ".reply nine o'clock", chat_id=GROUP, reply=target)
    assert "answered" in _said(sent)
    bodies = [row for row in runtime.client.sent if "nine o'clock" in row.text]
    assert bodies, "the reply never went out"
    assert bodies[0].kwargs.get("reply_to") == 555


async def test_reply_without_a_reply_is_refused_in_words(runtime) -> None:
    sent = await invoke(runtime, ".reply hello", chat_id=GROUP)
    assert "Reply to a message first" in _said(sent)


async def test_forward_keeps_the_message_identity(runtime) -> None:
    target = FakeMessage(id=556, chat_id=GROUP, sender_id=FRIEND, text="the address")
    await invoke(runtime, ".forward 999", chat_id=GROUP, reply=target)
    assert runtime.client.forwarded, "nothing was forwarded"
    to_chat, message, from_chat = runtime.client.forwarded[-1]
    assert (message, from_chat) == (556, GROUP)
    rows = await runtime.store.outbox(5)
    assert rows and rows[0]["kind"] == "forward"


async def test_copy_sends_the_words_with_no_forward_header(runtime) -> None:
    target = FakeMessage(id=557, chat_id=GROUP, sender_id=FRIEND, text="bring the keys")
    await invoke(runtime, ".copy 999", chat_id=GROUP, reply=target)
    assert runtime.client.forwarded == [], "a copy must not forward"
    assert [row for row in runtime.client.sent if "bring the keys" in row.text]


async def test_edit_refuses_someone_elses_message(runtime) -> None:
    theirs = FakeMessage(id=558, chat_id=GROUP, sender_id=FRIEND, text="mine", out=False)
    sent = await invoke(runtime, ".edit new words", chat_id=GROUP, reply=theirs)
    assert "not mine to edit" in _said(sent)
    assert runtime.client.edits == []


async def test_edit_changes_her_own_message(runtime) -> None:
    ours = FakeMessage(id=559, chat_id=GROUP, sender_id=OWNER, text="old", out=True)
    sent = await invoke(runtime, ".edit nine pm", chat_id=GROUP, reply=ours)
    assert "changed" in _said(sent)
    assert runtime.client.edits and runtime.client.edits[-1][2] == "nine pm"


async def test_delete_asks_first_then_deletes(runtime) -> None:
    target = FakeMessage(id=560, chat_id=GROUP, sender_id=FRIEND, text="oops")
    first = await invoke(runtime, ".delete", chat_id=GROUP, reply=target)
    assert "confirm" in _said(first).lower()
    assert runtime.client.deleted == []
    await invoke(runtime, ".delete --yes", chat_id=GROUP, reply=target)
    assert runtime.client.deleted, "the confirmed delete did not happen"


async def test_pin_and_unpin_reach_the_client(runtime) -> None:
    target = FakeMessage(id=561, chat_id=GROUP, sender_id=FRIEND, text="rules")
    await invoke(runtime, ".pin", chat_id=GROUP, reply=target)
    assert runtime.client.pinned == [(GROUP, 561, True)], runtime.client.pinned
    await invoke(runtime, ".unpin", chat_id=GROUP, reply=target)
    assert runtime.client.unpinned == [(GROUP, 561)], runtime.client.unpinned


# ---------------------------------------------------------------------------
# The inbox
# ---------------------------------------------------------------------------
async def test_inbox_lists_people_not_their_words(runtime) -> None:
    runtime.client.dialogs = [
        _Dialog(FakeUser(user_id=FRIEND, first_name="Sara", username="sara"), unread=3),
        _Dialog(FakeChat(chat_id=GROUP, title="Sunday lunch", chat_type="group"), is_user=False, is_group=True),
    ]
    sent = await invoke(runtime, ".inbox", chat_id=OWNER)
    body = _said(sent)
    assert "Sara" in body and "999" in body
    assert "Sunday lunch" not in body, "a group is not a private chat"


async def test_unread_counts_only_what_is_waiting(runtime) -> None:
    runtime.client.dialogs = [
        _Dialog(FakeUser(user_id=11, first_name="Sara"), unread=2),
        _Dialog(FakeUser(user_id=12, first_name="Ali"), unread=0),
    ]
    body = _said(await invoke(runtime, ".unread", chat_id=OWNER))
    assert "Sara" in body and "Ali" not in body


async def test_reading_one_conversation_summarises_it(runtime) -> None:
    runtime.ai = _SummaryProvider("she asked about the flat and you said friday.")
    runtime.client.history = [
        FakeMessage(id=1, chat_id=FRIEND, sender_id=FRIEND, text="about the flat?", is_private=True),
        FakeMessage(id=2, chat_id=FRIEND, sender_id=OWNER, text="friday", out=True, is_private=True),
    ]
    body = _said(await invoke(runtime, ".read 999", chat_id=OWNER))
    assert "flat" in body
    assert runtime.ai.calls, "the summary never asked the model"


async def test_raw_reading_prints_the_lines(runtime) -> None:
    runtime.ai = _SummaryProvider("unused")
    runtime.client.history = [
        FakeMessage(id=1, chat_id=FRIEND, sender_id=FRIEND, text="see you friday", is_private=True),
    ]
    body = _said(await invoke(runtime, ".read 999 --raw", chat_id=OWNER))
    assert "see you friday" in body


async def test_whois_answers_from_the_book(runtime) -> None:
    await runtime.people.touch(FRIEND, "Sara")
    await runtime.people.note(FRIEND, "her birthday is 3 May")
    await runtime.people.set_dm(FRIEND, True)
    body = _said(await invoke(runtime, ".whois 999", chat_id=OWNER))
    assert "Sara" in body and "3 May" in body


# ---------------------------------------------------------------------------
# Groups, folders, sessions
# ---------------------------------------------------------------------------
async def test_groups_lists_only_groups(runtime, monkeypatch) -> None:
    async def fake_groups(limit=200):
        return [
            {"id": -100, "title": "Sunday lunch", "username": "", "members": 12, "date": None},
            {"id": -101, "title": "Work", "username": "work", "members": 40, "date": None},
        ]

    monkeypatch.setattr(runtime.tg, "groups", fake_groups)
    body = _said(await invoke(runtime, ".groups", chat_id=OWNER))
    assert "Sunday lunch" in body and "12 members" in body


async def test_a_refusal_from_telegram_is_a_sentence(runtime, monkeypatch) -> None:
    async def refuse(chat_id, limit=200):
        raise ExtrasError("CHAT_ADMIN_REQUIRED", kind="refused")

    monkeypatch.setattr(runtime.tg, "participants", refuse)
    body = _said(await invoke(runtime, ".members 999", chat_id=OWNER))
    assert "CHAT_ADMIN_REQUIRED" in body


async def test_invite_prints_the_link(runtime, monkeypatch) -> None:
    async def link(chat_id, *, create=True):
        return "https://t.me/+abc123"

    monkeypatch.setattr(runtime.tg, "invite_link", link)
    body = _said(await invoke(runtime, ".invite 999", chat_id=OWNER))
    assert "https://t.me/+abc123" in body


async def test_mute_takes_a_duration(runtime, monkeypatch) -> None:
    seen: list[tuple[int, int]] = []

    async def mute(chat_id, seconds):
        seen.append((chat_id, seconds))

    monkeypatch.setattr(runtime.tg, "mute", mute)
    await invoke(runtime, ".mute 999 2h", chat_id=OWNER)
    assert seen == [(999, 7200)]


async def test_sessions_are_numbered_and_endable(runtime, monkeypatch) -> None:
    async def sessions():
        return [
            {"hash": 111, "device": "Pixel", "platform": "Android", "country": "IR", "current": True},
            {"hash": 222, "device": "Laptop", "platform": "Linux", "country": "NO", "current": False},
        ]

    ended: list[int] = []

    async def terminate(session_hash):
        ended.append(session_hash)

    monkeypatch.setattr(runtime.tg, "sessions", sessions)
    monkeypatch.setattr(runtime.tg, "terminate_session", terminate)
    body = _said(await invoke(runtime, ".sessions", chat_id=OWNER))
    assert "Pixel" in body and "Laptop" in body
    await invoke(runtime, ".endsession 2 --yes", chat_id=OWNER)
    assert ended == [222]


# ---------------------------------------------------------------------------
# Her own health
# ---------------------------------------------------------------------------
async def test_diagnose_answers_with_the_live_process(runtime) -> None:
    body = _said(await invoke(runtime, ".diagnose", chat_id=OWNER))
    assert "telegram" in body and "database" in body and "scheduler" in body


async def test_repair_frees_a_job_a_dead_process_left_running(runtime) -> None:
    job_id = await runtime.scheduler.enqueue("reach_send", utc_now(), {"chat_id": 1, "text": "x"})
    await runtime.db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    await runtime.db.execute(
        "UPDATE jobs SET updated_at = ? WHERE id = ?", (iso(utc_now() - timedelta(hours=1)), job_id)
    )
    body = _said(await invoke(runtime, ".repair", chat_id=OWNER))
    assert "un-stuck" in body
    row = await runtime.db.fetch_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "pending"


async def test_why_names_the_gate_that_held_her(runtime) -> None:
    runtime.persona_replies._skip("not_addressed")
    body = _said(await invoke(runtime, ".why", chat_id=OWNER))
    assert "not addressed" in body.lower() or "name" in body.lower()


async def test_news_reads_the_run_record(runtime, tmp_path, monkeypatch) -> None:
    record = {
        "history": [
            {
                "commit": "abcdef1234567",
                "healthy": True,
                "uptime_seconds": 1830,
                "written_at": iso(utc_now() - timedelta(minutes=5)),
                "reason": "stepped aside for the next deploy",
            }
        ]
    }
    path = tmp_path / "bot-status.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    body = _said(await invoke(runtime, ".news", chat_id=OWNER))
    assert "abcdef1" in body and "30m" in body


# ---------------------------------------------------------------------------
# The sentences, end to end
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sentence", "command"),
    (
        ("forward this to @sara", "forward @sara"),
        ("copy that to saved", "copy saved"),
        ("reply to that and say ok", "reply ok"),
        ("change that message to say nine", "edit nine"),
        ("unsend that", "delete"),
        ("pin that", "pin"),
        ("who's in my messages", "inbox"),
        ("what's unanswered", "unread"),
        ("what did Sara say", "read Sara"),
        ("who is @hmmm_6", "whois @hmmm_6"),
        ("my groups", "groups"),
        ("are you okay?", "diagnose"),
        ("fix yourself", "repair"),
        ("why didn't you answer", "why"),
        ("what's new", "news"),
        ("what version are you", "version"),
    ),
)
def test_the_sentence_becomes_the_command(sentence: str, command: str) -> None:
    found = intent_module.detect(sentence)
    assert found is not None, sentence
    assert found.lines[0] == command


def test_a_question_that_is_not_about_her_stays_conversation() -> None:
    for sentence in (
        "why didn't you answer that email from the bank",
        "how are you today?",
        "what did the doctor say about my back",
    ):
        assert intent_module.detect(sentence) is None, sentence


class _SummaryProvider:
    model = "stub"
    available = True

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.reply
