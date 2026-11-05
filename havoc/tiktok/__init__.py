"""The TikTok account, as a session — the same idea as the Telethon session.

On Telegram this project chose a **user** session over a bot token, because a
user session can do what the human can. On TikTok the equivalent is a browser
session wearing the owner's cookies: same account, same permissions, no API key,
no app review, no region gate.

Three rules carried over unchanged from the rest of the project:

* **Login is manual, once.** TikTok's ticket guard (ECDSA P-256 + DPoP JWT) is
  not something to break, and breaking it would be the worse product: the owner
  types their password into TikTok's own page, and no credential ever passes
  through this code. What is stored afterwards is a cookie jar they can revoke
  from TikTok's device settings — exactly like the Telegram ``.session`` file.
* **TikTok rotates tokens while the run is alive** (``msToken``, ``ttwid``,
  csrf). Telegram's session string is stable forever; this jar is not. So the
  jar is re-saved during the run and carried to the successor, or the next run
  boots logged out. This is the single most likely way the chain breaks.
* **A jar is a bearer credential.** It is written 0600 from the first byte,
  never printed, never committed except through the same deliberate path the
  Telegram session already uses.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Account",
    "SessionError",
    "TikTokSessionStore",
    "redact",
]

#: Cookie names whose *values* are credentials. Names stay visible on purpose:
#: "which cookies did the export contain" is the question a debugging human has,
#: and the names are not secret.
_SENSITIVE = (
    "sessionid",
    "sid_tt",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
    "sid_tt_ss",
    "passport_csrf_token",
    "mstoken",
    "tt-target-idc-sign",
    "csrf",
    "password",
    "token",
)


class SessionError(Exception):
    """A session problem the owner can act on.

    ``kind`` is machine-readable so a command can explain itself without a
    traceback: ``no_session`` | ``expired`` | ``bad_file`` | ``budget``.
    """

    def __init__(self, message: str, *, kind: str = "invalid", hint: str = "") -> None:
        self.kind = kind
        self.hint = hint
        super().__init__(message)


def redact(value: Any) -> Any:
    """A copy safe to print: credential values become a length, nothing more."""

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            # The browser's cookie shape hides the secret in a *sibling* field:
            # ``{"name": "sessionid", "value": "…"}``. Redacting by key alone
            # printed the session id in full — found by a test, which is why the
            # test exists.
            cookie_name = str(item.get("name", "")).lower()
            out: dict[str, Any] = {}
            for key, inner in item.items():
                lowered = str(key).lower()
                sibling_secret = bool(cookie_name) and any(
                    marker in cookie_name for marker in _SENSITIVE
                )
                if any(marker in lowered for marker in _SENSITIVE) or (
                    sibling_secret and lowered in {"value", "content"}
                ):
                    out[key] = f"<redacted {len(str(inner))} chars>"
                else:
                    out[key] = walk(inner)
            return out
        if isinstance(item, list):
            return [walk(inner) for inner in item]
        return item

    return walk(value)


@dataclass
class Account:
    """One logged-in TikTok account, as the browser sees it."""

    cookies: list[dict[str, Any]] = field(default_factory=list)
    user_agent: str = ""
    created_at: float = 0.0
    last_seen: float = 0.0
    #: ``@handle`` from the logged-in profile, once known.
    handle: str = ""
    #: TikTok's numeric user id for this account — the owner id, in TikTok's
    #: terms. Known after the first inbox poll.
    user_id: str = ""
    #: Which surfaces this account's web session exposes. Measured, not assumed:
    #: rollout differs per account, and a feature that silently does nothing is
    #: worse than one that says "not on this account".
    surfaces: dict[str, bool] = field(default_factory=dict)
    notes: str = ""

    def cookie(self, name: str) -> str:
        for item in self.cookies:
            if item.get("name") == name:
                return str(item.get("value", ""))
        return ""

    def logged_in(self) -> bool:
        return bool(self.cookie("sessionid") or self.cookie("sessionid_ss"))

    def age_days(self, now: float | None = None) -> float:
        if not self.created_at:
            return 0.0
        return ((now if now is not None else time.time()) - self.created_at) / 86_400

    def summary(self) -> dict[str, Any]:
        return {
            "handle": self.handle or "(unknown)",
            "user_id": self.user_id or "(unknown)",
            "logged_in": self.logged_in(),
            "cookies": len(self.cookies),
            "age_days": round(self.age_days(), 1),
            "last_seen": self.last_seen or None,
            "surfaces": dict(self.surfaces),
            "user_agent": self.user_agent[:48],
        }


class TikTokSessionStore:
    """Load, save and refresh one account's jar, plus its tiny ledger."""

    def __init__(self, directory: Path | str, *, account: str = "default") -> None:
        self.account = account or "default"
        self.directory = Path(directory)
        self.path = self.directory / f"{self.account}.json"
        self.state_path = self.directory / f"{self.account}.state.json"

    # -- jar ---------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Account:
        if not self.path.exists():
            raise SessionError(
                "no TikTok session in this workspace",
                kind="no_session",
                hint="run `havoc tt login` locally, then commit .auth/tiktok/default.json",
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"session file unreadable: {exc}", kind="bad_file") from exc
        if not isinstance(raw, dict):
            raise SessionError("session file is not an object", kind="bad_file")
        account = Account(
            cookies=list(raw.get("cookies") or []),
            user_agent=str(raw.get("user_agent") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            last_seen=float(raw.get("last_seen") or 0.0),
            handle=str(raw.get("handle") or ""),
            user_id=str(raw.get("user_id") or ""),
            surfaces=dict(raw.get("surfaces") or {}),
            notes=str(raw.get("notes") or ""),
        )
        if not account.logged_in():
            raise SessionError(
                "the saved jar has no session cookie — TikTok signed it out",
                kind="expired",
                hint="run `havoc tt login` again; the old file is kept until a new one works",
            )
        return account

    def save(self, account: Account) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": account.cookies,
            "user_agent": account.user_agent,
            "created_at": account.created_at or time.time(),
            "last_seen": account.last_seen or time.time(),
            "handle": account.handle,
            "user_id": account.user_id,
            "surfaces": account.surfaces,
            "notes": account.notes,
        }
        tmp = self.path.with_suffix(".json.tmp")
        # 0600 from the first byte: a jar chmod-ed afterwards has already been
        # readable by everything on the box.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - Windows without POSIX modes
            pass

    # -- small state (counters that must survive a restart) -----------------
    def state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)
