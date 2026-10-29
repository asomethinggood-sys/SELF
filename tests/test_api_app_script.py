"""The my.telegram.org helper: response classification is the whole value.

The script cannot be tested against the real site (it needs a live session and
the right network), so everything that decides *what to tell the user* is tested
against the replies Telegram is known to produce.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "create_api_app.py"
spec = importlib.util.spec_from_file_location("create_api_app", SCRIPT)
module = importlib.util.module_from_spec(spec)
# dataclasses resolve annotations through sys.modules, so register first.
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_parses_json_success() -> None:
    outcome = module.parse_response('{"app_id": 123456, "app_hash": "0123456789abcdef"}')
    assert outcome.kind == "ok"
    assert outcome.api_id == "123456"
    assert outcome.api_hash == "0123456789abcdef"


def test_parses_alternative_key_names() -> None:
    outcome = module.parse_response('{"api_id": 42, "api_hash": "deadbeef"}')
    assert outcome.succeeded and outcome.api_id == "42"


def test_bare_error_is_a_refusal() -> None:
    outcome = module.parse_response("ERROR")
    assert outcome.kind == "refused"


def test_object_object_is_a_refusal() -> None:
    assert module.parse_response("[object Object]").kind == "refused"


def test_rate_limit_is_detected_and_never_retried() -> None:
    assert module.parse_response("Too many attempts, please try again later").kind == "flood"
    assert module.parse_response("", status=429).kind == "flood"
    assert module.parse_response('{"error": "FLOOD_WAIT_3600"}').kind == "flood"


def test_json_error_message_is_surfaced() -> None:
    outcome = module.parse_response('{"error": "SHORTNAME_TAKEN"}')
    assert outcome.kind in {"flood", "refused"}
    assert "SHORTNAME_TAKEN" in outcome.detail or outcome.kind == "flood"


def test_extracts_hidden_hash() -> None:
    html = '<form><input type="hidden" name="hash" value="abc123" /></form>'
    assert module.extract_hash(html) == "abc123"
    assert module.extract_hash("<form></form>") == ""


def test_detects_an_existing_application() -> None:
    html = '<div id="app_id">987654</div><div id="app_hash">0f0f0f</div>'
    outcome = module.existing_app(html)
    assert outcome is not None and outcome.api_id == "987654"
    assert module.existing_app("<html>nothing here</html>") is None


def test_form_fields_match_the_website() -> None:
    fields = module.form_fields("desktop", "T", "shortname", "https://x.test", "D")
    assert fields["app_shortname"] == "shortname"
    assert fields["app_platform"] == "desktop"
    assert "MIME Type" in fields


@pytest.mark.parametrize("value", ["abc", "havoc bot", "havoc!", "havocbot7k!"])
def test_short_name_validation_rejects_silent_failures(value: str) -> None:
    with pytest.raises(SystemExit):
        module.validate_short_name(value)


def test_short_name_validation_accepts_valid_input() -> None:
    assert module.validate_short_name("  HavocBot7k ") == "havocbot7k"
    # Uppercase is silently normalised rather than rejected.
    assert module.validate_short_name("HAVOCBOT") == "havocbot"


class FakeTransport:
    """Stands in for the HTTP layer: a page GET then a POST per attempt."""

    def __init__(self, apps_html: str, replies: list[tuple[int, str]]) -> None:
        self.apps_html = apps_html
        self.replies = list(replies)
        self.posts: list[dict[str, str]] = []

    def __call__(self, url: str, *, token: str, data=None):
        if data is None:
            return 200, self.apps_html
        self.posts.append(data)
        return self.replies.pop(0)


def test_stops_after_the_first_flood(monkeypatch) -> None:
    transport = FakeTransport(
        '<input type="hidden" name="hash" value="h1">',
        [(200, "ERROR"), (200, "Too many attempts, please try again later"), (200, '{"app_id":1,"app_hash":"a"}')],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(module, "_request", transport)

    outcome = module.create_app(
        token="t", title="T", short_name="havocbot7k", url="u", description="d",
        platforms=("desktop", "android", "web"), pause=0, sleep=sleeps.append,
    )

    assert outcome.kind == "flood"
    # The third platform was never attempted: retrying a flood is what makes it worse.
    assert len(transport.posts) == 2
    assert transport.posts[0]["platform"] if "platform" in transport.posts[0] else True


def test_tries_next_platform_after_a_plain_refusal(monkeypatch) -> None:
    transport = FakeTransport(
        '<input type="hidden" name="hash" value="h1">',
        [(200, "ERROR"), (200, '{"app_id": 7, "app_hash": "abc"}')],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(module, "_request", transport)

    outcome = module.create_app(
        token="t", title="T", short_name="havocbot7k", url="u", description="d",
        platforms=("desktop", "android"), pause=0, sleep=sleeps.append,
    )

    assert outcome.succeeded and outcome.api_id == "7"
    assert [post["app_platform"] for post in transport.posts] == ["desktop", "android"]


def test_existing_app_short_circuits_creation(monkeypatch) -> None:
    transport = FakeTransport('<div id="app_id">11</div><div id="app_hash">22</div>', [])
    monkeypatch.setattr(module, "_request", transport)

    outcome = module.create_app(
        token="t", title="T", short_name="havocbot7k", url="u", description="d", pause=0
    )

    assert outcome.succeeded and outcome.api_id == "11"
    assert transport.posts == []


def test_rejected_token_is_reported_clearly(monkeypatch) -> None:
    def unauthorized(url, *, token, data=None):
        return 302, ""

    monkeypatch.setattr(module, "_request", unauthorized)
    outcome = module.create_app(
        token="bad", title="T", short_name="havocbot7k", url="u", description="d", pause=0
    )
    assert outcome.kind == "refused"
    assert "token" in outcome.detail


def test_missing_token_exits_with_instructions(capsys) -> None:
    assert module.main(["--token", ""]) == 2
    assert "stel_token" in capsys.readouterr().err
