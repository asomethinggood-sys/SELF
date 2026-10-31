"""Her hands — one action layer, and the leash on it.

An agent is memory, permissions and hands. She has had the first two for a while
(this is a companion with a lesson book and a person book); this module is the
third, and it exists mostly to make the third boring.

Everything she can *do* is a :class:`Tool`: a name, a JSON schema, a scope, a
risk, and one async function. Nothing is executed by the model. The model asks;
:class:`ActionLayer` checks, writes the attempt down, and only then runs it.

The policy, in one place so it can be argued with:

``read``
    free. Looking at something she already had access to is not an action.
``write``
    free by default (:data:`AUTO_SETTING`). Setting a reminder or turning her own
    clock off is a small thing; asking a yes/no question for it once, let alone
    every time, is a form, not a companion. The owner is the only one who can
    reach this layer at all, so the leash that matters is the one below.
``send``
    free by default for the same reason — she is already allowed to reply.
``delete``
    never silent, never remembered. Every single time, every single item, even
    with auto on. This is the one thing where a mistake is not reversible.
``money`` / ``identity``
    refused. Not confirmed — refused.
    There is no button for this, no "advanced" setting, and no amount of asking
    in the chat changes it.

Budgets are facts, not suggestions: :data:`MAX_CALLS_PER_TURN` per turn,
:data:`MAX_ACTIONS_PER_HOUR` per hour, and a loop breaker — the same tool with the
same arguments twice in one turn stops the loop and says so. All three are
counted from the audit table, so a restart is not a way around them.

One honest limitation: a Telegram *user* account cannot attach an inline
keyboard — ``reply_markup`` is a bot-only feature. The confirm is therefore a
typed ``.yura yes`` / ``.yura no`` with the same two-minute window, which is the
closest thing the platform actually allows.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..utils.time import utc_now

__all__ = [
    "ActionError",
    "ActionLayer",
    "ActionResult",
    "Tool",
    "ToolCall",
    "Turn",
    "SCOPES",
    "REFUSED_SCOPES",
    "MAX_CALLS_PER_TURN",
    "MAX_ACTIONS_PER_HOUR",
    "CONFIRM_WINDOW",
    "parse_calls",
]

#: What a tool is allowed to touch. The order is the order of severity.
SCOPES = ("read", "write", "send", "delete", "money", "identity")
#: Scopes that are not "dangerous" but *out of bounds*: no tool may have them.
REFUSED_SCOPES = frozenset({"money", "identity"})
RISKS = ("none", "low", "medium", "high")

#: A turn may not become a crawl: four is enough for "search, read, remember,
#: answer" and small enough that one message cannot spend the hour.
MAX_CALLS_PER_TURN = 4
#: The hourly valve, counted from the audit table.
MAX_ACTIONS_PER_HOUR = 20
#: How long a confirmation stays open. Two minutes is one glance at the phone.
CONFIRM_WINDOW = timedelta(minutes=2)
#: Repeats of the same (tool, args) inside one turn that stop the loop.
REPEAT_LIMIT = 2
#: A result is a card, not a document.
MAX_RESULT_CHARS = 1200

PENDING_SETTING = "actions.pending"
ALLOWED_SETTING = "actions.allowed"
#: ``actions.auto`` — routine hands do not ask. On by default, because the owner
#: said so in as many words: "i hate how it asks for permissions for every small
#: task… just turn it off". Only deleting still stops to ask, and money/identity
#: were never a question in the first place. Set it to 0 for the old behaviour,
#: where the first use of each writing tool needed a yes.
AUTO_SETTING = "actions.auto"


class ActionError(Exception):
    """A tool refused, failed, or was refused. ``kind`` is a stable code."""

    def __init__(self, message: str, kind: str = "action_error") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class ToolCall:
    """One request, before it is checked."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """What the loop breaker compares: the tool *and* the exact arguments."""
        blob = json.dumps(dict(self.args), ensure_ascii=False, sort_keys=True, default=str)
        return f"{self.name}:{hashlib.sha256(blob.encode()).hexdigest()[:16]}"

    def preview(self) -> str:
        """A short, readable version for the audit row and the confirm question."""
        parts = []
        for key, value in list(self.args.items())[:4]:
            text = " ".join(str(value).split())[:80]
            parts.append(f"{key}={text}")
        return ", ".join(parts)[:300] or "—"


@dataclass(frozen=True)
class ActionResult:
    """What a tool did, what the model is told, and what the owner sees.

    ``note`` is for the model (machine-ish is fine: "#1 at 14:33 UTC"). ``title``
    and ``lines`` are for the card under her answer — already localized by the
    tool, because a tool knows the words for what it just did and the display
    layer only knows how to draw them.
    """

    ok: bool = True
    note: str = ""
    detail: str = ""
    kind: str = "ok"
    title: str = ""
    lines: tuple[str, ...] = ()


    def as_observation(self) -> str:
        body = self.note or self.detail or "done"
        return body[:MAX_RESULT_CHARS]


#: ``run(turn, args) -> ActionResult``
ToolHandler = Callable[["Turn", Mapping[str, Any]], Awaitable[ActionResult]]


@dataclass(frozen=True)
class Tool:
    """One thing she can do, with the paperwork that lets her do it.

    ``properties`` is the JSON-schema property map (name → one-line description);
    it is used three ways: to tell the model what to send, to validate what it
    sent, and to render ``.yura tools`` for a human.
    """

    name: str
    summary: str
    properties: Mapping[str, str]
    run: ToolHandler
    scope: str = "read"
    risk: str = "low"
    required: tuple[str, ...] = ()
    #: Tools whose result is *for them*, not for the model: the answer is written
    #: from the result, never after another round trip.
    needs_answer: bool = True

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"tool {self.name}: unknown scope {self.scope!r}")
        if self.risk not in RISKS:
            raise ValueError(f"tool {self.name}: unknown risk {self.risk!r}")

    @property
    def refused(self) -> bool:
        return self.scope in REFUSED_SCOPES

    @property
    def destructive(self) -> bool:
        return self.scope in {"delete"}

    @property
    def needs_confirm(self) -> bool:
        """Deletes always ask; writes and sends ask only when auto is off."""
        return self.scope in {"write", "send", "delete"}

        return self.scope in {"read", "write", "send"} and not self.destructive

    @property
    def schema(self) -> dict[str, Any]:
        """The JSON schema, in the shape a model's tool API expects."""
        return {
            "type": "object",
            "properties": {
                name: {"type": "string", "description": text}
                for name, text in self.properties.items()
            },
            "required": list(self.required),
            "additionalProperties": False,
        }

    def signature(self) -> str:
        body = ", ".join(
            f"{name}{'*' if name in self.required else ''}" for name in self.properties
        )
        return f"{self.name}({body})"

    def validate(self, args: Mapping[str, Any] | None) -> dict[str, str]:
        """Coerce and check the arguments. Raises :class:`ActionError`."""
        if self.refused:
            raise ActionError(f"{self.name} is out of bounds", kind="refused")
        data = dict(args or {})
        unknown = set(data) - set(self.properties)
        if unknown:
            raise ActionError(
                f"{self.name} does not take {', '.join(sorted(unknown))}", kind="bad_args"
            )
        missing = [name for name in self.required if not str(data.get(name, "")).strip()]
        if missing:
            raise ActionError(
                f"{self.name} needs {', '.join(missing)}", kind="bad_args"
            )
        out: dict[str, str] = {}
        for name in self.properties:
            if name in data and data[name] is not None:
                out[name] = _as_text(data[name]).strip()[:4000]
        return out

    def as_function(self) -> dict[str, Any]:
        """The ``{"type": "function", ...}`` block for a tools-capable model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.summary,
                "parameters": self.schema,
            },
        }


def _as_text(value: Any) -> str:
    """A value as the line of text the tool wants.

    The model writes ``{"args": {"timezone": "Asia/Tehran"}}`` as often as it
    writes ``{"args": "timezone Asia/Tehran"}``, and the first form used to be a
    validation failure — she then told him she could not do the thing he had just
    asked for. A nested object is turned into the words he would have typed.
    """
    if isinstance(value, dict):
        return " ".join(f"{key} {_as_text(item)}".strip() for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(item) for item in value)
    return str(value)


@dataclass
class Turn:
    """One request from one person, and the leash attached to it."""

    chat_id: int
    actor_id: int = 0
    language: str = "en"
    is_owner: bool = False
    message_id: int | None = None
    #: The message the owner replied to, if any — what "it" refers to.
    reply_to: int | None = None
    started_at: datetime = field(default_factory=utc_now)
    #: How many tools have been used in this turn (the per-turn budget).
    calls: int = 0
    #: Every (tool, args) already used this turn — the loop breaker.
    seen: dict[str, int] = field(default_factory=dict)
    #: What ran, for the receipt line under her answer.
    done: list[str] = field(default_factory=list)
    #: Tools already answered "yes" for *this* turn. Set only by
    #: :meth:`ActionLayer.resolve`, so a delete that was confirmed cannot ask
    #: itself for permission again when it finally runs.
    approved: set[str] = field(default_factory=set)

    def note(self, call: ToolCall) -> None:
        self.calls += 1
        self.seen[call.key] = self.seen.get(call.key, 0) + 1


@dataclass(frozen=True)
class Outcome:
    """The executor's answer: ran, asks, refused, or blocked."""

    status: str  # done | needs_confirm | refused | blocked | failed
    tool: str = ""
    note: str = ""
    token: str = ""
    #: What the card says: a localized title and its italic lines.
    card: tuple[str, tuple[str, ...]] = ("", ())

    @property
    def ok(self) -> bool:
        return self.status == "done"


class ActionLayer:
    """The single door between a model's intention and the real world."""

    def __init__(self, runtime: Any, tools: Iterable[Tool] = ()) -> None:
        self.runtime = runtime
        self.store = getattr(runtime, "store", None)
        self.logger = getattr(runtime, "logger", None)
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name.strip().lower())

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def offered(self) -> tuple[Tool, ...]:
        """What the model is told about: everything except the refused scopes."""
        return tuple(tool for tool in self._tools.values() if not tool.refused)

    def as_functions(self) -> list[dict[str, Any]]:
        return [tool.as_function() for tool in self.offered()]

    @property
    def schema(self) -> dict[str, tuple[str, ...]]:
        """``{tool: (property, ...)}`` — what the parser needs to read a bare string."""
        return {tool.name: tuple(tool.properties) for tool in self._tools.values()}

    def catalogue(self, language: str = "en") -> str:
        """The lines that go into her system prompt. Short on purpose."""
        if not self._tools:
            return ""
        rows = []
        for tool in self.offered():
            rows.append(f"- {tool.signature()} — {tool.summary}")
        header = (
            "## hands"
            if language != "fa"
            else "## دست‌ها"
        )
        if language == "fa":
            body = (
                "این‌ها کارهایی هستند که می‌توانی *انجام* بدهی، نه فقط بگویی. اگر کاری لازم شد، "
                "در پایان جوابت یک بلوک بگذار:\n"
                "```tool\n{\"tool\": \"نام\", \"args\": {\"…\": \"…\"}}\n```\n"
                "قانون‌ها: وقتی از تو می‌خواهد کاری *بکنی* — چیزی را خاموش کن، یادآور بگذار، "
                "کاری از دست‌هایت را اجرا کن — باید جوابت با یک بلوک تمام شود؛ همان بلوک تنها "
                "راه انجام شدن است. هیچ‌وقت دربارهٔ این مکانیزم حرف نزن، هیچ‌وقت چیزی که نشد را نگو که شد. "
                "خواندن و کارهای کوچک آزاد است؛ پاک کردن اول از او اجازه می‌گیرد. کار کوچک را "
                "همان لحظه انجام بده، هرگز نپرس آیا یادآور بگذارم یا چیزی را خاموش کنم. "
                "حداکثر چهار کار در هر پیام. اگر بلوکی نگذاری، هیچ چیز عوض نمی‌شود — "
                "و *همان‌طور که همیشه می‌نویسی* جواب می‌دهی: همان لحن، همان پاراگراف‌ها، "
                "همان تایپوگرافی. دست‌ها صدای تو را عوض نمی‌کنند."
            )
        else:
            body = (
                "these are things you can *do*, not only say. when a request needs one, "
                "end your reply with one block:\n"
                "```tool\n{\"tool\": \"name\", \"args\": {\"…\": \"…\"}}\n```\n"
                "rules: when he asks you to DO something — turn something off, set a "
                "reminder, remember a fact, run one of your commands — you must end with a "
                "block; that block is the only way it happens. never mention this "
                "machinery. never claim something you did not do. reading and small writing "
                "are free; deleting asks him first. four actions per message at most. if you "
                "send no block, nothing changes — and "
                "you answer *exactly as you always do*: same voice, same paragraphs, same "
                "typography. hands do not change your voice."
            )
        return f"{header}\n" + "\n".join(rows) + "\n\n" + body

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------
    def _allowed_key(self, tool: Tool) -> str:
        return f"{ALLOWED_SETTING}.{tool.name}"

    async def is_auto(self) -> bool:
        """True when routine actions run without a question (the default)."""
        if self.store is None:
            return True
        try:
            return bool(await self.store.get_bool(AUTO_SETTING, True))
        except Exception:  # noqa: BLE001 - a broken setting must not start asking
            return True

    async def should_ask(self, tool: Tool, turn: Turn) -> bool:
        """The one place the confirm policy lives.

        Deleting: always, unless this very turn already said yes for it.
        Everything else: never while auto is on. With auto off, the old rule —
        once per tool — comes back, so nothing is unreachable.
        """
        if not tool.needs_confirm or tool.name in turn.approved:
            return False
        if tool.destructive:
            return True
        if await self.is_auto():
            return False
        return not await self.is_allowed(tool)

    async def is_allowed(self, tool: Tool) -> bool:
        """True when this tool has already been approved once (writes only)."""
        if self.store is None:
            return False
        try:
            return bool(await self.store.get_bool(self._allowed_key(tool), False))
        except Exception:  # noqa: BLE001 - a broken setting is not a yes
            return False

    async def allow(self, tool: Tool) -> None:
        if self.store is not None:
            await self.store.set_setting(self._allowed_key(tool), "1")

    async def hourly_count(self) -> int:
        if self.store is None:
            return 0
        since = (utc_now() - timedelta(hours=1)).isoformat()
        try:
            value = await self.store.db.scalar(
                "SELECT COUNT(*) FROM yura_actions WHERE created_at >= ?", (since,), default=0
            )
            return int(value or 0)
        except Exception:  # noqa: BLE001 - an unreadable count must not block a read
            return 0

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def execute(self, call: ToolCall, turn: Turn) -> Outcome:
        """Check, record, run, record. The only way anything happens."""
        # The owner's hands. Not "anyone in the room the owner is also in": a
        # stranger who talks to her may talk, and may not make her *do* anything.
        if not turn.is_owner:
            await self._audit(turn, self.get(call.name) or Tool(
                name=call.name[:40] or "unknown", summary="", properties={},
                run=_noop, scope="read",
            ), call, decision="refused", status="refused", error="not the owner")
            return Outcome("refused", tool=call.name, note="not the owner")
        tool = self.get(call.name)
        if tool is None:
            return Outcome("failed", tool=call.name, note=f"there is no tool called {call.name}")
        if tool.refused:
            await self._audit(turn, tool, call, decision="refused", status="refused")
            return Outcome("refused", tool=tool.name, note=tool.summary)
        # The loop breaker comes before the budget: a loop is a bug, not a spend.
        if turn.seen.get(call.key, 0) >= REPEAT_LIMIT:
            return Outcome(
                "blocked", tool=tool.name, note="that was already tried, twice"
            )
        if turn.calls >= MAX_CALLS_PER_TURN:
            return Outcome("blocked", tool=tool.name, note="too many actions in one message")
        if await self.hourly_count() >= MAX_ACTIONS_PER_HOUR:
            return Outcome("blocked", tool=tool.name, note="too many actions this hour")
        try:
            args = tool.validate(call.args)
        except ActionError as exc:
            await self._audit(turn, tool, call, decision="rejected", status="failed", error=str(exc))
            return Outcome("failed", tool=tool.name, note=str(exc))

        if await self.should_ask(tool, turn):
            token = await self._ask(turn, tool, args)
            if token:
                await self._audit(turn, tool, call, decision="asked", status="waiting")
                return Outcome("needs_confirm", tool=tool.name, token=token, note=tool.signature())

        row = await self._audit(turn, tool, call, decision="allowed", status="running")
        turn.note(call)
        try:
            result = await tool.run(turn, args)
        except ActionError as exc:
            await self._finish(row, ok=False, detail=str(exc))
            await self._count_outcome(tool, ok=False)
            return Outcome("failed", tool=tool.name, note=str(exc))
        except Exception as exc:  # noqa: BLE001 - a hand that throws is not a crash
            if self.logger is not None:
                self.logger.warning("action %s failed: %s", tool.name, exc)
            await self._finish(row, ok=False, detail=f"{type(exc).__name__}: {exc}")
            return Outcome("failed", tool=tool.name, note=f"{type(exc).__name__}: {exc}")
        await self._finish(row, ok=result.ok, detail=result.as_observation())
        await self._count_outcome(tool, ok=result.ok)
        if result.ok:
            turn.done.append(tool.name)
        return Outcome(
            "done" if result.ok else "failed",
            tool=tool.name,
            note=result.as_observation(),
            card=(result.title, tuple(result.lines)),
        )

    async def _count_outcome(self, tool: Tool, *, ok: bool) -> None:
        metrics = getattr(self.runtime, "metrics", None)
        if metrics is None:
            return
        try:
            metrics.count(f"actions_{'ok' if ok else 'failed'}")
            metrics.count(f"action_{tool.name}")
        except Exception:  # noqa: BLE001 - counting is not worth an exception
            pass

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------
    async def _ask(self, turn: Turn, tool: Tool, args: Mapping[str, str]) -> str:
        """Park a pending action and return its token (empty when it cannot)."""
        if self.store is None:
            return ""
        token = hashlib.sha256(
            f"{turn.chat_id}:{tool.name}:{json.dumps(args, sort_keys=True)}:{utc_now()}".encode()
        ).hexdigest()[:12]
        record = {
            "token": token,
            "tool": tool.name,
            "args": dict(args),
            "chat_id": turn.chat_id,
            "actor_id": turn.actor_id,
            "language": turn.language,
            "is_owner": bool(turn.is_owner),
            "expires_at": (utc_now() + CONFIRM_WINDOW).isoformat(),
        }
        try:
            await self.store.set_setting(f"{PENDING_SETTING}.{token}", record)
        except Exception:  # noqa: BLE001 - no store, no asking; the caller says so
            return ""
        return token

    async def pending(self, chat_id: int) -> dict[str, Any] | None:
        """The newest unexpired question for this chat."""
        if self.store is None:
            return None
        settings = await self.store.all_settings()
        best: dict[str, Any] | None = None
        for key, value in settings.items():
            if not key.startswith(f"{PENDING_SETTING}.") or not isinstance(value, dict):
                continue
            if int(value.get("chat_id", 0)) != int(chat_id):
                continue
            expires = _parse(value.get("expires_at"))
            if expires is None or expires <= utc_now():
                continue
            if best is None or str(value.get("expires_at")) > str(best.get("expires_at")):
                best = value
        return best

    async def resolve(self, chat_id: int, *, approve: bool) -> Outcome:
        """Answer the open question. ``approve=False`` forgets it and says so."""
        record = await self.pending(chat_id)
        if record is None:
            return Outcome("failed", note="nothing is waiting for an answer")
        token = str(record.get("token", ""))
        try:
            await self.store.delete_setting(f"{PENDING_SETTING}.{token}")
        except Exception:  # noqa: BLE001
            pass
        tool = self.get(str(record.get("tool", "")))
        if tool is None:
            return Outcome("failed", note="that tool is gone")
        if not approve:
            return Outcome("refused", tool=tool.name, note="cancelled")
        # Approving a write approves the *tool* from now on; deleting never is.
        if tool.scope != "delete":
            await self.allow(tool)
        turn = Turn(
            chat_id=chat_id,
            actor_id=int(record.get("actor_id", 0) or 0),
            language=str(record.get("language", "en") or "en"),
            # Who asked is who was asked; the yes itself is already owner-only.
            is_owner=bool(record.get("is_owner", True)),
            approved={tool.name},
        )
        return await self.execute(ToolCall(tool.name, dict(record.get("args") or {})), turn)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    async def _audit(
        self,
        turn: Turn,
        tool: Tool,
        call: ToolCall,
        *,
        decision: str,
        status: str,
        error: str = "",
    ) -> int:
        if self.store is None:
            return 0
        try:
            return await self.store.db.insert(
                """
                INSERT INTO yura_actions(
                    chat_id, actor_id, tool, scope, risk, args_hash, args_preview,
                    decision, status, error, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(turn.chat_id),
                    int(turn.actor_id or 0),
                    tool.name,
                    tool.scope,
                    tool.risk,
                    call.key,
                    call.preview(),
                    decision,
                    status,
                    error[:500],
                    utc_now().isoformat(),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the audit is not allowed to crash a read
            if self.logger is not None:
                self.logger.warning("action audit failed: %s", exc)
            return 0

    async def _finish(self, row: int, *, ok: bool, detail: str) -> None:
        if not row or self.store is None:
            return
        try:
            await self.store.db.execute(
                "UPDATE yura_actions SET status = ?, result = ?, finished_at = ? WHERE id = ?",
                ("done" if ok else "failed", detail[:1000], utc_now().isoformat(), row),
            )
        except Exception:  # noqa: BLE001
            pass

    async def recent(self, chat_id: int | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """The last few actions, newest first, for ``.yura actions`` and receipts."""
        if self.store is None:
            return []
        try:
            if chat_id is None:
                rows = await self.store.db.fetch_all(
                    "SELECT * FROM yura_actions ORDER BY id DESC LIMIT ?", (limit,)
                )
            else:
                rows = await self.store.db.fetch_all(
                    "SELECT * FROM yura_actions WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                    (int(chat_id), limit),
                )
        except Exception:  # noqa: BLE001
            return []
        return [dict(row) for row in rows]


async def _noop(turn: Turn, args: Mapping[str, Any]) -> ActionResult:  # pragma: no cover
    """Placeholder handler for an audit row about a tool that does not exist."""
    return ActionResult(False, detail="nothing ran", kind="refused")


# ---------------------------------------------------------------------------
# The wire format
# ---------------------------------------------------------------------------
#: A fenced block the model ends a reply with. Kept deliberately boring: models
#: are reliable with a fence and a small JSON object, and unreliable with clever.
TOOL_BLOCK = re.compile(
    r"```(?:tool|json)?\s*(?P<body>[\[{].*?[\]}])\s*```", re.IGNORECASE | re.DOTALL
)
#: A fence that says "tool" but whose contents are not valid JSON.
TOOL_LEFTOVER = re.compile(
    r"```[ \t]*(?:tool|json)?[ \t]*\r?\n?(?:(?!```).)*?\"(?:tool|command)\"(?:(?!```).)*?```",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
#: The same thing without a fence, as a last resort (`[[do: {...}]]`).
TOOL_INLINE = re.compile(r"\[\[\s*do\s*[::]\s*(?P<body>\{.*?\})\s*\]\]", re.IGNORECASE | re.DOTALL)

#: Every key a model has been seen to use for the name of the thing it wants.
#: "action" and "function" come from other frameworks; "command" is ambiguous and
#: is handled last, because it is also the name of her own console tool.
_NAME_KEYS = ("tool", "tool_name", "toolName", "name", "action", "function")
_CONSOLE_KEYS = ("command", "cmd")
_ARGS_KEYS = (
    "args",
    "arguments",
    "input",
    "params",
    "parameters",
    "values",
    "arg",
)


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _args_of(data: Mapping[str, Any]) -> Any:
    value = _first(data, _ARGS_KEYS)
    return {} if value is None else value


def _args_dict(value: Any, tool: str = "", schema: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Whatever was in ``args``, as a mapping — or ``None`` if it cannot be one."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return {f"arg{index}": item for index, item in enumerate(value)}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, dict):
                return {str(key): item for key, item in loaded.items()}
        # A bare string: the tool's only property is what it must mean. Anything
        # else and it is handed over as ``args`` so validation can say so plainly.
        properties = list(schema or ())
        if tool and len(properties) == 1:
            return {properties[0]: text}
        return {"args": text}
    return None


def _args_text(value: Any) -> str:
    """A value as the words a person would have typed."""
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_args_text(item)}".strip() for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_args_text(item) for item in value)
    if value in (None, ""):
        return ""
    return str(value)


def _call_of(data: Any, schema: Mapping[str, Any] | None = None) -> ToolCall | None:
    """One object, read as a call — in any of the shapes models actually write.

    Four have been seen in the wild, and all four now mean the same thing:

    ``{"tool": "remind", "args": {"when": "30m", "what": "call mom"}}``
        the documented shape;
    ``{"name": "recall", "arguments": "{\"about\": \"sara\"}"}``
        OpenAI's, with the arguments as a JSON *string*;
    ``{"command": "autoreply", "args": "add hi => hello"}``
        the console tool, implied by the key — she then runs that line;
    ``{"tool": "fal", "args": "page 166"}``
        a command named where a tool belongs: same console, same line.
    """
    if not isinstance(data, Mapping):
        return None

    name = _first(data, _NAME_KEYS)
    arguments = _args_of(data)
    if isinstance(name, Mapping):  # {"function": {"name": ..., "arguments": ...}}
        arguments = name.get("arguments", arguments)
        if isinstance(arguments, str) and arguments.strip().startswith("{"):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                arguments = arguments
        name = name.get("name")

    if isinstance(name, str) and name.strip():
        tool = name.strip().lower()
        if tool == "command":
            args = _args_dict(arguments, tool, schema)
            if args is None:
                return None
            # The console with a bare string is a line: "fal page 166".
            if set(args) == {"args"}:
                line = str(args["args"]).strip()
                if line:
                    head, _, rest = line.partition(" ")
                    return ToolCall("command", {"command": head, "args": rest})
            return ToolCall("command", args)
        if schema is not None and schema and tool not in schema:
            # A name that is not one of her tools is a *command*: models write
            # ``{"tool": "fal", "args": "page 166"}`` because both are "things she
            # can do". Sending it to the console is what he meant.
            line = " ".join(
                part for part in (tool, _args_text(arguments).strip()) if part
            )
            head, _, rest = line.partition(" ")
            return ToolCall("command", {"command": head, "args": rest})
        args = _args_dict(arguments, tool, (schema or {}).get(tool))
        if args is None:
            return None
        return ToolCall(tool, args)

    # No tool-shaped key at all: the model named a *command* in the console slot.
    console = _first(data, _CONSOLE_KEYS)
    if isinstance(console, str) and console.strip():
        line = " ".join(console.split())
        head, _, rest = line.partition(" ")
        extra = _args_dict(arguments, "command") or {}
        tail = " ".join(str(part) for part in extra.values()).strip()
        args = {"command": head, "args": " ".join(part for part in (rest, tail) if part).strip()}
        return ToolCall("command", {key: value for key, value in args.items() if value})
    return None


def _looks_like_call(data: Any) -> bool:
    """True for any object that *claims* to be a call, valid or not."""
    if not isinstance(data, Mapping):
        return False
    if any(key in data for key in _CONSOLE_KEYS + ("tool", "tool_name", "toolName")):
        return any(key in data for key in _ARGS_KEYS + _CONSOLE_KEYS) or "tool" in data
    # ``{"name": …, "arguments": …}`` is the same claim in another framework's
    # spelling — and it must not reach a chat window either.
    return any(key in data for key in ("name", "action")) and any(
        key in data for key in _ARGS_KEYS
    )


def _objects_in(body: str) -> list[Any]:
    """Every JSON value in a fence: one object, an array, or objects back to back."""
    blob = body.strip()
    if not blob:
        return []
    try:
        loaded = json.loads(blob)
    except (ValueError, TypeError):
        loaded = None
    if isinstance(loaded, dict):
        return [loaded]
    if isinstance(loaded, list):
        return list(loaded)
    decoder = json.JSONDecoder()
    out: list[Any] = []
    index = 0
    while index < len(blob):
        while index < len(blob) and blob[index] in " \t\r\n,":
            index += 1
        if index >= len(blob):
            break
        try:
            value, index = decoder.raw_decode(blob, index)
        except ValueError:
            break
        if isinstance(value, dict):
            out.append(value)
    return out


def _bare_calls(text: str, schema: Mapping[str, Any] | None = None) -> list[tuple[int, int, ToolCall]]:
    """Call objects anywhere in the text — the model does not always use a fence.

    The reported bug was exactly this: three copies of
    ``{"command": "autoreply", "args": "add private * he's not here"}`` arrived as
    her *message*, with a copy button in Telegram, and nothing ran. A model that
    writes the object bare has asked for the thing; it is not prose.
    """
    found: list[tuple[int, int, ToolCall]] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, TypeError):
            index = start + 1
            continue
        if isinstance(value, dict):
            call = _call_of(value, schema)
            if call is not None:
                found.append((start, end, call))
            index = end
            continue
        index = start + 1
    return found


def parse_calls(
    text: str, *, schema: Mapping[str, Any] | None = None
) -> tuple[list[ToolCall], str]:
    """Split a reply into tool calls and the words a person should see.

    ``schema`` is the tool layer's ``{tool: (property, ...)}``; with it, a bare
    string argument can be given to the tool's only property instead of being a
    validation error.

    Anything that is not a well-formed call object is left in the text: a model
    that writes a JSON example in a sentence must not lose the sentence, and a
    block without a name or with a non-object body is prose, not an action.
    """
    if not text:
        return [], text
    calls: list[ToolCall] = []
    removed: list[tuple[int, int]] = []
    for pattern in (TOOL_BLOCK, TOOL_INLINE):
        for match in pattern.finditer(text):
            # A fence may hold *several* objects — the model writes one per line
            # when it wants two things done, and json.loads on the whole body
            # fails, which used to dump the raw JSON into the chat as a code
            # block. Every object in the block is read; a block with none is
            # left alone as prose.
            found = [
                call
                for call in (_call_of(item, schema) for item in _objects_in(match.group("body")))
                if call
            ]
            if not found:
                continue
            calls.extend(found)
            removed.append((match.start(), match.end()))
    # ...and the same shapes with no fence at all, which is what he photographed.
    for start, end, call in _bare_calls(text, schema):
        if any(begin <= start < stop for begin, stop in removed):
            continue
        calls.append(call)
        removed.append((start, end))
    if not calls:
        return [], text
    # The same call written three times is one call. Both because it is what the
    # model does when it is unsure, and because doing it three times is worse
    # than not doing it.
    unique: list[ToolCall] = []
    seen: set[str] = set()
    for call in calls:
        if call.key in seen:
            continue
        seen.add(call.key)
        unique.append(call)
    out = []
    cursor = 0
    for start, end in sorted(removed):
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    return unique[:MAX_CALLS_PER_TURN], cleaned


def scrub_tool_text(text: str) -> str:
    """Whatever is left of a tool block after parsing, make it not her answer.

    Belt and braces for the turn-18 bug: the fenced block is parsed and executed
    first, and this removes any half-formed remains (a fence that is not valid
    JSON, a bare ``{"tool": …}`` or ``{"command": …}`` line). Nothing
    machine-shaped is allowed into a message; an empty result is better than a
    block of JSON with a copy button.
    """
    if not text or '"' not in text:
        return text
    cleaned = TOOL_INLINE.sub("", TOOL_LEFTOVER.sub("", text))
    # ...and the object itself, wherever it starts, because ``{"tool": …`` is
    # machine syntax even when the model wrote it mid-sentence. Braces are counted
    # so a nested ``args`` object goes with it.
    while True:
        start = _stray_object(cleaned)
        if start is None:
            break
        cleaned = cleaned[:start] + cleaned[_object_end(cleaned, start) :].lstrip(" \t")
    cleaned = " ".join(cleaned.split()) if "\n" not in cleaned else cleaned
    if cleaned == text:
        return text
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _stray_object(text: str) -> int | None:
    """The index of a call object that survived parsing, if there is one."""
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            return None
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, TypeError):
            index = start + 1
            continue
        if _looks_like_call(value):
            return start
        index = end
    return None


def _object_end(text: str, start: int) -> int:
    """The index just past the object that begins at ``start`` (or the line end)."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth <= 0:
                return index + 1
        elif char == "\n" and depth <= 0:
            return index
    return len(text)


def calls_from_native(payload: Any, schema: Mapping[str, Any] | None = None) -> list[ToolCall]:
    """Tool calls out of an OpenAI-shaped ``tool_calls`` list."""
    calls: list[ToolCall] = []
    if not isinstance(payload, list):
        return calls
    for entry in payload:
        function = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(function, dict):
            continue
        call = _call_of(
            {"tool": function.get("name"), "args": function.get("arguments") or {}}, schema
        )
        if call is not None:
            calls.append(call)
    return calls[:MAX_CALLS_PER_TURN]


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed
