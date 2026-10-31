"""Plans: one sentence, many moments.

"Turn the clock off for ten minutes, then turn it on and off again after three
minutes, then set an auto reply for private chats saying he's busy, then stop it,
and in our saved messages say 'I'm your good girl'."

That is six actions with five different times, and it is *one* thing he said. A
reminder cannot hold it and a single command cannot say it. So a plan is:

* a row in ``plans`` with the sentence exactly as he wrote it and a label to call
  it back by;
* one row per step in ``plan_steps``, each carrying an absolute moment and a
  **console line**;
* one scheduler job per step, so a deploy, a crash or a six-hour ceiling does not
  lose the tail of the list;
* a claim before every run, so a step fires exactly once.

Why console lines and not a new action language: every ability HAVOC has is
already a command with a usage string, a permission check and a dry-run gate.
A plan that runs commands can therefore do anything the bot can do, today and
after the next feature lands — and every step is testable by running the same
line by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..utils.security import redact_text
from ..utils.time import iso, utc_now

#: A plan may not be longer than this. Past it, he is describing a campaign, and
#: a campaign needs a confirmation, not a monologue.
MAX_STEPS = 60
#: The sentences that mean "and then". Order matters: longest first.
_SEQUENCE_WORDS = (
    "and then",
    "after that",
    "afterwards",
    "and afterwards",
    "then",
    "next",
    "بعد",
    "سپس",
    "و بعد",
)
#: "for ten minutes", "for 2h" — the shape that means "and undo it".
_FOR_A_WHILE = re.compile(
    r"\bfor\s+(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)\b",
    re.I,
)
#: "after 3 minutes", "in 2 hours" — a delay before this step.
_DELAY = re.compile(
    r"^(?:after|in|پس از|بعد از)\s+(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)\b",
    re.I,
)
_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
}


@dataclass
class Step:
    """One timed console line."""

    action: str
    at: datetime
    undo_of: int | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "at": iso(self.at), "note": self.note}


@dataclass
class Plan:
    """A sentence, broken into steps."""

    said: str
    steps: list[Step] = field(default_factory=list)
    label: str = ""

    def add(self, action: str, at: datetime, *, undo_of: int | None = None, note: str = "") -> Step:
        step = Step(action=action, at=at, undo_of=undo_of, note=note)
        self.steps.append(step)
        return step

    @property
    def empty(self) -> bool:
        return not self.steps

    def describe(self, *, limit: int = 12) -> list[str]:
        return [f"{iso(step.at)}  {step.action}" for step in self.steps[:limit]]

    def as_dict(self) -> dict[str, Any]:
        """The whole timeline, for the JSON side of ``.plan show``."""
        return {
            "said": self.said,
            "label": self.label,
            "steps": [step.as_dict() for step in self.steps],
        }


# ---------------------------------------------------------------------------
# Reading the sentence
# ---------------------------------------------------------------------------
def split_sequence(said: str) -> list[str]:
    """Cut a sentence at the words that mean "and then".

    A comma before or after the connector is part of the connector, not part of
    the instruction: "off for ten minutes, then turn it on" is two clauses.
    """
    text = str(said or "").strip()
    if not text:
        return []
    pattern = re.compile(
        r"\s*(?:,|;|،)?\s*(?:" + "|".join(re.escape(word) for word in _SEQUENCE_WORDS) + r")\s*(?:,|;|،)?\s*",
        re.I,
    )
    parts = [part.strip(" ,;،.") for part in pattern.split(text)]
    # "clock on and off again" is two instructions joined by "and", and so is
    # "set the reply and stop it". Only a verb on the right makes it a new clause:
    # "salt and pepper" is one thing, "turn it on and off" is two actions.
    verb = (
        r"(?:turn|switch|clock|set|say|stop|send|react|enable|disable|start|wait|"
        r"delete|message|post|put|remove|add|ignore|mute|on|off|in|tell|ask)"
    )
    # ", and …" is always a new clause in speech; " and …" only when a verb
    # follows, so "salt and pepper" stays one thing.
    split_on_and = re.compile(rf"(?:,\s*and\s+|\s+and\s+(?={verb}\b))", re.I)
    expanded: list[str] = []
    for part in parts:
        expanded.extend(piece.strip(" ,;،.") for piece in split_on_and.split(part))
    return [part for part in expanded if part]


def take_delay(clause: str) -> tuple[float | None, str]:
    """"after 3 minutes turn it on" → (180.0, "turn it on")."""
    match = _DELAY.match(str(clause or "").strip())
    if not match:
        return None, str(clause or "").strip()
    unit = (match.group("unit") or "").lower()
    try:
        seconds = float(match.group("amount")) * _UNIT_SECONDS.get(unit, 1.0)
    except (TypeError, ValueError):
        return None, clause
    return seconds, clause[match.end():].strip(" ,;،.")


_TRAILING_DELAY = re.compile(
    r"\b(?:after|in|پس از|بعد از)\s+(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)\s*$",
    re.I,
)


def take_trailing_delay(clause: str) -> tuple[float | None, str]:
    """"clock off again after 3 minutes" → (180.0, "clock off again")."""
    match = _TRAILING_DELAY.search(str(clause or ""))
    if not match:
        return None, str(clause or "").strip()
    unit = (match.group("unit") or "").lower()
    try:
        seconds = float(match.group("amount")) * _UNIT_SECONDS.get(unit, 1.0)
    except (TypeError, ValueError):
        return None, clause
    return seconds, clause[: match.start()].strip(" ,;،.")


def take_duration(clause: str) -> tuple[float | None, str]:
    """"turn the clock off for ten minutes" → (600.0, "turn the clock off")."""
    match = _FOR_A_WHILE.search(str(clause or ""))
    if not match:
        return None, str(clause or "").strip()
    unit = (match.group("unit") or "").lower()
    try:
        seconds = float(match.group("amount")) * _UNIT_SECONDS.get(unit, 1.0)
    except (TypeError, ValueError):
        return None, clause
    stripped = (clause[: match.start()] + " " + clause[match.end():]).strip(" ,;،.")
    return seconds, stripped


def label_for(said: str) -> str:
    """A short name he can say to cancel it: the first thing it does."""
    words = [word for word in re.split(r"\s+", str(said or "").strip()) if word]
    return " ".join(words[:4])[:60] or "a plan"


class PlanRunner:
    """Turn a :class:`Plan` into rows and jobs, and run its steps."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @property
    def store(self) -> Any:
        return self.runtime.store

    async def create(self, plan: Plan, *, said: str = "", owner_id: int | None = None) -> dict[str, Any]:
        """Persist a plan and queue its steps. Returns the plan as a dict."""
        steps = plan.steps[:MAX_STEPS]
        if not steps:
            return {"ok": False, "error": "this plan has no steps"}
        label = plan.label or label_for(said or plan.said)
        plan.label = label
        plan_id = await self.store.add_plan(
            owner_id=owner_id, label=label, said=said or plan.said, status="pending"
        )
        for index, step in enumerate(steps):
            step_id = await self.store.add_plan_step(plan_id, index, iso(step.at), step.action)
            await self.runtime.scheduler.enqueue(
                "plan_step", step.at, {"plan_id": plan_id, "step_id": step_id, "index": index}
            )
        self.runtime.metrics.count("plans")
        return {
            "ok": True,
            "id": plan_id,
            "label": label,
            "steps": len(steps),
            "first_at": iso(steps[0].at),
            "last_at": iso(steps[-1].at),
            # The whole timeline, not just its size: this is what ``.plan --json``
            # prints and what an agent gets back when it builds one.
            "plan": plan.as_dict(),
        }

    async def run_step(self, payload: dict[str, Any]) -> None:
        """A scheduler job: run one step's console line, once."""
        step_id = int(payload.get("step_id") or 0)
        plan_id = int(payload.get("plan_id") or 0)
        plan = await self.store.plan(plan_id)
        if plan is None:
            return
        if str(plan["status"]) != "pending":
            await self.store.step_fired(step_id, status="cancelled", note="plan is not pending")
            return
        steps = await self.store.plan_steps(plan_id)
        step = next((row for row in steps if int(row["id"]) == step_id), None)
        if step is None:
            return
        if not await self.store.step_fired(step_id, status="done"):
            return  # another runner claimed it
        await self._execute(str(step["action"]), plan_id=plan_id)
        await self._close_if_done(plan_id)

    async def _execute(self, action: str, *, plan_id: int) -> None:
        from .self_console import run as run_command

        self_id = int(getattr(self.runtime.permissions, "self_id", 0) or 0)
        reply = await run_command(
            self.runtime,
            action,
            chat_id=self_id or 0,
            sender_id=self_id,
        )
        if not reply.ok:
            self.runtime.logger.warning(
                "plan %s step %r failed: %s", plan_id, action, redact_text(reply.note)[:200]
            )

    async def _close_if_done(self, plan_id: int) -> None:
        remaining = await self.store.pending_steps(plan_id)
        if not remaining:
            await self.store.close_plan(plan_id, "done", note="every step ran")

    async def cancel(self, needle: str | None = None, *, newest: bool = True) -> dict[str, Any]:
        """Stop a plan: by id, by a word of its label, or the newest pending one."""
        if needle:
            plan = await self.store.find_plan(needle)
        elif newest:
            plan = await self.store.newest_plan()
        else:
            plan = None
        if plan is None:
            return {"ok": False, "error": "there is no plan to stop"}
        plan_id = int(plan["id"])
        if str(plan["status"]) != "pending":
            return {"ok": False, "error": f"plan #{plan_id} is already {plan['status']}"}
        await self.store.close_plan(plan_id, "cancelled", note="stopped by the owner")
        for step in await self.store.pending_steps(plan_id):
            await self.store.step_fired(int(step["id"]), status="cancelled", note="plan stopped")
        # The jobs stay queued but will find a cancelled plan and do nothing —
        # deleting rows behind a running tick is how two workers disagree.
        self.runtime.metrics.count("plans_cancelled")
        return {"ok": True, "id": plan_id, "label": str(plan["label"]), "steps": len(await self.store.plan_steps(plan_id))}

    async def show(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.store.plans(limit, status="pending")
        out: list[dict[str, Any]] = []
        for row in rows:
            steps = await self.store.plan_steps(int(row["id"]))
            out.append(
                {
                    "id": int(row["id"]),
                    "label": str(row["label"]),
                    "said": str(row["said"]),
                    "status": str(row["status"]),
                    "steps": [
                        {"at": step["at"], "action": step["action"], "status": step["status"]}
                        for step in steps
                    ],
                }
            )
        return out


# ---------------------------------------------------------------------------
# Building a plan from a sentence, using the same floor that reads commands
# ---------------------------------------------------------------------------
def _actions_for(clause: str, *, intent: Any, last_body: str = "") -> list[str]:
    """Turn one clause into console lines, using the deterministic floor.

    A clause can be more than one line ("set an auto reply" is the rule *and* the
    switch), and every line becomes its own step: a plan that merged them into one
    string would run neither.
    """
    found = intent.detect(clause, allow_plan=False)
    if found is not None and getattr(found, "lines", None):
        return [str(line).strip() for line in found.lines if str(line).strip()]
    # Anaphora: "and off again" / "back on" carry no subject. It is the subject of
    # the clause before — "clock on and off again" is clock twice.
    bare = re.fullmatch(r"(?:and\s+)?(on|off|back on|again on|again off|off again|on again)\b.*", clause.strip(), re.I)
    if bare and last_body:
        subject = _subject_of(last_body)
        if subject:
            word = bare.group(1).lower()
            direction = "off" if "off" in word else "on"
            retried = intent.detect(f"{subject} {direction}", allow_plan=False)
            if retried is not None and getattr(retried, "lines", None):
                return [str(line).strip() for line in retried.lines if str(line).strip()]
    return []


def _subject_of(clause: str) -> str:
    """The thing a clause is about: "turn my live clock off" → "clock"."""
    text = str(clause or "").lower()
    # "reply to every DM with X" *is* an auto reply, spelled as a sentence. Its
    # off switch is `autoreply off`, so the subject has to say so — reading the
    # last word instead ("bro") found nothing to undo and the rule outlived the
    # four hours he asked for.
    if re.search(r"\b(?:reply|respond|answer)\s+to\s+(?:every|each|any|all)\b", text):
        return "autoreply"
    if re.search(r"\b(?:auto[\s-]?repl(?:y|ies)|away\s+message)\b", text):
        return "autoreply"
    for word in ("clock", "autoreply", "auto reply", "away", "profile", "timer", "fal"):
        if word in text:
            return "autoreply" if word == "auto reply" else word
    words = [item for item in re.split(r"\W+", text) if len(item) > 2 and item not in {
        "the", "turn", "switch", "again", "back", "and", "with", "for", "minutes", "minute",
    }]
    return words[-1] if words else ""


def build_plan(
    said: str,
    *,
    intent: Any,
    now: datetime | None = None,
    timezone: str | None = None,
) -> Plan:
    """Read a whole sentence into steps.

    Each clause is resolved by the same intent floor a single instruction uses, so
    a plan can never understand a sentence that a plain message would not. The
    timings come from the words: "for ten minutes" makes an undo step, "after 3
    minutes" shifts the moment, and an unadorned clause runs immediately.
    """
    current = now or utc_now()
    plan = Plan(said=said, label=label_for(said))
    offset = 0.0
    last_body = ""
    seen: set[tuple[str, int]] = set()
    for clause in split_sequence(said):
        delay, body = take_delay(clause)
        if delay is not None:
            offset += delay
        duration, body = take_duration(body)
        tail_delay, body = take_trailing_delay(body)
        if tail_delay is not None:
            offset += tail_delay
        # "then stop it" means the thing the previous clause turned on.
        stopper = re.fullmatch(r"(?:then\s+)?(?:stop|turn off|switch off|disable)\s+(?:it|that|them)", body.strip(), re.I)
        lines = _actions_for(body, intent=intent, last_body=last_body)
        if not lines and stopper and last_body:
            inverse = _inverse(last_body, intent=intent)
            lines = [inverse] if inverse else []
        if not lines:
            continue
        moment = current + timedelta(seconds=offset)
        for action in lines:
            key = (action, int(offset))
            if key in seen:
                continue
            seen.add(key)
            plan.add(action, moment)
        last_body = body
        if duration:
            inverse = _inverse(body, intent=intent)
            if inverse:
                key = (inverse, int(offset + duration))
                if key not in seen:
                    seen.add(key)
                    plan.add(inverse, moment + timedelta(seconds=duration))
            offset += duration
    return plan


def _inverse(clause: str, *, intent: Any) -> str | None:
    """The sentence that undoes ``clause``: "off" → "on", "add" → "remove"."""
    text = str(clause or "").lower()
    swaps = (
        ("turn off", "turn on"),
        ("switch off", "switch on"),
        ("off", "on"),
        ("on", "off"),
        ("enable", "disable"),
        ("disable", "enable"),
        ("start", "stop"),
        ("stop", "start"),
        ("mute", "unmute"),
        ("unmute", "mute"),
    )
    for left, right in swaps:
        if re.search(rf"\b{re.escape(left)}\b", text):
            flipped = re.sub(rf"\b{re.escape(left)}\b", right, clause, count=1)
            lines = _actions_for(flipped, intent=intent)
            if lines:
                return lines[0]
    # "stop it" after "set an auto reply" means the reply, not the clock: the
    # subject's off switch is a console line, and it is used as one. Reading it
    # back through the sentence floor turned "autoreply off" into a *new rule*
    # saying "off" — which is how a plan can quietly do the opposite of its words.
    subject = _subject_of(clause)
    if subject in {"autoreply", "clock", "away"}:
        return f"{subject} off"
    if subject:
        lines = _actions_for(f"{subject} off", intent=intent)
        if lines:
            return lines[0]
    return None


__all__ = [
    "MAX_STEPS",
    "take_trailing_delay",
    "Plan",
    "PlanRunner",
    "Step",
    "build_plan",
    "label_for",
    "split_sequence",
    "take_delay",
    "take_duration",
]
