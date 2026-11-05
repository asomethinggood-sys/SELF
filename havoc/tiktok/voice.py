"""What Yura says about what her hands just did, on TikTok.

Not a second personality. The lines come from the same place the Telegram side
gets them — :mod:`havoc.persona` for the face and the tails, and the AI persona
prompt when a model is available — and this module only picks which *shape* of
sentence a given outcome deserves.

Why a separate module at all: TikTok is text-only, 150 characters on a comment,
no markup, no keyboards, no expandable quotes. A line composed for Telegram comes
out with ``**`` and ``>>`` in it, which on TikTok reads as broken. So the shapes
are here, short, and the voice is still hers: a small case, a kaomoji, no brand
voice, no apology-for-being-an-AI.

``persona.build_line`` stays the source of variety for greetings; nothing about
the 1.9M-line bank changes. This file is the *receipt* layer.
"""

from __future__ import annotations

import random
from typing import Any

__all__ = ["Receipt", "receipt", "refusal", "challenge_notice", "smalltalk_faces"]

#: Faces she uses on TikTok. A subset of the Telegram set on purpose: TikTok
#: comments are read in a 9:16 feed, and a long kaomoji wraps into a mess.
_FACES = ("ᵕ̈", ">_<", "(¬‿¬)", "♡", "…", "ehe", "hm?", "!”")

#: The short forms of her receipts. ``{what}`` is filled with the thing or person.
_LINES: dict[str, tuple[str, ...]] = {
    "like": ("liked it. {face}", "gave it a heart {face}", "ok i liked that one",),
    "unlike": ("took the heart back", "unliked. changed my mind {face}",),
    "follow": ("followed @{what} {face}", "ok following @{what} now",),
    "unfollow": ("unfollowed @{what}. don't ask", "let @{what} go {face}",),
    "block": ("blocked @{what}. bye", "blocked. that's that {face}",),
    "comment": ("said it under the video {face}", "commented. was nice about it",),
    "upload": ("video's in drafts. add a sound for me?", "it's sitting in your drafts {face}"),
    "dm": ("sent {face}", "ok sent it"),
    "open": ("couldn't open that one {face}",),
}

#: When the daily allowance is gone. Said once, plainly, not repeated per message.
_REFUSALS: dict[str, tuple[str, ...]] = {
    "budget": (
        "i've done enough {what} today. tomorrow?",
        "that's my {what} limit for today, i'm not pushing it",
    ),
    "challenge": (
        "tiktok wants a human right now. i'm stopping, not fighting it",
        "there's a captcha in the way. clear it and i'll come back",
    ),
    "drift": (
        "the page changed on me. i don't want to click the wrong thing",
        "i don't recognise tiktok's layout anymore {face} stopping here",
    ),
}


class Receipt:
    """One thing her hands did, phrased for TikTok."""

    def __init__(self, action: str, target: str = "", *, ok: bool = True, rng: random.Random | None = None) -> None:
        self.action = action
        self.target = target
        self.ok = ok
        self.rng = rng or random.Random()

    def text(self) -> str:
        pool = _LINES.get(self.action) or (f"{self.action} done",)
        face = self.rng.choice(_FACES)
        line = self.rng.choice(pool)
        target = self.target.lstrip("@").split("/")[0] or "them"
        return line.format(face=face, what=target)[:150]

    def __str__(self) -> str:
        return self.text()


def receipt(action: str, target: str = "", *, ok: bool = True, rng: random.Random | None = None) -> str:
    """The one-line receipt for an action outcome."""
    return Receipt(action, target, ok=ok, rng=rng).text()


def refusal(kind: str, action: str = "", *, rng: random.Random | None = None) -> str:
    """What she says when she is *not* doing the thing, and why."""
    pool = _REFUSALS.get(kind) or ("not doing that one",)
    chooser = rng or random.Random()
    line = chooser.choice(pool)
    return line.format(what=action or "that", face=chooser.choice(_FACES))[:150]


def challenge_notice() -> str:
    """The stop message. Never softened into a retry."""
    return "tiktok asked for a human. i stopped the run — clear the challenge and restart."


def smalltalk_faces() -> tuple[str, ...]:
    return _FACES


def from_result(result: Any, *, target: str = "", rng: random.Random | None = None) -> str:
    """A receipt for an :class:`~havoc.tiktok.browser.ActionResult`.

    A failure never pretends to be a success, and a *challenge* is reported as
    its own thing even though the result is also a failure: "clear the captcha"
    and "TikTok changed its page" need different reactions from a human.
    """
    data = getattr(result, "data", {}) or {}
    if getattr(result, "ok", False):
        return receipt(getattr(result, "action", "done"), target, rng=rng)
    if data.get("challenge"):
        return refusal("challenge", rng=rng)
    if data.get("drift"):
        return refusal("drift", rng=rng)
    detail = (getattr(result, "detail", "") or "").strip()
    return (refusal("budget", getattr(result, "action", ""), rng=rng) if "allowance" in detail else detail)[:150]
