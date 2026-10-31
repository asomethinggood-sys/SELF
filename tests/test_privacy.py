"""``.privacy`` and ``.security`` — reading, never writing.

The fake answers each ``GetPrivacyRequest`` with one rule list, so the test sees
exactly what Telegram's rule objects look like. The security command is checked
twice: with the lock on and off, and for the one thing it must never do — print a
password.
"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import invoke
from telethon.tl.types import (
    PrivacyValueAllowAll,
    PrivacyValueAllowContacts,
    PrivacyValueDisallowAll,
)


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _privacy_answer(rules) -> SimpleNamespace:
    return SimpleNamespace(rules=rules)


async def test_privacy_lists_the_settings_it_read(runtime) -> None:
    runtime.client.responses["GetPrivacyRequest"] = _privacy_answer([PrivacyValueAllowContacts()])
    sent = await invoke(runtime, ".privacy")
    text = _said(sent)
    assert "last seen" in text.lower()
    assert "phone number" in text.lower()
    assert "my contacts" in text.lower()
    # One request per setting, all real requests.
    asked = [row for row in runtime.client.raw_requests if type(row).__name__ == "GetPrivacyRequest"]
    assert len(asked) == 7


async def test_privacy_maps_every_rule_shape(runtime) -> None:
    from havoc.services.tg_extra import TelegramExtras

    assert TelegramExtras.rule_word([PrivacyValueAllowAll()]) == "everyone"
    assert TelegramExtras.rule_word([PrivacyValueDisallowAll()]) == "nobody"
    assert TelegramExtras.rule_word([PrivacyValueAllowContacts()]) == "contacts"
    assert TelegramExtras.rule_word([]) == "unknown"


async def test_privacy_with_no_rules_says_so(runtime) -> None:
    runtime.client.responses["GetPrivacyRequest"] = _privacy_answer([])
    sent = await invoke(runtime, ".privacy")
    assert "unknown" in _said(sent).lower() or "not reported" in _said(sent).lower()


async def test_privacy_never_writes(runtime) -> None:
    runtime.client.responses["GetPrivacyRequest"] = _privacy_answer([PrivacyValueAllowAll()])
    await invoke(runtime, ".privacy")
    names = {type(row).__name__ for row in runtime.client.raw_requests}
    assert names <= {"GetPrivacyRequest"}, names


async def test_security_reports_the_lock_on(runtime) -> None:
    runtime.client.responses["GetPasswordRequest"] = SimpleNamespace(
        has_password=True, hint="the usual", has_recovery=True, pending_reset_date=None
    )
    sent = await invoke(runtime, ".security")
    text = _said(sent)
    assert "ON" in text
    assert "the usual" in text
    assert "recovery email is set" in text.lower()


async def test_security_reports_the_lock_off(runtime) -> None:
    runtime.client.responses["GetPasswordRequest"] = SimpleNamespace(
        has_password=False, hint=None, has_recovery=False, pending_reset_date=None
    )
    sent = await invoke(runtime, ".security")
    text = _said(sent)
    assert "OFF" in text
    assert "no recovery email" in text.lower()


async def test_security_mentions_a_pending_reset(runtime) -> None:
    runtime.client.responses["GetPasswordRequest"] = SimpleNamespace(
        has_password=True, hint=None, has_recovery=False, pending_reset_date="2026-09-01"
    )
    sent = await invoke(runtime, ".security")
    assert "reset" in _said(sent).lower()


async def test_security_never_shows_a_password(runtime) -> None:
    """The hint may be shown; the secret never is — the request cannot even read it."""
    runtime.client.responses["GetPasswordRequest"] = SimpleNamespace(
        has_password=True, hint="", has_recovery=False, pending_reset_date=None
    )
    sent = await invoke(runtime, ".security")
    text = _said(sent).lower()
    assert "password itself is never read" in text
    assert "srp" not in text and "algo" not in text


async def test_a_refusal_is_a_sentence(runtime) -> None:
    async def refuse(self, request):
        self.raw_requests.append(request)
        raise RuntimeError("FLOOD_WAIT_5")

    original = type(runtime.client).__call__
    type(runtime.client).__call__ = refuse
    try:
        sent = await invoke(runtime, ".privacy")
    finally:
        type(runtime.client).__call__ = original
    assert "FLOOD_WAIT_5" in _said(sent)
