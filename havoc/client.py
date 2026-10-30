"""Telethon client construction and authentication.

Behaviour
---------
* ``SESSION_STRING`` wins when present (headless servers).
* Otherwise a SQLite session file is used at ``SESSION_PATH``; the file is the
  account credential and must never be committed.
* First login is interactive: phone → code → 2FA password if enabled.
  Prompts read from stdin, never from command-line arguments (which would leak
  into shell history) and never echo the password.
* Missing credentials, revoked sessions and 2FA failures raise
  :class:`AuthError` with actionable, secret-free messages.
* Telethon is imported lazily so the rest of HAVOC (and the test suite) works
  without it installed.

Feature areas: 1, 84 (reconnects), 9 (session safety), 16 (hosting docs).
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils.security import redact_text
from .utils.time import iso, utc_now


class AuthError(Exception):
    """Authentication could not be completed."""

    def __init__(self, message: str, hint: str = "") -> None:
        self.hint = hint
        super().__init__(message)


class TelegramUnavailable(AuthError):
    pass


@dataclass
class ClientInfo:
    user_id: int | None
    username: str | None
    first_name: str | None
    phone: str | None
    session_kind: str = "file"


@dataclass
class LoginChallenge:
    """State that has to survive between "send me a code" and "here is the code".

    Telegram ties the code to a ``phone_code_hash`` that is only returned by
    ``send_code_request``. An interactive shell keeps it in memory; a scripted
    login (a CI job, a two-step web form) has to persist it, so it gets a
    serializable shape.
    """

    phone: str
    phone_code_hash: str
    requested_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "phone": self.phone,
            "phone_code_hash": self.phone_code_hash,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoginChallenge:
        return cls(
            phone=str(data.get("phone", "")),
            phone_code_hash=str(data.get("phone_code_hash", "")),
            requested_at=str(data.get("requested_at", "")),
        )


def mask_phone(phone: str | None) -> str:
    """Show just enough of a number to recognize it, never the whole thing."""
    value = (phone or "").strip()
    if len(value) <= 4:
        return "…" if value else "(none)"
    return f"{value[:2]}…{value[-3:]}"


#: Telegram's error names mapped to what the operator should do about them.
#: These are the ones that stop a *setup*, as opposed to a runtime hiccup, and
#: each has exactly one correct response.
AUTH_ERROR_GUIDANCE: dict[str, tuple[str, str]] = {
    "API_ID_PUBLISHED_FLOOD": (
        "this api_id is one of Telegram's published ones and is rate-limited",
        "Register your own application at https://my.telegram.org/apps — a public "
        "api_id (Telegram Desktop's 2040 and friends) works only briefly, if at all.",
    ),
    "API_ID_INVALID": (
        "the api_id/api_hash pair was rejected",
        "Copy both values from https://my.telegram.org/apps again; an id without its "
        "matching hash is invalid.",
    ),
    "API_ID_PUBLISHED": (
        "this api_id belongs to a public client",
        "Register your own application at https://my.telegram.org/apps.",
    ),
    "PHONE_NUMBER_BANNED": (
        "this phone number is banned by Telegram",
        "Nothing in the bot can work around a ban; appeal via https://t.me/SpamBot.",
    ),
    "PHONE_NUMBER_INVALID": (
        "Telegram does not accept that phone number",
        "Use international format with the + and country code, e.g. +989010396925.",
    ),
    "PHONE_NUMBER_FLOOD": (
        "too many login codes were requested for this number",
        "Wait 24 hours and request exactly one code. Every retry restarts the wait.",
    ),
    "PHONE_CODE_EXPIRED": (
        "the login code expired",
        "Codes last a couple of minutes; start over with a fresh request.",
    ),
    "PHONE_CODE_INVALID": (
        "the login code was wrong",
        "Type the login code exactly as Telegram shows it — digits only.",
    ),
    "SESSION_PASSWORD_NEEDED": (
        "the account has two-factor authentication enabled",
        "Supply the 2FA password (the login prompt asks for it interactively).",
    ),
    "AUTH_KEY_UNREGISTERED": (
        "the saved session was revoked",
        "Log in again; the session string is no longer valid.",
    ),
    "FLOOD_WAIT": (
        "Telegram asked for a wait",
        "Wait the number of seconds it names, then retry once.",
    ),
}


def explain_telegram_error(exc: BaseException, *, fallback: str = "") -> tuple[str, str]:
    """Turn a Telethon exception into (message, hint) an operator can act on."""
    name = type(exc).__name__
    text = str(exc)

    # ``FloodWaitError`` carries the number of seconds; surface it.
    seconds = getattr(exc, "seconds", None)
    for key, (message, hint) in AUTH_ERROR_GUIDANCE.items():
        if key.lower() in name.lower() or key in text:
            if key == "FLOOD_WAIT" and seconds:
                message = f"Telegram asked for a {seconds}s wait"
            return (f"{message} ({name})", hint)

    if seconds:
        return (f"Telegram asked for a {seconds}s wait", "Wait it out, then retry once.")
    detail = redact_text(text)[:200] or name
    return (fallback or f"Telegram refused the request ({name})", detail)


def telethon_module() -> Any:
    try:
        import telethon  # noqa: F401

        return telethon
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise TelegramUnavailable(
            "Telethon is not installed",
            hint="Run: pip install -e .   (or: pip install 'telethon>=1.34')",
        ) from exc


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401

        return True
    except ImportError:  # pragma: no cover
        return False


class TelegramService:
    """Owns the Telethon client lifecycle."""

    def __init__(self, config: Any, logger: Any) -> None:
        self.config = config
        self.logger = logger
        self.client: Any | None = None
        self.me: Any | None = None
        self.connected = False
        self.reconnect_attempts = 0
        self._auth_prompt = _stdin_prompt

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_client(self) -> Any:
        telethon = telethon_module()
        if not self.config.api_id or not self.config.api_hash:
            raise AuthError(
                "API_ID/API_HASH are missing",
                hint="Create an app at https://my.telegram.org and put both values in .env",
            )
        if self.config.session_string:
            session: Any = telethon.sessions.StringSession(self.config.session_string)
            kind = "string"
        else:
            session_path = self.config.session_file
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session = telethon.sessions.SQLiteSession(str(session_path))
            kind = "file"
        proxy = self._build_proxy(telethon)
        client = telethon.TelegramClient(
            session,
            self.config.api_id,
            self.config.api_hash,
            proxy=proxy,
            device_model="Yura",
            system_version="2.0",
            app_version="2.0.0",
            connection_retries=5,
            retry_delay=2,
            auto_reconnect=True,
            request_retries=3,
            timeout=15,
        )
        self.session_kind = kind
        return client

    def _build_proxy(self, telethon: Any) -> Any | None:
        """Translate the PROXY_* settings into what Telethon expects.

        MTProto is blocked in some networks (Iran among them), so the Telegram
        connection itself sometimes has to travel through a proxy. Only this
        connection is proxied; the AI/translation HTTP clients are not.
        """
        if not getattr(self.config, "proxy_configured", False):
            return None
        kind = (self.config.proxy_type or "").lower()
        host = self.config.proxy_host
        port = int(self.config.proxy_port or 0)
        if kind == "mtproxy":
            secret = self.config.proxy_secret.strip()
            if not secret:  # pragma: no cover - validate() catches this first
                raise AuthError(
                    "PROXY_SECRET is required for an mtproxy",
                    hint="Copy the secret shown when the proxy was created.",
                )
            return (host, port, secret)
        supported = getattr(telethon, "connection", None)
        types = {
            "socks5": "ConnectionTcpSOCKS5",
            "socks4": "ConnectionTcpSOCKS4",
            "http": "ConnectionTcpHTTP",
        }
        attribute = types.get(kind)
        if attribute is None or not hasattr(supported, attribute):  # pragma: no cover
            raise AuthError(
                f"this Telethon version cannot use a {kind} proxy",
                hint="Upgrade Telethon, or use PROXY_TYPE=mtproxy.",
            )
        proxy_type = getattr(supported, attribute)
        if self.config.proxy_username:
            return (host, port, self.config.proxy_username, self.config.proxy_password, proxy_type)
        return (host, port, proxy_type)

    async def connect(self) -> Any:
        if self.client is not None and self.connected:
            return self.client
        client = self.client or self._build_client()
        self.client = client
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001
            proxy_on = bool(getattr(self.config, "proxy_configured", False))
            raise AuthError(
                f"could not connect to Telegram: {redact_text(str(exc))}",
                hint=(
                    "The connection through PROXY_TYPE/PROXY_HOST failed. Check the proxy "
                    "is running and that these values are right."
                    if proxy_on
                    else "Check your network. If Telegram is blocked where you are, set "
                    "PROXY_TYPE/PROXY_HOST/PROXY_PORT (see docs/CONFIGURATION.md)."
                ),
            ) from exc
        self.connected = bool(client.is_connected())
        self._register_disconnect_hooks(client)
        return client

    def _register_disconnect_hooks(self, client: Any) -> None:
        """Track disconnects/reconnects for health reporting."""
        try:
            from telethon import events

            async def _on_disconnect(_event: Any) -> None:
                self.connected = False
                self.logger.warning("Telegram connection lost; telethon will reconnect")

            async def _on_connect(_event: Any) -> None:
                self.connected = True
                self.logger.info("Telegram connection established")

            client.add_event_handler(_on_disconnect, events.Disconnect if hasattr(events, "Disconnect") else events.Raw)
            client.add_event_handler(_on_connect, events.Connect if hasattr(events, "Connect") else events.Raw)
        except Exception:  # pragma: no cover - older telethon versions
            self.logger.debug("disconnect hooks unavailable on this Telethon version")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def is_authorized(self) -> bool:
        client = await self.connect()
        try:
            authorized = bool(await client.is_user_authorized())
        except Exception as exc:  # noqa: BLE001 - network/entity failures
            raise AuthError(
                f"could not check the session: {redact_text(str(exc))}",
                hint="Check your connection, then try again.",
            ) from exc
        if authorized and self.me is None:
            self.me = await client.get_me()
        return authorized

    async def request_code(self, phone: str | None = None) -> LoginChallenge:
        """Ask Telegram to send a login code; returns the state needed later.

        Kept separate from :meth:`submit_code` so the two halves can run in
        different processes — an interactive shell, or two steps of a CI job.
        """
        client = await self.connect()
        if await client.is_user_authorized():  # pragma: no cover - operator mistake
            self.me = await client.get_me()
            raise AuthError(
                "this session is already logged in",
                hint="Nothing to do. Delete the session file (or unset SESSION_STRING) to start over.",
            )
        phone = (phone or getattr(self.config, "phone", "") or "").strip().replace(" ", "")
        if not phone:
            raise AuthError("a phone number is required for the first login", hint="Set PHONE in .env")
        try:
            sent = await client.send_code_request(phone)
        except Exception as exc:  # noqa: BLE001
            message, hint = explain_telegram_error(
                exc, fallback="Could not request a login code."
            )
            raise AuthError(f"could not request a login code: {message}", hint=hint) from exc
        self.logger.info("login code requested for %s", mask_phone(phone))
        return LoginChallenge(
            phone=phone,
            phone_code_hash=getattr(sent, "phone_code_hash", "") or "",
            requested_at=iso(utc_now()),
        )

    async def submit_code(
        self,
        challenge: LoginChallenge,
        code: str,
        *,
        password: str | None = None,
    ) -> ClientInfo:
        """Finish a login started by :meth:`request_code`."""
        client = await self.connect()
        telethon = telethon_module()
        code = (code or "").strip().replace(" ", "").replace("-", "")
        if not code:
            raise AuthError("a login code is required", hint="Run `havoc login --request` first.")
        if not challenge.phone_code_hash:
            raise AuthError(
                "the login challenge is incomplete",
                hint="Run `havoc login --request` again to get a fresh code.",
            )
        try:
            await client.sign_in(
                phone=challenge.phone, code=code, phone_code_hash=challenge.phone_code_hash
            )
        except telethon.errors.SessionPasswordNeededError:
            password = password or ""
            if not password:
                raise AuthError(
                    "this account is protected by two-factor authentication",
                    hint="Pass --password (or expose it as the TELEGRAM_2FA_PASSWORD secret).",
                ) from None
            try:
                await client.sign_in(password=password)
            except Exception as exc:  # noqa: BLE001
                message, hint = explain_telegram_error(
                    exc, fallback="Two-factor authentication failed."
                )
                raise AuthError(f"two-factor authentication failed: {message}", hint=hint) from exc
        except Exception as exc:  # noqa: BLE001
            message, hint = explain_telegram_error(exc, fallback="Login failed.")
            raise AuthError(f"login failed: {message}", hint=hint) from exc

        if not await client.is_user_authorized():  # pragma: no cover - defensive
            raise AuthError("login did not complete", hint="Try again; if it persists, delete the session file.")
        self.me = await client.get_me()
        self.logger.info("login complete for id=%s", getattr(self.me, "id", None))
        return self.info()

    def export_session_string(self) -> str:
        """Serialize the authorized session so it can be used headlessly."""
        if self.client is None:  # pragma: no cover - guarded by callers
            raise AuthError("not connected", hint="Log in first.")
        telethon = telethon_module()
        try:
            value = telethon.sessions.StringSession.save(self.client.session)
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"could not export the session: {redact_text(str(exc))}",
                hint="Export right after a successful login, in the same process.",
            ) from exc
        if not value:
            raise AuthError("the session is empty", hint="Log in first, then export.")
        return value

    async def authorize(self, *, interactive: bool = True) -> ClientInfo:
        # Asked through ``is_authorized`` so a failed session check is reported
        # the same way everywhere, and ``self.me`` is filled in exactly once.
        if await self.is_authorized():
            self.logger.info("session authorized as @%s (id=%s)", getattr(self.me, "username", None), getattr(self.me, "id", None))
            return self.info()

        if not interactive:
            raise AuthError(
                "the session is not authorized",
                hint="Run `havoc login` once on a machine with a terminal, or set SESSION_STRING.",
            )

        phone = self.config.phone
        if not phone:
            phone = await self._auth_prompt("Phone number (international format, e.g. +491234567890): ")
        challenge = await self.request_code(phone)
        code = await self._auth_prompt("Login code from Telegram: ")
        try:
            return await self.submit_code(challenge, code)
        except AuthError as exc:
            # Only the 2FA prompt can be answered interactively; anything else
            # (expired code, revoked app) needs a fresh run.
            if "two-factor" not in str(exc).lower():
                raise
        password = await self._auth_prompt("Two-factor password: ", secret=True)
        return await self.submit_code(challenge, code, password=password)

    def info(self) -> ClientInfo:
        me = self.me
        return ClientInfo(
            user_id=getattr(me, "id", None),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", None),
            phone=getattr(me, "phone", None),
            session_kind=getattr(self, "session_kind", "file"),
        )

    # ------------------------------------------------------------------
    # Runtime helpers used by commands
    # ------------------------------------------------------------------
    async def get_reply_message(self, message: Any) -> Any | None:
        if self.client is None:
            return None
        try:
            return await message.get_reply_message()
        except Exception:  # noqa: BLE001 - deleted/inaccessible reply
            try:
                return await self.client.get_messages(message.chat_id, ids=message.reply_to_msg_id)
            except Exception:
                return None

    async def resolve_entity(self, value: Any) -> Any | None:
        if self.client is None:
            return None
        try:
            return await self.client.get_entity(value)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("could not resolve entity %r: %s", value, exc)
            return None

    async def get_messages(self, entity: Any, **kwargs: Any) -> Any:
        if self.client is None:
            return []
        return await self.client.get_messages(entity, **kwargs)

    async def iter_messages(self, entity: Any, **kwargs: Any) -> Any:
        if self.client is None:
            return None
        return self.client.iter_messages(entity, **kwargs)

    async def download_media(self, message: Any, file: Any, **kwargs: Any) -> Any:
        if self.client is None:
            return None
        return await self.client.download_media(message, file=file, **kwargs)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    async def disconnect(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("error during disconnect: %s", exc)
        finally:
            self.connected = False
            self.client = None

    async def run_until_disconnected(self, *, max_retries: int = 5) -> None:
        """Block until the client stops, reconnecting with backoff on failure."""
        if self.client is None:
            raise TelegramUnavailable("client is not connected")
        attempt = 0
        while True:
            try:
                await self.client.run_until_disconnected()
                return
            except (TimeoutError, ConnectionError, OSError) as exc:
                attempt += 1
                self.reconnect_attempts += 1
                if attempt > max_retries:
                    raise AuthError(f"connection lost permanently: {redact_text(str(exc))}") from exc
                delay = min(60, 2 ** attempt)
                self.logger.warning("connection error (%s); reconnecting in %ss", exc, delay)
                await asyncio.sleep(delay)
                try:
                    await self.client.connect()
                except Exception as connect_exc:  # noqa: BLE001
                    self.logger.warning("reconnect attempt failed: %s", connect_exc)


def _stdin_prompt(prompt: str, *, secret: bool = False) -> str:
    """Prompt on stdin from a worker thread (keeps the loop responsive)."""
    if secret:
        try:
            return getpass.getpass(prompt)
        except (EOFError, OSError):  # pragma: no cover - non-tty
            pass
    try:
        return input(prompt)
    except EOFError as exc:  # pragma: no cover - non-interactive
        raise AuthError(
            "no terminal available for the login prompt",
            hint="Use SESSION_STRING for headless deployments.",
        ) from exc


def session_permissions_warning(session_path: Path) -> str | None:
    """Return a warning when the session file is readable by others."""
    if not session_path.exists():
        return None
    mode = session_path.stat().st_mode & 0o777
    if mode & 0o077:
        return f"session file {session_path} has permissions {oct(mode)}; restrict it with: chmod 600 {session_path}"
    return None




if sys.platform == "win32":  # pragma: no cover - documented platform note
    _ = None
