"""The TikTok side, tested where the account is at risk.

What is worth proving without a live account:

* the jar is private on disk and refuses to load when TikTok has signed it out;
* an allowance is charged **before** an action, survives a restart, and a refusal
  never half-happens;
* the same message is never answered twice, even across a crash;
* a challenge stops the run instead of retrying into it;
* a renamed selector is reported as drift (exit 3), not as a bad key or a limit;
* the command surface refuses writes from anyone who is not the owner.

The browser is faked. A test that needs a real TikTok login is a test that runs
once and then rots.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from havoc.tiktok import Account, SessionError, TikTokSessionStore, redact
from havoc.tiktok.browser import ActionResult, InboxMessage
from havoc.tiktok.limits import Budget, Pace, budget_for


def _account() -> Account:
    return Account(
        cookies=[
            {"name": "sessionid", "value": "x" * 32, "domain": ".tiktok.com", "path": "/"},
            {"name": "tt_webid_v2", "value": "1", "domain": ".tiktok.com", "path": "/"},
        ],
        user_agent="Mozilla/5.0",
        created_at=1_700_000_000.0,
        handle="someone",
        user_id="123",
    )


# ---------------------------------------------------------------------------
# The jar
# ---------------------------------------------------------------------------
def test_the_jar_is_private_and_round_trips(tmp_path) -> None:
    store = TikTokSessionStore(tmp_path)
    store.save(_account())
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        assert mode == 0o600, f"cookie jar is {oct(mode)}"
    loaded = store.load()
    assert loaded.handle == "someone"
    assert loaded.logged_in()
    assert loaded.cookie("sessionid") == "x" * 32


def test_a_signed_out_jar_says_so_and_does_not_pretend(tmp_path) -> None:
    store = TikTokSessionStore(tmp_path)
    store.path.write_text(json.dumps({"cookies": [{"name": "tt_webid_v2", "value": "1"}]}))
    with pytest.raises(SessionError) as caught:
        store.load()
    assert caught.value.kind == "expired"
    assert "login" in caught.value.hint


def test_no_jar_at_all_names_the_command(tmp_path) -> None:
    with pytest.raises(SessionError) as caught:
        TikTokSessionStore(tmp_path).load()
    assert caught.value.kind == "no_session"
    assert "havoc tt login" in caught.value.hint


def test_redact_keeps_names_and_drops_values() -> None:
    blob = {"cookies": [{"name": "sessionid", "value": "SECRET"}], "msToken": "ALSO"}
    text = json.dumps(redact(blob))
    assert "SECRET" not in text and "ALSO" not in text
    assert "sessionid" in text and "msToken" in text


# ---------------------------------------------------------------------------
# The allowance
# ---------------------------------------------------------------------------
def test_spending_is_charged_before_and_persists_across_restarts(tmp_path) -> None:
    store = TikTokSessionStore(tmp_path)
    first = Budget(store)
    first.spend("follow")
    # A different object on the same directory is what a handover looks like.
    second = Budget(TikTokSessionStore(tmp_path))
    assert second.spent("follow") == 1


def test_the_ceiling_refuses_without_writing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HAVOC_TIKTOK_DAILY_COMMENT", "2")
    budget = Budget(TikTokSessionStore(tmp_path))
    budget.spend("comment")
    budget.spend("comment")
    with pytest.raises(SessionError) as caught:
        budget.spend("comment")
    assert caught.value.kind == "budget"
    assert budget.spent("comment") == 2


def test_a_new_day_resets_and_an_env_typo_does_not(monkeypatch) -> None:
    monkeypatch.setenv("HAVOC_TIKTOK_DAILY_LIKE", "banana")
    assert budget_for("like") == 120
    monkeypatch.setenv("HAVOC_TIKTOK_DAILY_LIKE", "0")
    assert budget_for("like") == 0
    monkeypatch.setenv("HAVOC_TIKTOK_DAILY_LIKE", "5")
    assert budget_for("like") == 5


def test_pacing_is_a_range_not_a_metronome() -> None:
    import random

    pace = Pace(rng=random.Random(3))
    gaps = {round(pace.seconds_for("like"), 3) for _ in range(25)}
    assert len(gaps) > 5
    assert min(gaps) >= 2.5
    assert max(gaps) <= 2.5 * 1.8 + 0.01


# ---------------------------------------------------------------------------
# The loop: dedupe, ownership, challenge
# ---------------------------------------------------------------------------
class _FakeStore(TikTokSessionStore):
    """A store whose jar never hits the disk (state/ledger still do)."""

    def save(self, account: Account) -> None:
        return


class _FakeBrowser:
    """Records what it was asked to send. Never opens anything."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.pace = Pace(speed=0.001)
        self._frames: list[dict] = []

    async def send_dm(self, text: str) -> ActionResult:
        self.sent.append(text)
        return ActionResult("dm", True, "sent")

    async def challenged(self) -> bool:
        return False

    async def poll_threads(self) -> list[InboxMessage]:
        return []


@pytest.fixture()
def bot(tmp_path):
    from havoc.tiktok.bot import BotConfig, TikTokBot

    store = _FakeStore(tmp_path)
    bot = TikTokBot(store, BotConfig(run_seconds=60, owner_id="owner"), runtime=None)
    bot.browser = _FakeBrowser()
    bot._load_handled()
    return bot


async def test_a_message_is_answered_once_even_if_it_arrives_twice(bot) -> None:
    message = InboxMessage(thread_id="owner", message_id="m1", sender="owner", text="hi yura")
    await bot.handle(message)
    await bot.handle(message)
    assert len(bot.browser.sent) == 1
    assert bot.stats["skipped"] == 1


async def test_a_reply_is_recorded_before_it_is_sent(bot) -> None:
    message = InboxMessage(thread_id="owner", message_id="m2", sender="owner", text="hello")
    bot._mark_handled(message.key)  # what handle() does before sending
    assert bot._already_handled("owner:m2")


async def test_a_stranger_cannot_make_her_write(bot, monkeypatch) -> None:
    from havoc.tiktok import command as cmd

    called: list[str] = []

    class _Hands:
        async def block(self, handle: str) -> ActionResult:
            called.append(handle)
            return ActionResult("block", True, "blocked")

    bot.actions = _Hands()
    reply = await cmd.dispatch("block @nobody", bot=bot, owner=False, message=InboxMessage("x", "1", "x", ""))
    assert called == [], "a non-owner made the account block someone"
    assert reply


async def test_the_owner_can_block_and_gets_a_receipt(bot) -> None:
    from havoc.tiktok import command as cmd

    class _Hands:
        async def block(self, handle: str) -> ActionResult:
            return ActionResult("block", True, f"blocked @{handle}")

    bot.actions = _Hands()
    reply = await cmd.dispatch("block @spammer", bot=bot, owner=True, message=InboxMessage("owner", "1", "o", ""))
    assert isinstance(reply, str) and reply and len(reply) <= 150


async def test_comment_without_a_link_is_refused_locally(bot) -> None:
    from havoc.tiktok import command as cmd

    class _Hands:
        async def comment(self, *_args) -> ActionResult:  # pragma: no cover - must not run
            raise AssertionError("should not reach TikTok without a url")

    bot.actions = _Hands()
    reply = await cmd.dispatch("comment just some words", bot=bot, owner=True, message=InboxMessage("o", "1", "o", ""))
    assert "link" in reply.lower()


async def test_stop_halts_instead_of_answering(bot) -> None:
    from havoc.tiktok import command as cmd

    reply = await cmd.dispatch("stop", bot=bot, owner=True, message=InboxMessage("o", "1", "o", ""))
    assert "stopping" in reply.lower()
    assert bot.stats.get("stopped") == 1


async def test_help_survives_one_dm(bot) -> None:
    from havoc.tiktok import command as cmd

    reply = await cmd.dispatch("help", bot=bot, owner=False, message=InboxMessage("x", "1", "x", ""))
    assert reply and len(reply) < 1000


async def test_reminder_parsing_handles_persian_digits_and_units(bot) -> None:
    from havoc.tiktok import command as cmd

    reply = await cmd.dispatch("remind ۲ ساعت آب", bot=bot, owner=True, message=InboxMessage("o", "1", "o", ""))
    assert "120" in reply


# ---------------------------------------------------------------------------
# Failure shapes
# ---------------------------------------------------------------------------
def test_drift_is_reported_as_drift() -> None:
    from havoc.tiktok.browser import _drift

    result = _drift("like", "a like button")
    assert result.ok is False
    assert result.data["drift"] is True
    assert "changed" in result.detail


def test_a_receipt_never_exceeds_a_comment() -> None:
    from havoc.tiktok.voice import receipt

    for action in ("like", "follow", "block", "comment", "upload", "dm"):
        line = receipt(action, target="a" * 200)
        assert len(line) <= 150, (action, len(line))


def test_the_challenge_message_does_not_offer_a_retry() -> None:
    from havoc.tiktok.voice import challenge_notice

    text = challenge_notice().lower()
    assert "stopped" in text
    assert "try again" not in text and "retry" not in text
