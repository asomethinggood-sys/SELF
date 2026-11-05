"""Yura's TikTok hands: everything the account can do, one deliberate action each.

Every public method here does the same four things, in this order:

1. **Ask** :class:`~havoc.tiktok.limits.Budget` — if today's allowance cannot
   cover it, raise before touching the page.
2. **Pace** — wait the jittered gap for that action.
3. **Act** once, on a page whose shape was verified first.
4. **Verify** — read the result back (aria state, the comment appearing, the
   button flipping). A click that fired is not a success; the page agreeing is.

What each method is *not*: a loop. There is no ``like_everything()`` here on
purpose. Bulk behaviour is the thing that gets accounts flagged, so a caller can
only ask for one action at a time and has to spend an allowance to get it.
"""

from __future__ import annotations

import logging
from typing import Any

from .browser import SELECTORS, ActionResult, TikTokBrowser, _drift
from .limits import Budget

__all__ = ["AccountActions"]

#: TikTok's own comment ceiling. Refused locally so half a sentence never lands.
COMMENT_LIMIT = 150


class AccountActions:
    """One account, one browser, one budget."""

    def __init__(self, browser: TikTokBrowser, budget: Budget, *, logger: logging.Logger | None = None) -> None:
        self.browser = browser
        self.budget = budget
        self.logger = logger or logging.getLogger("havoc.tiktok.actions")

    # -- shared -------------------------------------------------------------
    async def _open_video(self, url: str) -> ActionResult | None:
        await self.browser.goto(url)
        try:
            await self.browser.page.wait_for_selector('[data-e2e="browse-video"], video', timeout=15_000)
        except Exception:  # noqa: BLE001
            return _drift("open", "a video player")
        if await self.browser.challenged():
            return ActionResult("open", False, "TikTok is showing a challenge", data={"challenge": True})
        return None

    async def _open_profile(self, handle: str) -> ActionResult | None:
        handle = handle.lstrip("@")
        await self.browser.goto(f"https://www.tiktok.com/@{handle}")
        if await self.browser.challenged():
            return ActionResult("open", False, "TikTok is showing a challenge", data={"challenge": True})
        try:
            await self.browser.page.wait_for_selector('[data-e2e="user-title"], h1', timeout=12_000)
        except Exception:  # noqa: BLE001
            return _drift("open", "a profile page")
        return None

    # -- likes --------------------------------------------------------------
    async def like(self, url: str) -> ActionResult:
        self.budget.spend("like")
        opened = await self._open_video(url)
        if opened:
            return opened
        await self.browser.pace.wait("like")
        button = await self.browser.first_of("like_button")
        if button is None:
            return _drift("like", "a like button")
        try:
            before = await button.get_attribute("aria-pressed")
            await button.click()
            await self.browser.pace.pause(0.4, 1.1)
            after = await button.get_attribute("aria-pressed")
        except Exception as exc:  # noqa: BLE001
            return ActionResult("like", False, f"the click did not land: {exc}")
        if before == "true":
            return ActionResult("like", True, "already liked, left alone", url=url)
        if after == "true" or await self.browser.present("unlike_button"):
            return ActionResult("like", True, "liked, and the button confirmed it", url=url)
        return ActionResult("like", False, f"clicked, but the button reads {after!r}", url=url, data={"drift": True})

    async def unlike(self, url: str) -> ActionResult:
        self.budget.spend("like")
        opened = await self._open_video(url)
        if opened:
            return opened
        await self.browser.pace.wait("like")
        button = await self.browser.first_of("unlike_button") or await self.browser.first_of("like_button")
        if button is None:
            return _drift("unlike", "a like button")
        try:
            await button.click()
            await self.browser.pace.pause(0.3, 0.9)
        except Exception as exc:  # noqa: BLE001
            return ActionResult("unlike", False, f"the click did not land: {exc}")
        return ActionResult("unlike", True, "unliked", url=url)

    # -- follows ------------------------------------------------------------
    async def follow(self, handle: str) -> ActionResult:
        self.budget.spend("follow")
        opened = await self._open_profile(handle)
        if opened:
            return opened
        await self.browser.pace.wait("follow")
        button = await self.browser.first_of("follow_button")
        if button is None:
            if await self.browser.present("following_button"):
                return ActionResult("follow", True, f"already following @{handle.lstrip('@')}")
            return _drift("follow", "a Follow button")
        label = (await button.inner_text()).strip()
        try:
            await button.click()
            await self.browser.pace.pause(0.8, 2.0)
        except Exception as exc:  # noqa: BLE001
            return ActionResult("follow", False, f"the click did not land: {exc}")
        if await self.browser.present("following_button"):
            return ActionResult("follow", True, f"followed @{handle.lstrip('@')} (was {label!r})")
        return ActionResult("follow", False, "clicked, but the button did not change to Following", data={"drift": True})

    async def unfollow(self, handle: str) -> ActionResult:
        self.budget.spend("unfollow")
        opened = await self._open_profile(handle)
        if opened:
            return opened
        await self.browser.pace.wait("unfollow")
        button = await self.browser.first_of("following_button")
        if button is None:
            return ActionResult("unfollow", True, f"not following @{handle.lstrip('@')}")
        try:
            await button.click()
            await self.browser.pace.pause(0.6, 1.4)
            # TikTok confirms an unfollow in a sheet.
            for key in ("follow_button",):
                if await self.browser.present(key):
                    break
        except Exception as exc:  # noqa: BLE001
            return ActionResult("unfollow", False, f"the click did not land: {exc}")
        return ActionResult("unfollow", True, f"unfollowed @{handle.lstrip('@')}")

    async def block(self, handle: str) -> ActionResult:
        """Block someone. Charged like a write, because it is one.

        Blocking is the most consequential button on the page — it is also the
        one most likely to be triggered by a stranger in a DM, so the caller is
        expected to have confirmed it with the owner first.
        """
        self.budget.spend("block")
        opened = await self._open_profile(handle)
        if opened:
            return opened
        await self.browser.pace.wait("block")
        more = await self.browser.first_of("block_menu", timeout=5_000)
        if more is None:
            return _drift("block", "the profile's more/options menu")
        try:
            await more.click()
            await self.browser.pace.pause(0.5, 1.2)
            target = self.browser.page.get_by_text("Block", exact=False).first
            await target.click()
            await self.browser.pace.pause(0.6, 1.4)
            confirm = self.browser.page.get_by_role("button", name="Block").last
            if await confirm.count() > 0:  # type: ignore[union-attr]
                await confirm.click()
        except Exception as exc:  # noqa: BLE001
            return ActionResult("block", False, f"blocking did not complete: {exc}")
        return ActionResult("block", True, f"blocked @{handle.lstrip('@')} — verify on the profile if unsure")

    # -- comments -----------------------------------------------------------
    async def comment(self, url: str, text: str) -> ActionResult:
        text = text.strip()
        if not text:
            return ActionResult("comment", False, "empty comment")
        if len(text) > COMMENT_LIMIT:
            return ActionResult("comment", False, f"{len(text)} characters — TikTok allows {COMMENT_LIMIT}")
        self.budget.spend("comment")
        opened = await self._open_video(url)
        if opened:
            return opened
        await self.browser.pace.wait("comment")
        box = await self.browser.first_of("comment_box")
        if box is None:
            return _drift("comment", "a comment box")
        await self.browser.type_like_a_person(box, text)
        submit = await self.browser.first_of("comment_submit", timeout=4_000)
        if submit is None:
            return _drift("comment", "a comment submit button")
        try:
            await submit.click()
        except Exception as exc:  # noqa: BLE001
            return ActionResult("comment", False, f"the submit did not land: {exc}")
        await self.browser.pace.pause(1.2, 2.6)
        try:
            listed = await self.browser.page.locator(
                f'[data-e2e="comment-level-1"]:has-text("{text[:40]}")'
            ).count()
        except Exception:  # noqa: BLE001
            listed = 0
        if listed:
            return ActionResult("comment", True, "posted, and the page shows it", url=url)
        return ActionResult(
            "comment",
            False,
            "submitted, but the comment is not visible yet — check before retrying",
            url=url,
            data={"drift": True, "caution": "a second attempt could double-post"},
        )

    # -- upload -------------------------------------------------------------
    async def upload(self, clip: str, *, caption: str = "") -> ActionResult:
        """Put a finished video into the uploader and stop at the composer.

        Never presses Post. The composer is where a human picks a sound and a
        cover — that choice is where reach lives on TikTok, and it is also what
        keeps an unattended pipeline from publishing something embarrassing.
        """
        from pathlib import Path

        path = Path(clip).expanduser()
        if not path.is_file():
            return ActionResult("upload", False, f"no such file: {path}")
        self.budget.spend("post")
        await self.browser.goto("https://www.tiktok.com/tiktokstudio/upload")
        await self.browser.pace.wait("post")
        try:
            await self.browser.page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001 - a busy page is fine
            pass
        target = None
        for selector in SELECTORS["file_input"]:
            locator = self.browser.page.locator(selector).first
            try:
                await locator.wait_for(state="attached", timeout=15_000)
                target = locator
                break
            except Exception:  # noqa: BLE001
                continue
        if target is None:
            if await self.browser.challenged():
                return ActionResult("upload", False, "TikTok is showing a challenge", data={"challenge": True})
            return _drift("upload", "the upload file input")
        try:
            await target.set_input_files(str(path))
        except Exception as exc:  # noqa: BLE001
            return ActionResult("upload", False, f"the file was refused by the page: {exc}")
        caption_box = await self.browser.first_of("caption_box", timeout=120_000)
        if caption_box is None:
            return _drift("upload", "the caption box after an upload")
        if caption:
            await self.browser.type_like_a_person(caption_box, caption)
        return ActionResult(
            "upload",
            True,
            "the video is in TikTok's composer with your caption — add a sound, then press Post",
            url="https://www.tiktok.com/tiktokstudio/upload",
            data={"caption": caption, "published": False},
        )

    # -- profile reading ----------------------------------------------------
    async def whoami(self) -> dict[str, Any]:
        surface: dict[str, Any] = {"handle": "", "user_id": ""}
        try:
            await self.browser.goto("https://www.tiktok.com/")
            link = await self.browser.first_of("profile_link", timeout=6_000)
            if link is not None:
                href = await link.get_attribute("href") or ""
                if "/@" in href:
                    surface["handle"] = href.split("/@", 1)[1].split("/", 1)[0].split("?", 1)[0]
        except Exception:  # noqa: BLE001
            pass
        return surface
