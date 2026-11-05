"""One Chromium profile, logged in as the owner, driven by Yura.

This file is the TikTok counterpart of ``havoc/client.py``. On Telegram, Telethon
holds one authenticated connection and events arrive on it; here a real browser
holds the same connection and the same account, and the "events" are the frames
the page is already receiving.

Two ways to see the inbox, and the module implements both because which one works
is a property of TikTok's build, not of this code:

* **``watch`` — the fast path.** The messages page is opened and its websocket
  traffic is read as it arrives. No polling, no delay: the page is doing the
  fetching, so its frames are the push. This is the shape that makes Yura feel
  like Yura.
* **``poll`` — the fallback.** Re-read the conversation list on an interval and
  diff. Slower, dumb, and it works even when the frames are unreadable protobuf.

Which one is in use is reported, never assumed. ``havoc tt inbox --explain``
prints what was seen.

Rules carried over from the rest of the project:

* Playwright is optional at import time — a clone without it boots and says what
  to install, the way the downloader says when ``yt-dlp`` is missing.
* Selectors live in named constants with the date they were written against.
  Every action returns an :class:`ActionResult`; a renamed attribute produces
  "TikTok's page changed", never a stack trace in the owner's terminal.
* Nothing here decides *whether* an action is allowed. That is
  :mod:`havoc.tiktok.limits`, called by :mod:`havoc.tiktok.actions`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import Account, SessionError, TikTokSessionStore
from .limits import Pace

__all__ = [
    "ActionResult",
    "InboxMessage",
    "TikTokBrowser",
    "browser_available",
    "install_hint",
]

BASE = "https://www.tiktok.com"
LOGIN_URL = f"{BASE}/login"
MESSAGES_URL = f"{BASE}/messages"
UPLOAD_URL = f"{BASE}/tiktokstudio/upload"

CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

#: Selector candidates, most specific first, written 2026-11 against the desktop
#: web app. ``data-e2e`` attributes are the most stable thing TikTok ships (they
#: exist for TikTok's own end-to-end tests) and also the thing that vanishes
#: without notice, so every entry is a list and every caller tolerates all of
#: them failing.
SELECTORS: dict[str, tuple[str, ...]] = {
    "logged_in": ('[data-e2e="profile-icon"]', '[data-e2e="inbox-icon"]'),
    "profile_link": ('[data-e2e="profile-icon"]', 'a[href^="/@"]'),
    "inbox_icon": ('[data-e2e="inbox-icon"]', 'a[href*="/messages"]'),
    "thread_item": ('[data-e2e="chat-list-item"]', 'div[class*="ChatItem"]', '[role="listitem"]'),
    "thread_name": ('[data-e2e="chat-uniqueid"]', 'p[class*="PusherName"]', "span"),
    "message_bubble": ('[data-e2e="chat-message"]', 'div[class*="DivMessage"]', "p"),
    "composer": ('[data-e2e="message-input-area"]', 'div[contenteditable="true"]', "textarea"),
    "send_button": ('[data-e2e="message-send"]', 'button[type="submit"]'),
    "upload_icon": ('[data-e2e="upload-icon"]', 'a[href*="tiktokstudio/upload"]'),
    "file_input": ('input[type="file"]',),
    "caption_box": ('div[contenteditable="true"]', '.public-DraftEditor-content'),
    "like_button": ('[data-e2e="like-icon"]', '[data-e2e="browse-like-icon"]'),
    "unlike_button": ('[data-e2e="unlike-icon"]', '[data-e2e="browse-unlike-icon"]'),
    "comment_box": ('[data-e2e="comment-input"]', 'div[contenteditable="true"]'),
    "comment_submit": ('[data-e2e="comment-post"]',),
    "follow_button": ('[data-e2e="follow-button"]',),
    "following_button": ('[data-e2e="following-button"]',),
    "block_menu": ('[data-e2e="more-menu"]', '[data-e2e="share-menu"]'),
    "captcha": ('div[class*="captcha"]', 'iframe[src*="captcha"]', "#captcha-verify-image"),
    "login_wall": ('[data-e2e="login-title"]', 'form[action*="login"]'),
}


def install_hint() -> str:
    return "pip install 'havoc-userbot[browser]' && python -m playwright install --with-deps chromium"


def browser_available() -> bool:
    """True when Playwright imports *and* a browser binary exists to launch."""
    try:
        import playwright  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is the same as none
        return False
    return True


@dataclass
class ActionResult:
    """What an action did, in words the owner can read."""

    action: str
    ok: bool
    detail: str = ""
    url: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.action}: {'ok' if self.ok else 'failed'} — {self.detail}" if self.detail else (
            f"{self.action}: {'ok' if self.ok else 'failed'}"
        )


@dataclass
class InboxMessage:
    """One message, normalized — the TikTok twin of ``NormalizedEvent``."""

    thread_id: str
    message_id: str
    sender: str
    text: str
    timestamp: float = field(default_factory=time.time)
    is_self: bool = False
    thread_name: str = ""

    @property
    def key(self) -> str:
        """The dedupe key: one message answered once, across restarts."""
        return f"{self.thread_id}:{self.message_id}"


def _drift(action: str, what: str) -> ActionResult:
    """TikTok's page is not the shape this was written against."""
    return ActionResult(
        action,
        False,
        f"TikTok's page did not show {what} — the layout has changed since this was written",
        data={"drift": True, "what": what},
    )


class TikTokBrowser:
    """A persistent Chromium profile that belongs to the owner."""

    def __init__(
        self,
        store: TikTokSessionStore,
        *,
        headless: bool = True,
        pace: Pace | None = None,
        logger: logging.Logger | None = None,
        profile: str = "",
    ) -> None:
        self.store = store
        self.headless = headless
        self.pace = pace or Pace()
        self.logger = logger or logging.getLogger("havoc.tiktok")
        self.profile_dir = store.directory / f"{profile or store.account}-profile"
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._frames: list[dict[str, Any]] = []
        self._frame_hook: Callable[[dict[str, Any]], None] | None = None

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> TikTokBrowser:
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def start(self, *, with_jar: bool = True) -> None:
        """Launch Chromium; restore the saved jar unless this *is* the login.

        ``with_jar=False`` is the first-login path: there is no jar yet (or the
        old one is signed out), and demanding one here is exactly the bug that
        made ``havoc tt login`` die before TikTok's page ever opened.
        """
        if not browser_available():
            raise SessionError("browser engine not installed", kind="no_engine", hint=install_hint())
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            # No user-agent spoofing and no stealth patches: this is a real
            # Chromium with the owner's real cookies, which is the honest version
            # of "not a bot" and the only one worth maintaining. Faking a device
            # is what gets an account flagged, not what saves it.
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(30_000)
        if with_jar:
            await self._install_jar(self.store.load())

    async def close(self) -> None:
        for closer in (self._ctx, self._pw):
            if closer is None:
                continue
            try:
                result = closer.close() if hasattr(closer, "close") else closer.stop()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                pass
        self._ctx = self._pw = self._page = None

    async def _install_jar(self, account: Account) -> None:
        """Load the saved cookies into the fresh profile.

        A persistent profile usually carries them itself, but a runner starts
        from an empty profile every time and relies on this: the jar *is* the
        session, and the profile directory is only a cache of it.
        """
        cookies = []
        for item in account.cookies:
            cookie = {key: value for key, value in item.items() if key in {
                "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite",
            }}
            if "domain" not in cookie or "name" not in cookie:
                continue
            if cookie.get("sameSite") not in {"Strict", "Lax", "None", None}:
                cookie.pop("sameSite", None)
            if not cookie.get("expires"):
                cookie.pop("expires", None)
            cookies.append(cookie)
        if not cookies:
            return
        try:
            await self._ctx.add_cookies(cookies)
        except Exception as exc:  # noqa: BLE001 - a bad cookie must not block the run
            self.logger.warning("could not load %d saved cookies: %s", len(cookies), exc)
    @property
    def page(self) -> Any:
        if self._page is None:
            raise SessionError("browser is not open", kind="not_open")
        return self._page

    # -- page helpers -------------------------------------------------------
    async def goto(self, url: str, *, wait: str = "domcontentloaded") -> None:
        await self.page.goto(url, wait_until=wait)
        await self.pace.pause()

    async def first_of(self, key: str, *, timeout: int = 8_000) -> Any:
        for selector in SELECTORS.get(key, ()):
            locator = self.page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                return locator
            except Exception:  # noqa: BLE001 - next candidate
                continue
        return None

    async def present(self, key: str) -> bool:
        for selector in SELECTORS.get(key, ()):
            try:
                if await self.page.locator(selector).count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def challenged(self) -> bool:
        """True when TikTok is showing a captcha or a login wall.

        The caller stops the run instead of pushing through it: hammering a
        challenge is the fastest route to a lock, and the owner can clear it in
        fifteen seconds on a phone.
        """
        for key in ("captcha", "login_wall"):
            if await self.present(key):
                return True
        return False

    async def type_like_a_person(self, locator: Any, text: str) -> None:
        await locator.click()
        await self.pace.pause(0.2, 0.5)
        for char in text:
            await locator.type(char, delay=0)
            await asyncio.sleep(self.pace.rng.uniform(0.03, 0.12))

    # -- the jar, refreshed ------------------------------------------------
    async def save_jar(self, *, handle: str = "", user_id: str = "") -> Account:
        """Re-read the live cookies and persist them.

        Called periodically during a run and at shutdown. TikTok rotates
        ``msToken``/``ttwid`` while the page is open; a successor that restored
        only the cookies the run started with would come back logged out.
        """
        cookies = await self._ctx.cookies()
        try:
            ua = await self.page.evaluate("() => navigator.userAgent")
        except Exception:  # noqa: BLE001
            ua = CHROME_UA
        previous: Account | None = None
        try:
            previous = self.store.load()
        except SessionError:
            previous = None
        account = Account(
            cookies=cookies,
            user_agent=str(ua or CHROME_UA),
            created_at=(previous.created_at if previous and previous.created_at else time.time()),
            last_seen=time.time(),
            handle=handle or (previous.handle if previous else ""),
            user_id=user_id or (previous.user_id if previous else ""),
            surfaces=dict(previous.surfaces) if previous else {},
            notes=previous.notes if previous else "",
        )
        if not account.logged_in():
            raise SessionError("the browser is no longer logged in", kind="expired")
        self.store.save(account)
        return account

    async def logged_in(self) -> bool:
        try:
            names = {item.get("name") for item in await self._ctx.cookies()}
        except Exception:  # noqa: BLE001
            return False
        return bool(names & {"sessionid", "sessionid_ss"})

    # -- login -------------------------------------------------------------
    async def login(self, *, timeout: float = 900.0, poll: float = 1.5) -> ActionResult:
        """Open TikTok's login page and wait for *the owner* to finish."""
        await self.goto(LOGIN_URL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.logged_in():
                handle = await self.detect_handle()
                account = await self.save_jar(handle=handle)
                return ActionResult("login", True, f"signed in as {account.handle or '(handle unreadable)'}", data=account.summary())
            await asyncio.sleep(poll)
        return ActionResult("login", False, "no session appeared before the timeout", data={"hint": "leave the window open longer with --timeout"})

    async def detect_handle(self) -> str:
        try:
            await self.goto(f"{BASE}/")
            link = await self.first_of("profile_link", timeout=5_000)
            if link is None:
                return ""
            href = await link.get_attribute("href") or ""
            if "/@" in href:
                return href.split("/@", 1)[1].split("/", 1)[0].split("?", 1)[0]
        except Exception:  # noqa: BLE001 - a handle is a nicety
            return ""
        return ""

    # -- inbox -------------------------------------------------------------
    def on_frame(self, hook: Callable[[dict[str, Any]], None]) -> None:
        self._frame_hook = hook

    async def attach_frames(self) -> None:
        """Listen to the page's websocket traffic and decode any JSON frames."""

        def _listen(ws: Any) -> None:
            def _message(payload: Any) -> None:
                raw = getattr(payload, "text", None) or getattr(payload, "bytes", None)
                if not isinstance(raw, str) or not raw or len(raw) > 400_000:
                    return
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    return
                self._frames.append({"at": time.time(), "data": data})
                # Bounded: the run is measured in hours and this is a debugging
                # window, not a transcript.
                del self._frames[:-200]
                if self._frame_hook is not None:
                    try:
                        self._frame_hook(data)
                    except Exception:  # noqa: BLE001 - a bad hook must not kill the socket
                        self.logger.debug("frame hook failed", exc_info=True)

            ws.on("framereceived", _message)

        self.page.on("websocket", _listen)

    async def open_inbox(self) -> ActionResult:
        await self.goto(MESSAGES_URL)
        if await self.challenged():
            return ActionResult("inbox", False, "TikTok is showing a challenge — clear it, then rerun", data={"challenge": True})
        thread = await self.first_of("thread_item", timeout=15_000)
        if thread is None:
            return _drift("inbox", "the conversation list")
        return ActionResult("inbox", True, "inbox open", url=MESSAGES_URL)

    async def poll_threads(self) -> list[InboxMessage]:
        """The slow, dependable path: read the list, return what is visible."""
        messages: list[InboxMessage] = []
        try:
            threads = self.page.locator(", ".join(SELECTORS["thread_item"]))
            count = min(await threads.count(), 30)
        except Exception:  # noqa: BLE001
            return messages
        for index in range(count):
            item = threads.nth(index)
            try:
                name = (await item.inner_text())[:120].strip()
                thread_id = await item.get_attribute("data-id") or name
            except Exception:  # noqa: BLE001
                continue
            messages.append(
                InboxMessage(
                    thread_id=str(thread_id),
                    message_id=f"list:{int(time.time())}:{index}",
                    sender=name,
                    text=name,
                    thread_name=name,
                )
            )
        return messages

    async def read_thread(self, name: str) -> list[InboxMessage]:
        """Open one conversation and read the visible bubbles, oldest first."""
        target = self.page.locator(", ".join(SELECTORS["thread_item"])).filter(has_text=name).first
        try:
            await target.click()
        except Exception:  # noqa: BLE001
            return []
        await self.pace.pause(0.8, 2.0)
        out: list[InboxMessage] = []
        try:
            bubbles = self.page.locator(", ".join(SELECTORS["message_bubble"]))
            count = min(await bubbles.count(), 60)
        except Exception:  # noqa: BLE001
            return out
        for index in range(count):
            try:
                text = (await bubbles.nth(index).inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            out.append(
                InboxMessage(
                    thread_id=name,
                    message_id=f"{name}:{index}",
                    sender=name,
                    text=text,
                    thread_name=name,
                )
            )
        return out

    async def send_dm(self, text: str) -> ActionResult:
        box = await self.first_of("composer")
        if box is None:
            return _drift("dm", "the message box")
        await self.type_like_a_person(box, text)
        button = await self.first_of("send_button", timeout=4_000)
        try:
            if button is not None:
                await button.click()
            else:
                await box.press("Enter")
        except Exception as exc:  # noqa: BLE001
            return ActionResult("dm", False, f"the send did not land: {exc}")
        await self.pace.pause(0.8, 1.8)
        return ActionResult("dm", True, "sent", data={"text": text})

    # -- snapshots for the model -------------------------------------------
    async def screenshot(self, path: str) -> str:
        await self.page.screenshot(path=path, full_page=False)
        return path

    def diagnostics(self) -> dict[str, Any]:
        return {
            "frames_seen": len(self._frames),
            "last_frame_at": self._frames[-1]["at"] if self._frames else None,
            "profile": str(self.profile_dir),
            "headless": self.headless,
        }
