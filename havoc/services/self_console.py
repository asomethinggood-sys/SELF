"""Her own command line — the console she can read and pull.

Everything HAVOC can do is a :class:`havoc.command_registry.Command` with a name,
a usage string and a description. This module turns that registry into two things
a model can use:

* :func:`catalogue` — the whole list, written the way ``.help`` writes it, so she
  knows what exists, how to call it and what it takes;
* :func:`run` — an actual dispatch through the same :class:`Dispatcher` a typed
  command goes through, with the same permission checks, the same dry-run gate and
  the same feature switches.

That second part is the point. "yura fal page 166" is not a new ability with a new
implementation; it is the command the owner already has, called on his behalf.
Re-implementing twenty commands as tools would mean twenty places to be wrong and
two behaviours for one feature.

A short denylist keeps the recursion honest: she may not call the commands that
would call *her*, change who the owner is, or replace her key. Those stay a
human's business.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..event_engine import EventType, NormalizedEvent
from ..utils.parsing import ParseError, parse_command

__all__ = ["BLOCKED", "ConsoleReply", "catalogue", "run"]

#: Commands a tool may not call: the ones that would re-enter her, change
#: ownership, or swap her credentials. Everything else is fair game.
BLOCKED: frozenset[str] = frozenset(
    {
        "yura", "person", "self",          # would call her again, mid-answer
        "help", "commands", "start",       # menus, not work
        "owner", "key", "prefix",          # who may do this is not hers to change
        "stop", "dryrun", "restart",       # the emergency switches
        "alias", "plugins",                # changing the command set itself
    }
)
#: A catalogue line is a prompt, not a manual page. Long descriptions are cut.
MAX_DESCRIPTION = 90
#: The whole catalogue is capped: past this the prompt is a phone book.
MAX_CATALOGUE_CHARS = 6500


@dataclass
class ConsoleReply:
    """What a dispatched command did, as far as she can tell."""

    ok: bool
    command: str = ""
    note: str = ""
    kind: str = "ok"
    #: What the command itself sent, so she can react to it instead of guessing.
    said: list[str] = field(default_factory=list)


@dataclass
class _ConsoleMessage:
    """The smallest object a command handler can be run against.

    Handlers read ``.id``, ``.chat_id``, ``.sender_id``, ``.text``, ``.media`` and
    optionally ``get_reply_message()``. Nothing here is a mock of Telethon for its
    own sake: it is the four fields the command layer actually touches, so a
    command that reaches for more than that fails loudly in a test instead of
    quietly in production.
    """

    id: int
    chat_id: int
    sender_id: int
    text: str = ""
    out: bool = False
    is_private: bool = False
    is_group: bool = False
    media: Any = None
    reply_to_msg_id: Any = None

    async def get_reply_message(self) -> Any:
        return None


class _CapturingSender:
    """Wraps the real sender and remembers what a command said.

    The command still goes out to the chat — a command that draws a Hafez card
    should draw it — but she also gets the text, so her answer can refer to the
    thing that just happened rather than to "something".
    """

    def __init__(self, inner: Any, lines: list[str]) -> None:
        self._inner = inner
        self._lines = lines

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _simulate(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # A command forces ``dry_run=False`` on its own reply (a command's answer
        # is never a side effect). In a dry run that would try to reach Telegram
        # through a client that may not exist — so the simulation is restored
        # here, one layer up, where it belongs.
        if getattr(self._inner, "dry_run", False):
            kwargs["dry_run"] = True
        return kwargs

    async def respond(self, event: Any, text: str, **kwargs: Any) -> Any:
        self._lines.append(_plain(text)[:400])
        return await self._inner.respond(event, text, **self._simulate(kwargs))

    async def send(self, chat_id: Any, text: str, **kwargs: Any) -> Any:
        self._lines.append(_plain(text)[:400])
        return await self._inner.send(chat_id, text, **self._simulate(kwargs))

    async def send_media(self, chat_id: Any, media: Any, **kwargs: Any) -> Any:
        self._lines.append(f"[media] {getattr(media, 'name', media)}")
        if getattr(self._inner, "dry_run", False):
            kwargs["dry_run"] = True
        try:
            return await self._inner.send_media(chat_id, media, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a picture is not worth an answer
            self._lines.append(f"[media failed: {type(exc).__name__}]")
            return None


class _Proxy:
    """``runtime`` with a different sender. Everything else is the real object."""

    def __init__(self, runtime: Any, sender: Any) -> None:
        self._runtime = runtime
        self.sender = sender

    def __getattr__(self, name: str) -> Any:
        return getattr(self._runtime, name)


def _plain(text: str) -> str:
    """Strip the HTML a command formats with, for her eyes only."""
    out, depth = [], 0
    for char in str(text):
        if char == "<":
            depth += 1
            continue
        if char == ">":
            if depth:
                depth -= 1
                continue
            out.append(char)
            continue
        if not depth:
            out.append(char)
    return "".join(out).strip()


def catalogue(runtime: Any, language: str = "en", *, full: bool = False) -> str:
    """Every command she may call, one line each, grouped by category.

    ``full`` gives descriptions and usages; the prompt version is the same list
    with shorter text, because a longer prompt is not a smarter one.
    """
    registry = getattr(runtime, "registry", None)
    if registry is None:
        return ""
    prefix = getattr(runtime, "prefix", ".")
    rows: list[str] = []
    seen = 0
    for command in registry.all():
        if command.hidden or command.name in BLOCKED:
            continue
        usage = command.usage.strip()
        line = f"{prefix}{command.name}"
        if usage:
            line += f" {usage}"
        if full:
            description = ""
            try:
                description = str(runtime.t(command.description_key, language))
            except Exception:  # noqa: BLE001 - a plugin may have no locale key
                description = command.description or ""
            description = " ".join(description.split())[:MAX_DESCRIPTION]
            if description:
                line += f" — {description}"
        rows.append(line)
        seen += 1
    if not rows:
        return ""
    header = "## her own commands" if language != "fa" else "## دستورهای خودش"
    body = "\n".join("- " + row for row in rows)
    if len(body) > MAX_CATALOGUE_CHARS:
        body = body[:MAX_CATALOGUE_CHARS].rsplit("\n", 1)[0]
    if language == "fa":
        tail = (
            "این‌ها را می‌توانی با ابزار `command` صدا بزنی، دقیقاً همان‌طور که او تایپ می‌کند. "
            "مثلاً «fal page 166» یا «clock on -- timezone Asia/Tehran». "
            "اگر کسی از تو چیزی خواست که یکی از این‌ها جوابش است، لازم نیست توضیح بدهی — انجامش بده."
        )
    else:
        tail = (
            "call any of these through the `command` tool, exactly as he would type "
            "them — e.g. \"fal page 166\", \"clock on -- timezone Asia/Tehran\". if a "
            "request is answered by one of these, do it instead of explaining it."
        )
    return f"{header}\n{body}\n\n{tail}"


async def run(
    runtime: Any,
    line: str,
    *,
    chat_id: int,
    sender_id: int,
    message_id: int | None = None,
    language: str = "en",
    is_private: bool = False,
    reply_to: int | None = None,
) -> ConsoleReply:
    """Dispatch ``line`` (``fal page 166``) as the owner, through the real path."""
    text = " ".join(str(line or "").split())
    if not text:
        return ConsoleReply(False, note="nothing to run", kind="bad_args")
    prefix = getattr(runtime, "prefix", ".")
    bare = text[1:] if text.startswith(prefix) else text
    name = bare.split(" ", 1)[0].lower()
    if name in BLOCKED:
        return ConsoleReply(False, name, note=f"{name} is not hers to call", kind="blocked")
    command = runtime.registry.resolve(name)
    if command is None:
        return ConsoleReply(False, name, note=f"there is no command called {name}", kind="unknown")
    if not command.owner_only:
        # A public command is still fine; the check is here so a future public
        # command cannot quietly become a hand.
        pass
    try:
        parsed = parse_command(f"{prefix}{bare}", prefix)
    except ParseError as exc:
        return ConsoleReply(False, name, note=f"could not parse that: {exc}", kind="bad_args")
    if parsed is None:
        return ConsoleReply(False, name, note="not a command", kind="bad_args")

    said: list[str] = []
    proxy = _Proxy(runtime, _CapturingSender(runtime.sender, said))
    # ``out=True`` is the truth: this is the account acting, not a stranger
    # typing. Without it the dispatcher asked "is this sender the owner?" about a
    # sender id of 0 and every command she ran for him answered "restricted to
    # the account owner" — the bug he photographed.
    self_id = int(sender_id or 0) or int(getattr(getattr(runtime, "permissions", None), "self_id", 0) or 0)
    event = NormalizedEvent(
        type=EventType.MESSAGE,
        chat_id=int(chat_id),
        message_id=message_id,
        sender_id=self_id,
        timestamp=time.time(),
        text=f"{prefix}{bare}",
        language=language,
        is_outgoing=True,
        is_private=bool(is_private),
        is_group=not is_private,
        # "react to his message": the console is the hand, so it has to know
        # which message was pointed at — a hand with no target is why a plain
        # "به پیامش قلب واکنش بده" did nothing.
        reply_to_id=int(reply_to) if reply_to else None,
        is_reply=bool(reply_to),
    )
    message = _ConsoleMessage(
        id=int(message_id or 0),
        chat_id=int(chat_id),
        sender_id=self_id,
        text=f"{prefix}{bare}",
        out=True,
        is_private=bool(is_private),
        is_group=not is_private,
        reply_to_msg_id=int(reply_to) if reply_to else None,
    )
    from ..command_registry import Dispatcher

    dispatcher = Dispatcher(proxy)
    try:
        ran = await dispatcher.handle(event, message, parsed, language)
    except Exception as exc:  # noqa: BLE001 - a command that throws is a sentence
        return ConsoleReply(False, name, note=f"{type(exc).__name__}: {exc}", kind="error")
    note = " · ".join(said[:2]) if said else ("ran" if ran else "did not run")
    return ConsoleReply(bool(ran), name, note=note[:400], kind="ok" if ran else "noop", said=said)
