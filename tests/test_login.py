"""Scripted (two-phase) login: request a code in one process, finish in another.

The interactive login is exercised by hand; this covers the path that a CI job
or a headless server uses, where the code arrives somewhere else:

    havoc login --request            # sends the code, saves the challenge
    havoc login --code 12345         # signs in, optionally exports a session
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_config
from telethon.sessions import MemorySession, StringSession

from havoc import main as cli_main
from havoc.client import AuthError, LoginChallenge, TelegramService, mask_phone


class FakeSentCode:
    def __init__(self, phone_code_hash: str = "hash-123") -> None:
        self.phone_code_hash = phone_code_hash
        self.type = "app"


class FakeMe:
    id = 99
    username = "owner"
    first_name = "Owner"
    phone = "+989010396925"


class FakeAuthClient:
    """The slice of Telethon's client that login touches."""

    def __init__(self, *, needs_password: bool = False, reject_code: bool = False) -> None:
        self.needs_password = needs_password
        self.reject_code = reject_code
        self.authorized = False
        self.sent_codes: list[str] = []
        self.sign_ins: list[dict[str, str]] = []
        self.session = MemorySession()
        self.session.set_dc(2, "149.154.167.51", 443)
        self.session.auth_key = None

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def send_code_request(self, phone: str) -> FakeSentCode:
        self.sent_codes.append(phone)
        return FakeSentCode()

    async def sign_in(self, **kwargs) -> None:
        self.sign_ins.append({key: str(value) for key, value in kwargs.items()})
        if "code" in kwargs:
            if self.reject_code:
                raise RuntimeError("PHONE_CODE_INVALID")
            if self.needs_password:
                from telethon.errors import SessionPasswordNeededError

                raise SessionPasswordNeededError(request=None)
        self.authorized = True

    async def get_me(self) -> FakeMe:
        return FakeMe()


def make_service(config, client: FakeAuthClient) -> TelegramService:
    service = TelegramService(config, __import__("logging").getLogger("login-test"))
    service.client = client
    service.connected = True
    return service


def test_mask_phone_keeps_only_the_edges() -> None:
    assert mask_phone("+989010396925") == "+9…925"
    assert mask_phone("") == "(none)"


def test_login_challenge_roundtrip() -> None:
    challenge = LoginChallenge(phone="+989010396925", phone_code_hash="abc", requested_at="now")
    restored = LoginChallenge.from_dict(json.loads(json.dumps(challenge.to_dict())))
    assert restored == challenge
    assert LoginChallenge.from_dict({}).phone == ""


async def test_request_code_sends_to_configured_phone(tmp_path: Path) -> None:
    config = make_config(tmp_path, phone="+989010396925")
    client = FakeAuthClient()
    service = make_service(config, client)

    challenge = await service.request_code()

    assert client.sent_codes == ["+989010396925"]
    assert challenge.phone == "+989010396925"
    assert challenge.phone_code_hash == "hash-123"
    assert challenge.requested_at


async def test_request_code_needs_a_phone(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = make_service(config, FakeAuthClient())
    with pytest.raises(AuthError, match="phone number"):
        await service.request_code()


async def test_submit_code_completes_login(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeAuthClient()
    service = make_service(config, client)
    challenge = await service.request_code("+989010396925")

    info = await service.submit_code(challenge, " 12-345 ")

    assert info.user_id == 99
    assert info.username == "owner"
    assert client.sign_ins[-1]["code"] == "12345"
    assert client.sign_ins[-1]["phone_code_hash"] == "hash-123"
    assert service.me is not None


async def test_submit_code_requires_a_code(tmp_path: Path) -> None:
    service = make_service(make_config(tmp_path), FakeAuthClient())
    challenge = await service.request_code("+989010396925")
    with pytest.raises(AuthError, match="code is required"):
        await service.submit_code(challenge, "   ")


async def test_two_factor_without_password_explains_itself(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = make_service(config, FakeAuthClient(needs_password=True))
    challenge = await service.request_code("+989010396925")

    with pytest.raises(AuthError) as caught:
        await service.submit_code(challenge, "12345")
    assert "two-factor" in str(caught.value)
    assert "--password" in caught.value.hint


async def test_two_factor_with_password_succeeds(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeAuthClient(needs_password=True)
    service = make_service(config, client)
    challenge = await service.request_code("+989010396925")

    info = await service.submit_code(challenge, "12345", password="s3cret")

    assert info.user_id == 99
    assert any("password" in call for call in client.sign_ins)


async def test_bad_code_is_reported_without_leaking_it(tmp_path: Path) -> None:
    service = make_service(make_config(tmp_path), FakeAuthClient(reject_code=True))
    challenge = await service.request_code("+989010396925")

    with pytest.raises(AuthError) as caught:
        await service.submit_code(challenge, "999999")
    message = str(caught.value)
    assert "999999" not in message
    assert "code" in caught.value.hint.lower()


def test_export_session_string_roundtrips(tmp_path: Path) -> None:
    client = FakeAuthClient()
    from telethon.crypto import AuthKey

    client.session.auth_key = AuthKey(b"\x07" * 256)
    service = make_service(make_config(tmp_path), client)

    value = service.export_session_string()

    assert value
    assert StringSession(value).auth_key.key == b"\x07" * 256


# ---------------------------------------------------------------------------
# CLI wiring: the two dispatches used by the GitHub workflow
# ---------------------------------------------------------------------------
class FakeService:
    instances: list[FakeService] = []

    def __init__(self, config, logger) -> None:
        self.config = config
        self.disconnected = False
        self.submitted: list[tuple[LoginChallenge, str, str | None]] = []
        FakeService.instances.append(self)

    async def request_code(self, phone=None) -> LoginChallenge:
        return LoginChallenge(phone=phone or "+989010396925", phone_code_hash="hash-cli")

    async def submit_code(self, challenge, code, *, password=None):
        self.submitted.append((challenge, code, password))
        from havoc.client import ClientInfo

        return ClientInfo(user_id=99, username="owner", first_name="Owner", phone="+98")

    async def authorize(self, *, interactive=True):
        raise AssertionError("the scripted path must not go interactive")

    def export_session_string(self) -> str:
        return "1" + "A" * 300

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "API_ID": "12345",
        "API_HASH": "0123456789abcdef0123456789abcdef",
        "PHONE": "+989010396925",
        "DATA_DIR": str(tmp_path / "data"),
        "DATABASE_PATH": str(tmp_path / "data" / "cli.db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "PLUGIN_DIR": str(tmp_path / "plugins"),
        "SESSION_PATH": str(tmp_path / "data" / "session"),
        "HAVOC_ENV_FILE": str(tmp_path / "missing.env"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli_main, "TelegramService", FakeService)
    FakeService.instances.clear()
    return values


def test_cli_request_writes_challenge_file(cli_env, capsys) -> None:
    state = Path(cli_env["DATA_DIR"]) / "login-state.json"

    assert cli_main.cli(["login", "--request"]) == cli_main.EXIT_OK

    payload = json.loads(state.read_text())
    assert payload["phone_code_hash"] == "hash-cli"
    assert (state.stat().st_mode & 0o777) == 0o600
    out = capsys.readouterr().out
    assert "code requested" in out.lower()
    # The full number never reaches the terminal.
    assert "+989010396925" not in out
    assert FakeService.instances[0].disconnected is True


def test_cli_request_then_code_logs_in_and_cleans_up(cli_env, capsys) -> None:
    assert cli_main.cli(["login", "--request"]) == cli_main.EXIT_OK
    capsys.readouterr()

    assert cli_main.cli(["login", "--code", "12345"]) == cli_main.EXIT_OK

    service = FakeService.instances[-1]
    assert service.submitted and service.submitted[0][1] == "12345"
    assert not (Path(cli_env["DATA_DIR"]) / "login-state.json").exists()
    assert "Logged in successfully" in capsys.readouterr().out


def test_cli_code_without_challenge_explains_the_order(cli_env, capsys) -> None:
    code = cli_main.cli(["login", "--code", "12345"])
    assert code == cli_main.EXIT_CONFIG
    assert "--request" in capsys.readouterr().err


def test_cli_export_session_writes_private_file(cli_env, tmp_path: Path) -> None:
    target = tmp_path / "session.txt"
    assert cli_main.cli(["login", "--request"]) == cli_main.EXIT_OK
    assert cli_main.cli(["login", "--code", "12345", "--export-session", str(target)]) == cli_main.EXIT_OK

    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600
    assert target.read_text().startswith("1AAAA")


# ---------------------------------------------------------------------------
# Connection proxy: the Telegram connection itself, not the HTTP providers
# ---------------------------------------------------------------------------
class _FakeTelethon:
    class connection:  # noqa: N801 - mirrors telethon.connection
        ConnectionTcpSOCKS5 = "SOCKS5"
        ConnectionTcpSOCKS4 = "SOCKS4"
        ConnectionTcpHTTP = "HTTP"


def _service_with(config):
    service = TelegramService(config, __import__("logging").getLogger("proxy-test"))
    return service


def test_no_proxy_configured_builds_none(tmp_path) -> None:
    service = _service_with(make_config(tmp_path))
    assert service._build_proxy(_FakeTelethon) is None


def test_socks5_proxy_is_passed_with_credentials(tmp_path) -> None:
    config = make_config(
        tmp_path,
        proxy_type="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1080,
        proxy_username="user",
        proxy_password="secret",
    )
    proxy = _service_with(config)._build_proxy(_FakeTelethon)
    assert proxy == ("127.0.0.1", 1080, "user", "secret", "SOCKS5")


def test_socks5_without_credentials_uses_the_short_form(tmp_path) -> None:
    config = make_config(tmp_path, proxy_type="socks5", proxy_host="10.0.0.1", proxy_port=1080)
    assert _service_with(config)._build_proxy(_FakeTelethon) == ("10.0.0.1", 1080, "SOCKS5")


def test_mtproxy_passes_the_secret(tmp_path) -> None:
    config = make_config(
        tmp_path,
        proxy_type="mtproxy",
        proxy_host="proxy.example",
        proxy_port=443,
        proxy_secret="dd0123456789abcdef0123456789abcdef",
    )
    assert _service_with(config)._build_proxy(_FakeTelethon) == (
        "proxy.example",
        443,
        "dd0123456789abcdef0123456789abcdef",
    )


def test_mtproxy_without_secret_is_an_auth_error(tmp_path) -> None:
    config = make_config(tmp_path, proxy_type="mtproxy", proxy_host="p", proxy_port=443)
    with pytest.raises(AuthError, match="PROXY_SECRET"):
        _service_with(config)._build_proxy(_FakeTelethon)


# ---------------------------------------------------------------------------
# Telegram's setup errors, translated into something actionable
# ---------------------------------------------------------------------------
def test_published_api_id_explains_the_real_fix() -> None:
    from havoc.client import explain_telegram_error

    message, hint = explain_telegram_error(RuntimeError("API_ID_PUBLISHED_FLOOD"))
    assert "published" in message.lower()
    assert "my.telegram.org" in hint


def test_invalid_api_id_points_at_the_pair() -> None:
    from havoc.client import explain_telegram_error

    message, hint = explain_telegram_error(Exception("API_ID_INVALID"))
    assert "rejected" in message
    assert "matching hash" in hint or "both values" in hint


def test_flood_wait_reports_the_seconds() -> None:
    from havoc.client import explain_telegram_error

    class FloodWaitError(Exception):
        seconds = 3600

    message, hint = explain_telegram_error(FloodWaitError("FLOOD_WAIT_3600"))
    assert "3600" in message
    assert "wait" in hint.lower()


def test_unknown_error_falls_back_to_redacted_detail() -> None:
    from havoc.client import explain_telegram_error

    message, hint = explain_telegram_error(
        RuntimeError("weird 0123456789abcdef0123456789abcdef failure"), fallback="Login failed."
    )
    assert message.startswith("Login failed")
    # a 32-hex string looks like a hash and must be redacted
    assert "0123456789abcdef0123456789abcdef" not in hint


def test_two_factor_error_is_recognised() -> None:
    from havoc.client import explain_telegram_error

    message, hint = explain_telegram_error(Exception("SESSION_PASSWORD_NEEDED"))
    assert "two-factor" in message
    assert "2FA" in hint
