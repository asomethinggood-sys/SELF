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
    # Persian: "بعدش" and "و بعدش" are the everyday "and then". A bare "بعد"
    # inside a word — "بعدش", "بعدی" — is not a connector, so the Persian
    # alternatives require a word of their own.
    "و بعدش",
    "بعدش",
    "و سپس",
    "سپس",
    "و بعد",
    "بعد از آن",
)
#: The amount, in the words people actually use for short waits.
_COUNT_WORDS = {
    "یک": 1, "یه": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6,
    "هفت": 7, "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12,
    "پانزده": 15, "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60,
    "نیم": 0.5, "یک ربع": 0.25,
}

#: "for ten minutes", "for 2h", "برای دو ساعت" — the shape that means "and
#: undo it". Persian puts the window where English does, between the verb's
#: object and the verb, so the same shape works.
_EN_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "a couple of": 2, "a couple": 2, "half an": 0.5,
    "half a": 0.5, "a half": 0.5, "quarter of an": 0.25,
}
_EN_AMOUNT = r"(?:\d+(?:\.\d+)?|" + "|".join(
    sorted((re.escape(word) for word in _EN_WORDS), key=len, reverse=True)
) + r")"
_FOR_A_WHILE = re.compile(
    rf"\bfor\s+(?P<amount>{_EN_AMOUNT})\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days)(?![a-z])"
    r"|(?:^|\s)(?:برای|واسه|به\s*مدت|به\s*اندازهٔ?)\s*(?P<fa_amount>\d+|یک|یه|دو|سه|چهار|پنج|ده|بیست|نیم)\s*"
    r"(?P<fa_unit>ثانیه|دقیقه|ساعت|روز|هفته)",
    re.I,
)
_AMOUNT = r"(?:\d+(?:\.\d+)?|یک ربع|" + "|".join(_COUNT_WORDS) + r")"
_UNIT = (
    r"(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days"
    r"|ثانیه|دقیقه|ساعت|روز|هفته)"
)
#: "after 3 minutes", "in 2 hours", "بعد از دو ساعت" — a delay before this step.
_DELAY = re.compile(
    rf"^(?:after|in|پس از|بعد از)\s+(?P<amount>{_AMOUNT})\s*(?P<unit>{_UNIT})(?![\wء-ي])",
    re.I,
)
#: "ده دقیقه پیامتو پاک کن" — a bare wait at the head of a clause, which in
#: Persian is how a *follow-up* step says "+10 minutes", and in English speech
#: is "ten minutes later delete it".
_BARE_DELAY = re.compile(
    rf"^(?P<amount>{_AMOUNT})\s*(?P<unit>{_UNIT})\s*"
    rf"(?:که\s+(?:تموم|گذشت|شد)(?:\s+(?:شد|بشه|باشه|بعد|دیگه))?"
    rf"|بعد|دیگه|later|after that)?\s*(?P<rest>.+)$",
    re.I,
)
#: "دو ساعت که تموم شد برو …" without the number being a bare clause head.
_WHEN_DONE = re.compile(
    rf"^(?P<amount>{_AMOUNT})\s*(?P<unit>{_UNIT})\s*که\s+"
    rf"(?:تموم|گذشت|شد)(?:\s+(?:شد|بشه|باشه|بعد|دیگه))?\s*(?P<rest>.+)$",
    re.I,
)
_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    # Persian units, because a plan he speaks is a plan he types.
    "ثانیه": 1.0, "ثانیه‌ای": 1.0, "دقیقه": 60.0, "دقیقه‌ای": 60.0,
    "ساعت": 3600.0, "ساعتی": 3600.0, "روز": 86400.0, "هفته": 604800.0,
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
        r"\s*(?:,|;|،)?\s*(?:(?<![\u0600-\u06ff])(?:"
        + "|".join(re.escape(word) for word in _SEQUENCE_WORDS)
        + r")(?![\u0600-\u06ff]))\s*(?:,|;|،)?\s*",
        re.I,
    )
    # "… ignore her for two hours, two hours later DM محمد": a comma followed by
    # a number-and-unit is a *new step*, not the continuation of the first one.
    # Without this, the wait and everything after it stay glued to the rule and
    # are read — and silently dropped — as part of it.
    wait_split = re.compile(
        rf"\s*[,،;]\s*(?={_AMOUNT}\s*{_UNIT})",
        re.I,
    )
    text = "\x00".join(wait_split.split(text))
    parts = [
        piece
        for part in pattern.split(text)
        for piece in part.split("\x00")
    ]
    parts = [part.strip(" ,;،.") for part in parts]
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


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _normalize_digits(text: str) -> str:
    """Persian and Arabic-Indic digits are the same numbers, so read them."""
    return str(text or "").translate(_FA_DIGITS)


def _seconds_between(amount: str, unit: str) -> float | None:
    """"2"/"hours" → 7200.0 — numbers as digits or as words, in either script."""
    raw = re.sub(r"\s+", " ", _normalize_digits(str(amount or "").strip())).lower()
    if raw in _COUNT_WORDS:
        value = float(_COUNT_WORDS[raw])
    elif raw in _EN_WORDS:
        value = float(_EN_WORDS[raw])
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
    scale = _UNIT_SECONDS.get(str(unit or "").strip().lower())
    if scale is None:
        return None
    return value * scale


def take_delay(clause: str) -> tuple[float | None, str]:
    """"after 3 minutes turn it on" → (180.0, "turn it on")."""
    text = _normalize_digits(str(clause or "").strip())
    match = _DELAY.match(text)
    if not match:
        return None, str(clause or "").strip()
    seconds = _seconds_between(match.group("amount"), match.group("unit"))
    if seconds is None:
        return None, text
    return seconds, text[match.end():].strip(" ,;،.")


def take_bare_delay(clause: str) -> tuple[float | None, str]:
    """"ده دقیقه پیامتو پاک کن" → (600.0, "پیامتو پاک کن").

    A number-plus-unit at the head of a clause is a *wait* only when the rest of
    the clause is an instruction in its own right. "دو ساعت خیلی زیاده" is a
    sentence about two hours; it carries no order to run ten minutes later.
    """
    text = _normalize_digits(str(clause or "").strip())
    match = _WHEN_DONE.match(text) or _BARE_DELAY.match(text)
    if not match:
        return None, str(clause or "").strip()
    rest = (match.group("rest") or "").strip(" ,;،.")
    seconds = _seconds_between(match.group("amount"), match.group("unit"))
    if seconds is None or len(rest.split()) < 2:
        return None, str(clause or "").strip()
    return seconds, rest


_TRAILING_DELAY = re.compile(
    rf"\b(?:after|in|پس از|بعد از)\s+(?P<amount>{_EN_AMOUNT}|{_AMOUNT})\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours|d|day|days"
    r"|ثانیه|دقیقه|ساعت|روز|هفته)\s*(?:دیگه|بعد)?\s*$",
    re.I,
)


def take_trailing_delay(clause: str) -> tuple[float | None, str]:
    """"clock off again after 3 minutes" → (180.0, "clock off again")."""
    text = _normalize_digits(str(clause or ""))
    match = _TRAILING_DELAY.search(text)
    if not match:
        return None, str(clause or "").strip()
    seconds = _seconds_between(match.group("amount"), match.group("unit"))
    if seconds is None:
        return None, text
    return seconds, text[: match.start()].strip(" ,;،.")


def take_duration(clause: str) -> tuple[float | None, str]:
    """"turn the clock off for ten minutes" → (600.0, "turn the clock off")."""
    text = _normalize_digits(str(clause or ""))
    match = _FOR_A_WHILE.search(text)
    if not match:
        return None, str(clause or "").strip()
    seconds = _seconds_between(
        match.group("amount") or match.group("fa_amount"),
        match.group("unit") or match.group("fa_unit"),
    )
    if seconds is None:
        return None, text
    stripped = (text[: match.start()] + " " + text[match.end():]).strip(" ,;،.")
    return seconds, re.sub(r"\s+", " ", stripped).strip(" ,;،.")


def plan_from_steps(
    steps: str,
    *,
    runtime: Any = None,
    now: datetime | None = None,
    timezone: str | None = None,
) -> tuple[Plan | None, str]:
    """A timeline the *model* wrote, instead of one read from a sentence.

    He says something the floor cannot parse ("بعد از ساعت هشت یه پیام به محمد
    بده، نصف ساعت بعدشم پاکش کن"), the model understands it, and the model should
    be able to say what it understood — not be forced back through a reader that
    already failed. ``steps`` is a JSON array (or one step per line)::

        [{"action": "send محمد سلام", "after": "2h"},
         {"action": "unsend last", "after": "10m"}]

    ``after``/``in`` is a wait from the previous step, ``at`` an absolute moment,
    and a step with neither runs as soon as the one before it does. Returns
    ``(plan, "")`` or ``(None, why-not)``.
    """
    import json as _json

    raw = str(steps or "").strip()
    if not raw:
        return None, "no steps"
    rows: list[Any]
    try:
        parsed = _json.loads(raw)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except (ValueError, TypeError):
        # A model that flattened the array into words: "after 2h action send …
        # after 10m action unsend last". The tokens it chose are stable enough
        # to split on, and anything else is one step per line.
        text = raw.replace("\n", " ; ")
        parts = [part for part in re.split(r"\s*;\s*|\s+(?=action\s)", text) if part.strip()]
        rows = parts
    current = now or utc_now()
    plan = Plan(said=raw, label="")
    offset = 0.0
    for index, row in enumerate(rows):
        if isinstance(row, str):
            wait, body = take_delay(row)
            body = body if wait is not None else row
            if wait is not None:
                offset += wait
            body = re.sub(r"^\s*action\s+", "", body, flags=re.I).strip()
            at_text = ""
        elif isinstance(row, dict):
            body = str(row.get("action") or row.get("line") or row.get("command") or "").strip()
            at_text = str(row.get("at") or "").strip()
            after = str(row.get("after") or row.get("in") or "").strip()
            wait = None
            if after:
                wait = _any_wait(after)
                if wait is None:
                    return None, f"step {index + 1}: {after!r} is not a wait"
        else:
            return None, f"step {index + 1} is not an action"
        if not body:
            return None, f"step {index + 1} has no action"
        name = body.split(maxsplit=1)[0]
        if runtime is not None and runtime.registry.resolve(name) is None:
            return None, f"{name!r} is not one of her commands"
        if at_text:
            from ..utils.time import TimeParseError, parse_when

            try:
                moment = parse_when(at_text, timezone or "UTC", now=current)
            except TimeParseError:
                return None, f"step {index + 1}: {at_text!r} is not a time"
            offset = max(0.0, (moment - current).total_seconds())
        elif isinstance(row, dict) and row.get("after"):
            offset += float(wait or 0.0)
        plan.add(body, current + timedelta(seconds=offset))
    if plan.empty:
        return None, "no steps"
    # A label he can say to stop it: the first thing it does, never the JSON.
    plan.label = label_for(plan.steps[0].action)
    return plan, ""


def _any_wait(text: str) -> float | None:
    """A wait in any of the shapes he or a model writes it."""
    seconds, rest = take_delay(text)
    if seconds is not None:
        return seconds
    seconds, rest = take_bare_delay(text)
    if seconds is not None:
        return seconds
    moment = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+|ثانیه|دقیقه|ساعت|روز|هفته)\s*", text)
    if moment:
        return _seconds_between(moment.group(1), moment.group(2))
    return None


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
        ok, why = await self._execute(str(step["action"]), plan_id=plan_id)
        if not ok:
            # A step that could not run must not look like one that did: the row
            # carries the reason, and the plan is closed as failed at the end —
            # but the *other* steps still run, because a fal does not depend on
            # whether a message could be sent.
            await self.store.step_failed(step_id, why or "the step failed")
        await self._close_if_done(plan_id)

    async def _execute(self, action: str, *, plan_id: int) -> tuple[bool, str]:
        from .self_console import run as run_command

        self_id = int(getattr(self.runtime.permissions, "self_id", 0) or 0)
        # Every send this step makes is stamped with the plan, so a later step
        # that says "delete your message" can address the step's own bubble.
        # Set for the duration of the step and cleared again: a stray value left
        # on the runtime would mislabel every later message he sends by hand.
        previous = getattr(self.runtime, "current_plan_id", None)
        self.runtime.current_plan_id = int(plan_id)
        try:
            reply = await run_command(
                self.runtime,
                action,
                chat_id=self_id or 0,
                sender_id=self_id,
            )
        finally:
            self.runtime.current_plan_id = previous
        if not reply.ok:
            self.runtime.logger.warning(
                "plan %s step %r failed: %s", plan_id, action, redact_text(reply.note)[:200]
            )
            return False, str(getattr(reply, "note", "") or "the step failed")
        return True, ""

    async def _close_if_done(self, plan_id: int) -> None:
        remaining = await self.store.pending_steps(plan_id)
        if remaining:
            return
        steps = await self.store.plan_steps(plan_id)
        broken = [row for row in steps if str(row["status"]) == "failed"]
        if broken:
            # "every step ran" would be a lie about a plan with a broken step.
            await self.store.close_plan(
                plan_id,
                "failed",
                note=redact_text(str(broken[0]["note"]) or "a step failed")[:200],
            )
        else:
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
        return [_plan_action(str(line).strip()) for line in found.lines if str(line).strip()]
    # Anaphora: "and off again" / "back on" carry no subject. It is the subject of
    # the clause before — "clock on and off again" is clock twice.
    # "then turn it on" / "then switch it off": the verb is present, the subject
    # is not. It is the subject of the clause before — "clock off for ten
    # minutes, then turn it on" is the clock, twice.
    bare = re.fullmatch(
        r"(?:and\s+)?(?:(?:turn|switch|put|set)\s+(?:it|that|this|them)?\s*(?:back\s+)?)?"
        r"(on|off|back on|again on|again off|off again|on again)\b.*",
        clause.strip(),
        re.I,
    )
    if bare and last_body:
        subject = _subject_of(last_body)
        if subject:
            word = bare.group(1).lower()
            direction = "off" if "off" in word else "on"
            retried = intent.detect(f"{subject} {direction}", allow_plan=False)
            if retried is not None and getattr(retried, "lines", None):
                return [str(line).strip() for line in retried.lines if str(line).strip()]
    return []


def _plan_action(line: str) -> str:
    """One console line, corrected for the fact that it is running in a plan.

    ``delete``/``edit`` aim at the message a *command* was a reply to. A plan
    step has no reply — the scheduler fired it — so "delete it" in a timeline
    means the bubble this plan just sent, which is ``unsend last``.
    """
    if line == "delete":
        return "unsend last"
    return line


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
    #: What the previous clause was *about* ("clock"), which is what "stop it"
    #: and "turn it back on" mean when they carry no subject of their own.
    last_subject = ""
    #: The length of the window the previous clause opened ("for two hours").
    #: When the next clause says "two hours later", it is anchored to the *end*
    #: of that window, not to a second two hours after it.
    settled: float | None = None
    seen: set[tuple[str, int]] = set()
    for clause in split_sequence(said):
        delay, body = take_delay(clause)
        if delay is not None:
            offset += delay
            settled = None
        # A wait at the head of a *later* clause is relative to the step before
        # it: "… ignore سارا for 2h, 2h later DM محمد" is +2h from the rule, not
        # two hours after midnight. The first clause's own window ("for two
        # hours") is a duration, and :func:`take_duration` reads that one.
        # A wait at the head of a clause is measured from the step before it; on
        # the first clause, "from now" is that same step. "دو ساعت دیگه به محمد
        # پیام بده سلام" is a send scheduled two hours out.
        waited, rest = take_bare_delay(body)
        if waited is not None:
            if settled is not None and abs(waited - settled) < 1.0:
                settled = None  # already inside the window that just closed
            else:
                offset += waited
            body = rest
        tail_delay, body = take_trailing_delay(body)
        if tail_delay is not None:
            offset += tail_delay
            settled = None
        duration, stripped = take_duration(body)
        if duration is not None:
            settled = duration
        # The line comes from the sentence *with* its window when the window is
        # part of the order ("ignore سارا for two hours" is a two-hour rule, not
        # a rule plus an undo), and from the sentence without it otherwise.
        lines = _actions_for(body, intent=intent, last_body=last_body)
        if duration is not None:
            # With the window taken out of the words, the same order reads as a
            # clean line: "reply to every DM with X for 4 hours" must not send X
            # followed by "for 4 hours".
            clean = _actions_for(stripped, intent=intent, last_body=last_body)
            # …unless the window *is* the order: a reply rule carries its own
            # expiry, and stripping it would make a two-hour rule permanent.
            if clean and not any("--for" in line for line in lines):
                lines = clean
                body = stripped
        # "then stop it" means the thing the previous clause turned on.
        stopper = re.fullmatch(
            r"(?:then\s+)?(?:stop|turn off|switch off|disable)\s+(?:it|that|them)",
            body.strip(),
            re.I,
        )
        if not lines and stopper and (last_body or last_subject):
            inverse = _inverse(last_body, intent=intent) if last_body else None
            if inverse is None and last_subject:
                # "stop it" after "turn it on": the verb was anaphoric, so undo
                # the *subject*, not the sentence that carried no subject.
                inverse = _inverse(f"{last_subject} on", intent=intent)
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
        if _subject_of(body):
            last_subject = _subject_of(body)
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
    "take_bare_delay",
    "take_delay",
    "plan_from_steps",
    "take_duration",
]
