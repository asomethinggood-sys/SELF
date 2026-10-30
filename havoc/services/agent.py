"""The loop: she plans, the executor decides, she answers — once.

This is the smallest honest version of "agentic". It is not a planner that runs
for ten steps of its own reasoning; it is one extra round trip, capped, and only
when the model actually asked for a hand.

    ask → (tool call?) → executor → results → ask again → one answer

When no tool is called — which is most messages — *nothing* changes: no second
request, no extra prompt block beyond the tool catalogue, and the same cleaning,
restyling and echo stripping every answer already goes through. That is the
promise the owner made this feature conditional on.

Two wire formats are accepted, because free models are not consistent:

* native ``tool_calls`` (OpenAI-shaped) when the model implements function calling;
* a fenced block at the end of the reply, which every chat model can produce.

Both end in :meth:`havoc.services.actions.ActionLayer.execute`, so the policy,
the audit row and the budgets are the same either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .actions import (
    MAX_CALLS_PER_TURN,
    ActionLayer,
    Outcome,
    ToolCall,
    Turn,
    calls_from_native,
    parse_calls,
)
from .ai_provider import AIMessage, AIProvider

__all__ = ["Agent", "AgentStep"]

#: How many times the model may be re-asked inside one turn: "I ran what you
#: asked, now answer", then a correction, then one repair of a line the console
#: rejected for its syntax. Ten would be a chatbot writing a novel on the owner's
#: quota — and three is the number that makes a wrong guess recoverable instead of
#: final.
MAX_ROUNDS = 3


@dataclass
class AgentStep:
    """What the loop produced."""

    text: str
    used: bool = False
    outcomes: list[Outcome] = field(default_factory=list)
    #: The question waiting for a yes/no, if the loop stopped for one.
    waiting: Outcome | None = None
    #: ``(title, lines)`` for each tool that actually ran — the card under her
    #: answer is built from these, so what she says and what was done agree.
    cards: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    @property
    def did(self) -> list[str]:
        return [outcome.tool for outcome in self.outcomes if outcome.status == "done"]


class Agent:
    """Runs the tool loop over whatever engine it is handed."""

    def __init__(self, layer: ActionLayer, *, logger: Any = None) -> None:
        self.layer = layer
        self.logger = logger

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.layer.as_functions()

    async def run(
        self,
        provider: AIProvider,
        messages: list[AIMessage],
        *,
        turn: Turn,
        max_tokens: int,
        ask_again: str = "",
    ) -> AgentStep:
        """Ask, act, ask again. Never raises: a broken hand is a sentence."""
        if not self.tools:
            return AgentStep(text="", used=False)
        text, calls = await self._ask(provider, messages, max_tokens=max_tokens)
        if not calls:
            # No hand requested: hand back exactly what the model wrote, untouched.
            return AgentStep(text=text, used=False)

        outcomes: list[Outcome] = []
        rounds = 0
        while calls and rounds < MAX_ROUNDS:
            rounds += 1
            for call in calls:
                outcome = await self.layer.execute(call, turn)
                outcomes.append(outcome)
                if outcome.status == "needs_confirm":
                    # Stop here: the answer *is* the question. Letting the model
                    # keep talking about an action it has not been allowed to take
                    # is how a bot ends up narrating things it did not do.
                    return AgentStep(
                        text=text, used=True, outcomes=outcomes, waiting=outcome
                    )
                if outcome.status == "blocked":
                    break
            if turn.calls >= MAX_CALLS_PER_TURN:
                break
            messages = [
                *messages,
                AIMessage("assistant", text or "(tool call)"),
                AIMessage("user", self._observation(outcomes, language=turn.language, extra=ask_again)),
            ]
            text, calls = await self._ask(provider, messages, max_tokens=max_tokens)
            if not calls:
                break
        return AgentStep(
            text=text,
            used=True,
            outcomes=outcomes,
            cards=[o.card for o in outcomes if o.status == "done" and o.card[0]],
        )

    async def _ask(
        self, provider: AIProvider, messages: list[AIMessage], *, max_tokens: int
    ) -> tuple[str, list[ToolCall]]:
        """One request with the catalogue, tolerating both wire formats."""
        converse_tools = getattr(provider, "converse_tools", None)
        try:
            if converse_tools is not None:
                text, raw = await converse_tools(
                    messages, tools=self.tools, max_tokens=max_tokens
                )
                calls = calls_from_native(raw, self.layer.schema)
            else:  # pragma: no cover - every provider in this repo has the method
                text = await provider.converse(messages, max_tokens=max_tokens)
                calls = []
        except TypeError:  # pragma: no cover - a stub without the keyword
            text = await provider.converse(messages, max_tokens=max_tokens)
            calls = []
        if calls:
            return text, calls
        # No native call: a block — fenced or bare — is the second route to the
        # same door.
        parsed, clean = parse_calls(text, schema=self.layer.schema)
        return (clean if parsed else text), parsed

    def _observation(self, outcomes: list[Outcome], *, language: str, extra: str = "") -> str:
        """What the model is told between rounds. Facts, not instructions to chat."""
        lines = []
        for outcome in outcomes:
            detail = " ".join(str(outcome.note or "").split())
            lines.append(f"- {outcome.tool}: {outcome.status} — {detail}")
        header = "## what actually happened" if language != "fa" else "## آنچه واقعاً شد"
        body = "\n".join(lines) or "- nothing"
        if language == "fa":
            tail = (
                "حالا جواب بده — همان‌طور که همیشه می‌نویسی: همان لحن، همان پاراگراف‌ها، "
                "همان تایپوگرافی، همان طول. دربارهٔ این مکانیزم حرف نزن؛ چیزی که نشد را نگو که شد."
            )
        else:
            tail = (
                "now answer him — exactly as you always write: same voice, same "
                "paragraphs, same typography, same length. do not mention this "
                "machinery, and do not claim anything that did not happen."
            )
        if extra:
            tail = f"{tail}\n{extra}"
        return f"{header}\n{body}\n\n{tail}"
