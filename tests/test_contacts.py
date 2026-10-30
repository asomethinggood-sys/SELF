"""Her window into the address book and the block list.

The fake client answers raw requests through ``responses``, so these tests see
the same objects a live session would hand back. What matters is that a *refusal*
from Telegram becomes a sentence, and that a block only happens when asked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import FakeUser, invoke

OWNER = 42


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _contacts(*users: FakeUser) -> SimpleNamespace:
    return SimpleNamespace(users=list(users))


async def test_contacts_lists_names_handles_and_numbers(runtime) -> None:
    runtime.client.responses["GetContactsRequest"] = _contacts(
        FakeUser(501, "Sara", "Ahmadi", "sara_a"),
        FakeUser(502, "Reza", "", None),
    )
    runtime.telegram.me.id = OWNER
    sent = await invoke(runtime, ".contacts")
    text = _said(sent)
    assert "Sara Ahmadi" in text and "@sara_a" in text
    assert "Reza" in text
    assert "501" in text


async def test_contacts_filters_by_query(runtime) -> None:
    runtime.client.responses["GetContactsRequest"] = _contacts(
        FakeUser(501, "Sara", "Ahmadi", "sara_a"),
        FakeUser(502, "Reza", "", None),
    )
    sent = await invoke(runtime, ".contacts sara")
    text = _said(sent)
    assert "Sara" in text
    assert "Reza" not in text


async def test_contacts_with_no_match_is_honest(runtime) -> None:
    runtime.client.responses["GetContactsRequest"] = _contacts(FakeUser(501, "Sara"))
    sent = await invoke(runtime, ".contacts nobody")
    assert "nobody" in _said(sent).lower()


async def test_contacts_on_an_empty_book_says_so(runtime) -> None:
    runtime.client.responses["GetContactsRequest"] = _contacts()
    sent = await invoke(runtime, ".contacts")
    assert "empty" in _said(sent).lower()


async def test_contacts_failure_is_reported_with_the_reason(runtime) -> None:
    """A rate limit is not a crash: his chat gets Telegram's own words."""

    async def failing(self, request):
        self.raw_requests.append(request)
        raise RuntimeError("FLOOD_WAIT_30")

    original = type(runtime.client).__call__
    type(runtime.client).__call__ = failing
    try:
        sent = await invoke(runtime, ".contacts")
    finally:
        type(runtime.client).__call__ = original
    assert "FLOOD_WAIT_30" in _said(sent)


async def test_adding_a_contact_imports_the_number(runtime) -> None:
    runtime.client.responses["ImportContactsRequest"] = _contacts(
        FakeUser(601, "Sara", "Ahmadi", "sara_a")
    )
    sent = await invoke(runtime, ".contact +15551234567 Sara Ahmadi")
    assert "Sara Ahmadi" in _said(sent)
    request = runtime.client.raw_requests[-1]
    assert type(request).__name__ == "ImportContactsRequest"
    imported = request.contacts[0]
    assert imported.phone == "+15551234567"
    assert imported.first_name == "Sara"
    assert imported.last_name == "Ahmadi"


async def test_a_bad_number_never_reaches_telegram(runtime) -> None:
    sent = await invoke(runtime, ".contact not-a-number Sara")
    assert "not-a-number" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_adding_a_contact_without_a_name_asks_for_both(runtime) -> None:
    sent = await invoke(runtime, ".contact +15551234567")
    assert ".contact" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_a_number_telegram_does_not_know_says_nothing_was_added(runtime) -> None:
    runtime.client.responses["ImportContactsRequest"] = _contacts()
    sent = await invoke(runtime, ".contact +15550000000 Nobody")
    assert "nothing was added" in _said(sent).lower()


async def test_block_sends_a_block_request_for_the_named_chat(runtime) -> None:
    sent = await invoke(runtime, ".block --yes @spammer")
    assert "cannot reach" in _said(sent).lower()
    request = runtime.client.raw_requests[-1]
    assert type(request).__name__ == "BlockRequest"
    assert runtime.client.input_peers, "the peer was never resolved"


async def test_block_asks_before_it_blocks(runtime) -> None:
    sent = await invoke(runtime, ".block @spammer")
    assert "confirm" in _said(sent).lower()
    assert runtime.client.raw_requests == []


async def test_block_with_yes_goes_through(runtime) -> None:
    await invoke(runtime, ".block --yes @spammer")
    assert runtime.client.raw_requests[-1].__class__.__name__ == "BlockRequest"


async def test_unblock_sends_an_unblock_request(runtime) -> None:
    await invoke(runtime, ".unblock --yes @spammer")
    assert runtime.client.raw_requests[-1].__class__.__name__ == "UnblockRequest"


async def test_block_needs_a_target(runtime) -> None:
    sent = await invoke(runtime, ".block")
    assert ".block" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_blocklist_names_who_is_blocked(runtime) -> None:
    runtime.client.responses["GetBlockedRequest"] = _contacts(FakeUser(701, "Spam", "", "spammer"))
    sent = await invoke(runtime, ".blocklist")
    text = _said(sent)
    assert "Spam" in text and "@spammer" in text and "701" in text


async def test_blocklist_empty_is_a_sentence(runtime) -> None:
    runtime.client.responses["GetBlockedRequest"] = _contacts()
    sent = await invoke(runtime, ".blocklist")
    assert "nobody is blocked" in _said(sent).lower()


@pytest.mark.parametrize("line", [".contacts --limit 5", ".blocklist"])
async def test_every_listing_command_answers_something(runtime, line) -> None:
    runtime.client.responses["GetContactsRequest"] = _contacts()
    runtime.client.responses["GetBlockedRequest"] = _contacts()
    sent = await invoke(runtime, line)
    assert _said(sent).strip(), "an empty answer is not an answer"
