"""Automatic behaviour driven by detected events.

Every subscriber here follows the same checklist before acting:

1. feature switch on,
2. emergency stop off,
3. chat allowed (allowlist/denylist, per-chat overrides),
4. quiet hours inactive,
5. cooldown clean,
6. dry-run honoured (notify instead of act),
7. loop protection (never answer our own output).

If any step fails, the automation stays silent — except in dry-run mode, where
the owner gets a "would have done X because Y" preview (feature area 97).

Feature areas: 21, 22, 28, 29, 31-40, 50, 57, 59, 75, 84, 96, 97.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..event_engine import EventType, NormalizedEvent
from ..services.cache import TTLCache, dedupe_key
from ..services.translation_provider import TranslationError, normalize_lang
from ..utils.formatting import bold, escape
from ..utils.templates import TemplateError, render_template, template_values
from ..utils.text import normalize_persian, truncate

REMINDER_HINTS = (
    "remind me", "reminder", "don't forget", "dont forget", "remember to",
    "یادم بنداز", "یادم بیار", "یادت نره", "یادآوری", "فراموش نکن",
)
NOTIFY_COOLDOWN = 30.0


@dataclass
class AutomationState:
    sent: TTLCache[str] = field(default_factory=lambda: TTLCache[bool](maxsize=512, ttl=180))
    last_flood_notice: float = 0.0
    #: Bursts are a description of the account's traffic, not an event the owner
    #: can act on. One notice every half hour is information; one every few
    #: minutes is the bot spamming the very chat it is supposed to be quiet in.
    last_burst_notice: float = 0.0
    bursts_suppressed: int = 0
    triggered: int = 0
    replied: int = 0
    notified: int = 0
    skipped: int = 0


def _normalize(text: str) -> str:
    return " ".join(normalize_persian(text or "").lower().split())


def _match(text: str, pattern: str, match_type: str) -> bool:
    haystack = _normalize(text)
    needle = _normalize(pattern)
    if match_type == "any":
        return True
    if match_type == "exact":
        return haystack == needle
    if match_type == "startswith":
        return haystack.startswith(needle)
    if match_type == "endswith":
        return haystack.endswith(needle)
    if match_type == "regex":
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False
    return needle in haystack


class Automations:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        self.state = AutomationState()
        self.self_id = runtime.permissions.self_id

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    async def _allowed(self, event: NormalizedEvent, feature: str) -> bool:
        decision = await self.runtime.permissions.can_act(
            event.chat_id, feature, is_private=event.is_private if event.chat_id else None
        )
        if not decision.allowed:
            self.state.skipped += 1
            if self.runtime.config.debug:
                self.logger.debug("automation %s skipped: %s", feature, decision.code)
            return False
        return True

    async def _owner_language(self) -> str:
        owner = self.runtime.permissions.self_id
        if owner:
            pref = await self.runtime.store.get_language("user", str(owner))
            if pref in {"en", "fa"}:
                return pref
        return self.runtime.config.default_language

    def _loop_guard(self, event: NormalizedEvent) -> bool:
        """True when this message looks like our own output (loop risk)."""
        if not event.text:
            return False
        if self.state.sent.get(dedupe_key(event.text.strip().lower())):
            return True
        sender = event.sender_id
        if sender is not None and sender == self.self_id:
            return True
        return False

    def _remember_sent(self, text: str) -> None:
        if text:
            self.state.sent.set(dedupe_key(text.strip().lower()), True)

    async def _dry_run(self) -> bool:
        return await self.runtime.permissions.dry_run_active()

    async def _preview(self, event: NormalizedEvent, feature: str, action: str, reason: str) -> None:
        language = await self._owner_language()
        self.state.triggered += 1
        text = self.runtime.t(
            "dry_run.preview",
            language,
            feature=escape(feature),
            action=escape(action),
            reason=escape(reason),
            chat=escape(str(event.chat_id)),
            preview=escape(event.safe_preview(60)),
        )
        # A preview is the whole point of dry-run, so it must not be swallowed.
        await self.runtime.sender.notify_owner(text, dry_run=False)

    async def _notify_owner(self, text: str) -> None:
        self.state.notified += 1
        await self.runtime.notify_owner(text)

    # ------------------------------------------------------------------
    # Subscribers
    # ------------------------------------------------------------------
    async def on_message(self, event: NormalizedEvent) -> None:
        if event.is_outgoing or event.message_type == "service":
            return
        if self._loop_guard(event):
            self.state.skipped += 1
            return
        sender = getattr(getattr(event.raw, "sender", None), "bot", False)
        if sender:
            self.state.skipped += 1
            return

        # The reins come first. A rule he wrote about one person ("don't answer
        # him", "just heart him") is about *answering*, and every feature below
        # answers: an auto-reply that keeps talking to somebody he silenced would
        # make the rule a decoration.
        if await self._rule_gate(event):
            return

        await self._autoreply(event)
        await self._away(event)
        await self._watches(event)
        await self._triggers(event, event_type="message")
        await self._reactions(event)
        await self._mention_notifications(event)
        await self._reminder_intent(event)
        await self._auto_translate(event)

    async def on_edit(self, event: NormalizedEvent) -> None:
        await self._triggers(event, event_type="edit")
        if not await self.runtime.store.feature_enabled("notifications"):
            return
        if event.is_outgoing:
            return
        language = await self._owner_language()
        await self._notify_owner(
            self.runtime.t(
                "notify.edited",
                language,
                chat=escape(str(event.chat_id)),
                link=escape(_link(event)),
                preview=escape(event.safe_preview(80)),
            )
        )

    async def on_deleted(self, event: NormalizedEvent) -> None:
        if not await self.runtime.store.feature_enabled("notifications"):
            return
        ids = event.extra.get("deleted_ids") or []
        language = await self._owner_language()
        await self._notify_owner(
            self.runtime.t(
                "notify.deleted",
                language,
                chat=escape(str(event.chat_id)),
                count=len(ids),
            )
        )

    async def on_new_chat(self, event: NormalizedEvent) -> None:
        if not await self.runtime.store.feature_enabled("notifications"):
            return
        language = await self._owner_language()
        await self._notify_owner(
            self.runtime.t(
                "notify.new_chat",
                language,
                chat=escape(str(event.chat_id)),
                users=escape(", ".join(str(uid) for uid in event.extra.get("user_ids") or []) or "—"),
            )
        )

    #: Seconds between two burst notices. Thirty minutes: often enough to notice
    #: a real flood, rare enough that the notification never becomes the flood.
    BURST_NOTICE_INTERVAL = 1800.0

    async def on_burst(self, event: NormalizedEvent) -> None:
        if not await self.runtime.store.feature_enabled("notifications"):
            return
        now = time.monotonic()
        if self.state.last_burst_notice and now - self.state.last_burst_notice < self.BURST_NOTICE_INTERVAL:
            self.state.bursts_suppressed += 1
            return
        suppressed = self.state.bursts_suppressed
        self.state.bursts_suppressed = 0
        self.state.last_burst_notice = now
        language = await self._owner_language()
        notice = self.runtime.t(
            "notify.burst",
            language,
            count=event.extra.get("count", 0),
            window=event.extra.get("window", 0),
            chat=escape(str(event.extra.get("sample_chat", "—"))),
        )
        if suppressed:
            notice += "\n" + self.runtime.t("notify.burst_suppressed", language, count=suppressed)
        await self._notify_owner(notice)

    async def on_network(self, event: NormalizedEvent) -> None:
        language = await self._owner_language()
        key = "notify.reconnected" if event.type == EventType.NETWORK_RECONNECT else "notify.disconnected"
        if event.type == EventType.NETWORK_DISCONNECT:
            # Never promise delivery while offline; log-only for disconnects.
            self.logger.warning("network disconnected")
            return
        await self._notify_owner(self.runtime.t(key, language))

    async def on_flood_wait(self, event: NormalizedEvent) -> None:
        now = time.monotonic()
        if now - self.state.last_flood_notice < 60:
            return
        self.state.last_flood_notice = now
        language = await self._owner_language()
        await self._notify_owner(
            self.runtime.t("notify.flood_wait", language, seconds=event.extra.get("seconds", 0))
        )

    async def on_startup(self, event: NormalizedEvent) -> None:
        if self.runtime.config.debug:
            self.logger.info("automation subscribers ready")

    # ------------------------------------------------------------------
    # Auto-replies
    # ------------------------------------------------------------------
    async def _rule_gate(self, event: NormalizedEvent) -> bool:
        """``True`` when a rule about this person already answered for her.

        ``ignore`` stops every automatic answerer dead — no auto-reply text, no
        away message, no trigger, no watcher. ``react`` and ``sticker`` take the
        place of all of them: one glyph instead of a paragraph.
        """
        speaker = getattr(event, "sender_id", None)
        if speaker is None or self.runtime.permissions.is_owner(speaker):
            return False
        try:
            rule = await self.runtime.store.person_rule(speaker, event.chat_id)
        except Exception:  # noqa: BLE001 - a rule table is not a lockout
            return False
        if rule is None:
            return False
        mode = str(rule["mode"] or "")
        if mode == "ignore":
            self.state.skipped += 1
            return True
        if mode == "answer":
            return False
        target = getattr(event, "message_id", None)
        if not target:
            return mode != "answer"
        emoji = str(rule["emoji"] or "") or "❤️"
        try:
            if mode == "react":
                await self.runtime.sender.react(event.chat_id, int(target), emoji)
            elif mode == "sticker":
                from .stickers import _documents, _wanted

                documents = await _documents(self.runtime.client, _wanted(emoji) or emoji)
                if documents:
                    await self.runtime.sender.send_media(
                        event.chat_id, random.choice(documents), reply_to=target
                    )
        except Exception as exc:  # noqa: BLE001 - a sticker is not worth an answer
            self.logger.warning("automation: rule action failed (%s)", exc)
        return True

    async def _autoreply(self, event: NormalizedEvent) -> None:
        if not event.text or not await self._allowed(event, "autoreply"):
            return
        rules = await self.runtime.store.list_autoreplies(scope_chat_id=event.chat_id, enabled_only=True)
        for rule in rules:
            # Who this rule is allowed to speak in. "Reply to anyone who DMs me"
            # must not answer in a group — that is the difference between a
            # secretary and a bot talking over him in public.
            scope = str(rule["scope"] or "any") if "scope" in rule.keys() else "any"
            if scope == "private" and not event.is_private:
                continue
            if scope == "group" and not event.is_group:
                continue
            if not _match(event.text, rule["pattern"], rule["match_type"]):
                continue
            key = f"autoreply:{rule['id']}:{event.chat_id}"
            cooldown = float(rule["cooldown"] or 300)
            if self.runtime.limiter.cooldown_remaining(key, cooldown) > 0:
                self.state.skipped += 1
                continue
            if await self._dry_run():
                await self._preview(
                    event, "autoreply", escape(truncate(str(rule["response"]), 80)), f"matched '{rule['pattern']}'"
                )
                self.runtime.limiter.run(key, cooldown)
                return
            try:
                await self.runtime.sender.send(event.chat_id, escape(rule["response"]), reply_to=event.message_id)
                self._remember_sent(rule["response"])
                self.runtime.limiter.run(key, cooldown)
                self.state.replied += 1
                await self.runtime.store.bump_usage("autoreply", "sent")
            except Exception as exc:  # noqa: BLE001
                self.runtime.health.record_error("autoreply", exc)
            return

    # ------------------------------------------------------------------
    # Away mode
    # ------------------------------------------------------------------
    async def _away(self, event: NormalizedEvent) -> None:
        if not event.is_private or not event.text:
            return
        if not await self._allowed(event, "away"):
            return
        message = await self.runtime.store.get_setting("away.message", "")
        if not message:
            return
        key = f"away:{event.sender_id}"
        if self.runtime.limiter.cooldown_remaining(key, 900) > 0:
            self.state.skipped += 1
            return
        if await self._dry_run():
            await self._preview(event, "away", escape(truncate(str(message), 80)), "away mode is on")
            self.runtime.limiter.run(key, 900)
            return
        try:
            await self.runtime.sender.send(event.chat_id, escape(str(message)), reply_to=event.message_id)
            self._remember_sent(str(message))
            self.runtime.limiter.run(key, 900)
            self.state.replied += 1
            await self.runtime.store.bump_usage("away", "sent")
        except Exception as exc:  # noqa: BLE001
            self.runtime.health.record_error("away", exc)

    # ------------------------------------------------------------------
    # Watches
    # ------------------------------------------------------------------
    async def _watches(self, event: NormalizedEvent) -> None:
        if not event.text:
            return
        if not await self.runtime.feature_manager.is_enabled("watches"):
            return
        if await self.runtime.store.emergency_stop():
            return
        rows = await self.runtime.store.list_watches(enabled_only=True)
        if not rows:
            return
        normalized = _normalize(event.text)
        language = await self._owner_language()
        for row in rows:
            if row["scope_chat_id"] is not None and int(row["scope_chat_id"]) != event.chat_id:
                continue
            keyword = _normalize(row["keyword"])
            if not keyword or keyword not in normalized:
                continue
            key = f"watch:{row['id']}:{event.chat_id}"
            if self.runtime.limiter.cooldown_remaining(key, NOTIFY_COOLDOWN) > 0:
                continue
            self.runtime.limiter.run(key, NOTIFY_COOLDOWN)
            self.state.triggered += 1
            if await self._dry_run():
                await self._preview(event, "watches", "notify", f"keyword '{row['keyword']}'")
                continue
            await self._notify_owner(
                self.runtime.t(
                    "notify.watch",
                    language,
                    keyword=escape(row["keyword"]),
                    chat=escape(str(event.chat_id)),
                    sender=escape(str(event.sender_id or "—")),
                    link=escape(_link(event)),
                    preview=escape(event.safe_preview(120)),
                )
            )

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------
    async def _triggers(self, event: NormalizedEvent, *, event_type: str) -> None:
        if not await self.runtime.feature_manager.is_enabled("triggers"):
            return
        if await self.runtime.store.emergency_stop():
            return
        rows = await self.runtime.store.list_triggers(enabled_only=True)
        for row in rows:
            if (row["event_type"] or "message") != event_type:
                continue
            if row["scope_chat_id"] is not None and int(row["scope_chat_id"]) != event.chat_id:
                continue
            if not _match(event.text, row["pattern"], row["match_type"]):
                continue
            key = f"trigger:{row['id']}:{event.chat_id}"
            if self.runtime.limiter.cooldown_remaining(key, 60) > 0:
                continue
            decision = await self.runtime.permissions.can_act(
                event.chat_id, "triggers", is_private=event.is_private
            )
            if not decision.allowed:
                self.state.skipped += 1
                continue
            action = row["action_type"]
            payload = row["payload"] or ""
            self.state.triggered += 1
            if await self._dry_run():
                await self._preview(event, "triggers", f"{action} {payload}".strip(), f"pattern '{row['pattern']}'")
                self.runtime.limiter.run(key, 60)
                continue
            try:
                if action == "notify":
                    language = await self._owner_language()
                    await self._notify_owner(
                        self.runtime.t(
                            "notify.trigger",
                            language,
                            name=escape(row["name"]),
                            chat=escape(str(event.chat_id)),
                            link=escape(_link(event)),
                            preview=escape(event.safe_preview(120)),
                        )
                    )
                elif action == "react":
                    await self._send_reaction(event, payload)
                elif action == "template":
                    template = await self.runtime.store.get_template(payload.lower())
                    if template is None:
                        self.logger.warning("trigger %s references missing template %r", row["name"], payload)
                        continue
                    body = render_template(
                        template["body"],
                        template_values(chat=str(event.chat_id), name=str(event.sender_id or ""), id=event.chat_id),
                    )
                    await self.runtime.sender.send(event.chat_id, escape(body), reply_to=event.message_id)
                    self._remember_sent(body)
                elif action == "translate":
                    await self._translate_reply(event, target=payload or None)
            except (TemplateError, TranslationError) as exc:
                self.runtime.health.record_error("trigger", exc)
            except Exception as exc:  # noqa: BLE001
                self.runtime.health.record_error("trigger", exc)
            finally:
                self.runtime.limiter.run(key, 60)
            return

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    async def _reactions(self, event: NormalizedEvent) -> None:
        if not event.text or event.message_id is None:
            return
        if not await self._allowed(event, "reactions"):
            return
        rows = await self.runtime.store.list_reaction_rules(enabled_only=True)
        for row in rows:
            if row["scope_chat_id"] is not None and int(row["scope_chat_id"]) != event.chat_id:
                continue
            if not _match(event.text, row["pattern"], row["match_type"]):
                continue
            key = f"reaction:{row['id']}:{event.chat_id}"
            if self.runtime.limiter.cooldown_remaining(key, float(row["cooldown"] or 60)) > 0:
                continue
            if await self._dry_run():
                await self._preview(event, "reactions", f"react {row['emoji']}", f"pattern '{row['pattern']}'")
                self.runtime.limiter.run(key, float(row["cooldown"] or 60))
                return
            if await self._send_reaction(event, row["emoji"]):
                self.runtime.limiter.run(key, float(row["cooldown"] or 60))
                self.state.triggered += 1
            return

    async def _send_reaction(self, event: NormalizedEvent, emoji: str) -> bool:
        client = self.runtime.client
        if client is None:
            return False
        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji

            await client(
                SendReactionRequest(
                    peer=event.chat_id,
                    msg_id=event.message_id,
                    reaction=[ReactionEmoji(emoticon=emoji)],
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 - reactions are often restricted
            self.runtime.health.record_error("reaction", exc)
            return False

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    async def _mention_notifications(self, event: NormalizedEvent) -> None:
        if not (event.mentions_me or event.replies_to_me):
            return
        if not await self.runtime.feature_manager.is_enabled("notifications"):
            return
        if await self.runtime.store.emergency_stop():
            return
        key = f"mention:{event.chat_id}:{event.message_id}"
        if self.runtime.limiter.cooldown_remaining(key, NOTIFY_COOLDOWN) > 0:
            return
        self.runtime.limiter.run(key, NOTIFY_COOLDOWN)
        language = await self._owner_language()
        template = "notify.mention" if event.mentions_me else "notify.reply"
        await self._notify_owner(
            self.runtime.t(
                template,
                language,
                chat=escape(str(event.chat_id)),
                sender=escape(str(event.sender_id or "—")),
                link=escape(_link(event)),
                preview=escape(event.safe_preview(120)),
            )
        )

    async def _reminder_intent(self, event: NormalizedEvent) -> None:
        if not event.text:
            return
        if not await self.runtime.feature_manager.is_enabled("reminder_intent"):
            return
        if await self.runtime.store.emergency_stop():
            return
        lowered = _normalize(event.text)
        if not any(hint in lowered for hint in REMINDER_HINTS):
            return
        key = f"intent:{event.chat_id}:{event.message_id}"
        if self.runtime.limiter.cooldown_remaining(key, 300) > 0:
            return
        self.runtime.limiter.run(key, 300)
        language = await self._owner_language()
        await self._notify_owner(
            self.runtime.t(
                "notify.reminder_intent",
                language,
                chat=escape(str(event.chat_id)),
                link=escape(_link(event)),
                preview=escape(event.safe_preview(120)),
                prefix=self.runtime.prefix,
            )
        )

    # ------------------------------------------------------------------
    # Auto-translate (opt-in)
    # ------------------------------------------------------------------
    async def _auto_translate(self, event: NormalizedEvent) -> None:
        if not event.text:
            return
        if not await self.runtime.feature_manager.is_enabled("auto_translate"):
            return
        if not self.runtime.translator.available:
            return
        target = await self.runtime.store.get_language("chat", str(event.chat_id))
        if target not in {"en", "fa"}:
            return
        detected = event.language if event.language in {"en", "fa"} else None
        if detected == target or event.language == "mixed":
            return
        if not await self._allowed(event, "auto_translate"):
            return
        key = f"translate:{event.chat_id}"
        if self.runtime.limiter.cooldown_remaining(key, 120) > 0:
            return
        self.runtime.limiter.run(key, 120)
        if await self._dry_run():
            await self._preview(event, "auto_translate", f"translate → {target}", "language mismatch")
            return
        await self._translate_reply(event, target=target)

    async def _translate_reply(self, event: NormalizedEvent, *, target: str | None) -> None:
        if not self.runtime.translator.available:
            return
        target_lang = normalize_lang(target or await self.runtime.store.get_language("chat", str(event.chat_id)) or "en")
        try:
            result = await self.runtime.translator.translate(event.text, target_lang)
        except TranslationError as exc:
            self.runtime.health.record_error("auto_translate", exc)
            return
        header = self.runtime.t(
            "translate.inline_header",
            await self._owner_language(),
            target=escape(target_lang),
            provider=escape(result.provider),
        )
        await self.runtime.sender.send(
            event.chat_id, f"{bold(header)}\n{escape(result.text)}", reply_to=event.message_id
        )
        self._remember_sent(result.text)


def _link(event: NormalizedEvent) -> str:
    if event.chat_id and event.message_id:
        raw = str(event.chat_id)
        if raw.startswith("-100"):
            return f"https://t.me/c/{raw[4:]}/{event.message_id}"
    return ""


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    automations = Automations(runtime)
    # Exposed so the tests (and `.diagnostics`) can reach the same instance the
    # events reach, instead of one built separately with its own throttles.
    runtime.automation = automations
    engine = runtime.event_engine

    engine.subscribe(
        "automation.message",
        automations.on_message,
        {EventType.MESSAGE},
        priority=50,
        description="auto-replies, away mode, watches, triggers, reactions, notifications",
    )
    engine.subscribe("automation.edit", automations.on_edit, {EventType.EDIT}, priority=60)
    engine.subscribe("automation.deleted", automations.on_deleted, {EventType.DELETED}, priority=60)
    engine.subscribe("automation.new_chat", automations.on_new_chat, {EventType.NEW_CHAT}, priority=60)
    engine.subscribe("automation.burst", automations.on_burst, {EventType.BURST}, priority=70)
    engine.subscribe(
        "automation.network",
        automations.on_network,
        {EventType.NETWORK_DISCONNECT, EventType.NETWORK_RECONNECT},
        priority=70,
    )
    engine.subscribe("automation.flood", automations.on_flood_wait, {EventType.FLOOD_WAIT}, priority=70)
    engine.subscribe("automation.startup", automations.on_startup, {EventType.STARTUP}, priority=90)
