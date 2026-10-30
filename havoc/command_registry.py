"""Centralized command registration, lookup, help generation and dispatch.

A command is a small declarative object — metadata, permissions, feature
ownership and one async handler. Nothing is parsed from docstrings and there
is no chain of ``if name == ...`` anywhere in the codebase.

Metadata drives behaviour:
* ``requires_reply``      → validated before the handler runs
* ``changes_state``       → respects dry-run mode
* ``needs_confirmation``  → asks the owner to confirm (destructive actions)
* ``feature``             → gated by :class:`FeatureManager`
* ``category``            → groups ``.help`` output

Feature areas: 2, 4, 79, 93, 94, 95, 97.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from . import ui
from .aliases import aliases_for
from .database import DatabaseError
from .localization import LocaleFormatError, LocaleManager, MissingTranslationError
from .services.ai_provider import AIError
from .services.rate_limiter import RateLimited
from .services.sender import SendError
from .services.translation_provider import TranslationError
from .utils.formatting import bold, code, escape
from .utils.parsing import ParsedCommand, ParseError, demote_subcommand
from .utils.time import TimeParseError
from .utils.validation import ValidationError

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

CommandHandler = Callable[["CommandContext"], Awaitable[None]]

CATEGORY_ORDER: tuple[str, ...] = (
    "core",
    "profile",
    "messaging",
    "detection",
    "information",
    "scheduling",
    "management",
    "fun",
    "persona",
    "media",
)


class CommandError(Exception):
    """Raised by handlers for a localized, user-facing failure."""

    def __init__(self, key: str, **variables: Any) -> None:
        self.key = key
        self.variables = variables
        super().__init__(key)


#: Commands that write state. Dry-run and emergency-stop gating key off this,
#: so anything with a persistent side effect must be listed here. The registry
#: enforces it at registration time instead of trusting each definition.
STATE_CHANGING_COMMANDS: frozenset[str] = frozenset(
    {
        "remind", "reminders", "tasks", "schedule", "scheduled", "quiet-hours",
        "bookmark", "note", "labels", "export", "media",
        "clock", "bio", "preset", "profile", "counter",
        "autoreply", "away", "template", "fight", "rules", "chatsettings",
        "alias", "quick", "backup", "plugins",
        "watch", "trigger", "react",
        "feature", "lang", "prefix", "dryrun", "stop",
    }
)


@dataclass(frozen=True)
class Command:
    name: str
    handler: CommandHandler
    category: str = "core"
    aliases: tuple[str, ...] = ()
    feature: str | None = None
    usage: str = ""
    examples: tuple[str, ...] = ()
    requires_reply: bool = False
    changes_state: bool = False
    needs_confirmation: bool = False
    requires_feature: bool = False
    owner_only: bool = True
    hidden: bool = False
    #: Literal description used when no locale key exists (plugins).
    description: str = ""
    parameters: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()
    #: Minimum seconds between two runs of this command (0 = no limit).
    cooldown: float = 0.0

    @property
    def description_key(self) -> str:
        return f"cmd.{self.name}.description"

    @property
    def usage_key(self) -> str:
        return f"cmd.{self.name}.usage"

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass
class CommandContext:
    """Everything a handler needs, with no global state."""

    runtime: Runtime
    event: Any
    message: Any
    parsed: ParsedCommand
    language: str
    command: Command
    dry_run: bool = False
    is_owner: bool = True
    started_at: float = field(default_factory=time.monotonic)
    _reply_cache: Any = field(default=None, repr=False, compare=False)
    _reply_loaded: bool = field(default=False, repr=False, compare=False)

    # -- locale ------------------------------------------------------
    def t(self, key: str, /, *, digits: bool = False, **variables: Any) -> str:
        return self.runtime.t(key, self.language, digits=digits, **variables)

    @property
    def locale(self) -> LocaleManager:
        return self.runtime.locales

    # -- argument helpers -------------------------------------------
    @property
    def args(self) -> tuple[str, ...]:
        return self.parsed.args

    @property
    def sub(self) -> str | None:
        return self.parsed.subcommand

    def arg(self, index: int, default: str = "") -> str:
        return self.parsed.arg(index, default)

    @property
    def raw_args(self) -> str:
        return self.parsed.raw_args

    @property
    def rest(self) -> tuple[str, ...]:
        return self.parsed.rest

    @property
    def rest_raw(self) -> str:
        return self.parsed.rest_raw

    def rest_arg(self, index: int, default: str = "") -> str:
        return self.parsed.rest_arg(index, default)

    def flag(self, name: str, default: str = "") -> str:
        return self.parsed.flag(name, default)

    def has_flag(self, name: str) -> bool:
        return self.parsed.has_flag(name)

    def require_args(self, count: int, hint_key: str = "errors.missing_argument") -> tuple[str, ...]:
        if len(self.args) < count:
            usage = self.usage_text()
            raise CommandError(hint_key, usage=usage)
        return tuple(self.args[:count])

    def usage_text(self) -> str:
        usage = self.t(self.command.usage_key, default="") or self.command.usage
        return f"{self.runtime.prefix}{self.command.name} {usage}".strip()

    # -- output ------------------------------------------------------
    async def reply(self, text: str, *, reply: bool = True, **kwargs: Any) -> Any:
        # A command's own answer is not a side effect: dry-run previews them and
        # the user must always see what happened.
        kwargs.setdefault("dry_run", False)
        return await self.runtime.sender.respond(self.event, text, reply=reply, **kwargs)

    async def reply_error(self, text: str, **kwargs: Any) -> Any:
        header = self.t("common.error_title")
        return await self.reply(f"{bold(header)}\n{text}", **kwargs)

    async def reply_ok(self, text: str, **kwargs: Any) -> Any:
        return await self.runtime.sender.respond(self.event, f"{self.t('common.ok')} {text}", **kwargs)

    @property
    def store(self) -> Any:
        return self.runtime.store

    @property
    def config(self) -> Any:
        return self.runtime.config

    @property
    def sender(self) -> Any:
        return self.runtime.sender

    @property
    def db(self) -> Any:
        return self.runtime.db

    @property
    def chat_id(self) -> int:
        return int(self.event.chat_id)

    @property
    def is_private(self) -> bool:
        for source in (self.event, self.message):
            value = getattr(source, "is_private", None)
            if value is not None:
                return bool(value)
        return False

    async def reply_message(self) -> Any | None:
        """Fetch (once) the message this command replies to, if any."""
        if not self._reply_loaded:
            self._reply_loaded = True
            getter = getattr(self.message, "get_reply_message", None)
            if getter is not None:
                try:
                    self._reply_cache = await getter()
                except Exception:  # noqa: BLE001 - a deleted reply is not fatal
                    self._reply_cache = None
            else:
                self._reply_cache = getattr(self.message, "_havoc_reply", None)
        return self._reply_cache


#: Settings key holding the names of commands the owner switched off.
DISABLED_SETTING = "commands.disabled"


async def disabled_commands(runtime: Any) -> set[str]:
    """Commands he told her to stop serving.

    A feature covers a *family* (poetry, roasts, clock). Some things are a single
    command with no feature behind it — ``fal``, ``fight`` — and "turn off fal"
    has to mean something. This is that something, and it is one list, read from
    one place, so the dispatcher, ``.help``, her prompt and her hands agree.
    """
    try:
        stored = await runtime.store.get_setting(DISABLED_SETTING, [])
    except Exception:  # noqa: BLE001 - an unreadable switch must not switch things off
        return set()
    if isinstance(stored, str):
        stored = [chunk.strip() for chunk in stored.split(",")]
    if not isinstance(stored, (list, tuple, set)):
        return set()
    return {str(name).strip().lower() for name in stored if str(name).strip()}


async def set_command_disabled(runtime: Any, name: str, disabled: bool) -> set[str]:
    """Add or remove one name from the list and return the new one."""
    current = await disabled_commands(runtime)
    key = (name or "").strip().lower()
    if disabled:
        current.add(key)
    else:
        current.discard(key)
    await runtime.store.set_setting(DISABLED_SETTING, sorted(current))
    return current


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        self._alias_map: dict[str, str] = {}
        self._reserved: set[str] = set()

    # ------------------------------------------------------------------
    def register(self, command: Command) -> Command:
        if command.name in STATE_CHANGING_COMMANDS and not command.changes_state:
            command = replace(command, changes_state=True)
        # Persian names come from one table, so every command speaks both
        # languages without each feature repeating the strings.
        extra = tuple(alias for alias in aliases_for(command.name) if alias not in command.aliases)
        if extra:
            command = replace(command, aliases=command.aliases + extra)
        name = command.name.lower()
        if name in self._commands:
            raise ValueError(f"duplicate command name: {name}")
        for alias in command.aliases:
            alias_lower = alias.lower()
            if alias_lower in self._alias_map or alias_lower in self._commands:
                raise ValueError(f"duplicate command alias: {alias_lower}")
            self._alias_map[alias_lower] = name
        self._commands[name] = command
        return command

    def register_many(self, commands: Iterable[Command]) -> None:
        for command in commands:
            self.register(command)

    def reserved_names(self) -> set[str]:
        return set(self._commands) | set(self._alias_map)

    def resolve(self, name: str) -> Command | None:
        key = (name or "").lower()
        if key in self._commands:
            return self._commands[key]
        target = self._alias_map.get(key)
        return self._commands.get(target) if target else None

    def get(self, name: str) -> Command | None:
        return self._commands.get((name or "").lower())

    def unregister(self, name: str) -> bool:
        """Remove a command and its aliases (used when unloading plugins)."""
        key = name.lower()
        command = self._commands.pop(key, None)
        if command is None:
            return False
        for alias in command.aliases:
            self._alias_map.pop(alias.lower(), None)
        return True

    def all(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: (CATEGORY_ORDER.index(c.category) if c.category in CATEGORY_ORDER else 99, c.name))

    def visible(self) -> list[Command]:
        return [command for command in self.all() if not command.hidden]

    def by_category(self) -> dict[str, list[Command]]:
        grouped: dict[str, list[Command]] = {}
        for command in self.visible():
            grouped.setdefault(command.category, []).append(command)
        return grouped

    def __len__(self) -> int:
        return len(self._commands)

    # ------------------------------------------------------------------
    # Help rendering
    # ------------------------------------------------------------------
    def command_detail(
        self,
        command: Command,
        locales: LocaleManager,
        language: str,
        prefix: str,
        features: dict[str, bool] | None = None,
    ) -> str:
        def t(key: str, default: str = "") -> str:
            return locales.t(key, language, default=default)

        description = t(command.description_key, default=command.description or command.name)
        usage = t(command.usage_key, default=command.usage)
        icon = ui.CATEGORY_ICONS.get(command.category, "◇")
        blocks = [ui.title(icon, prefix + command.name), ui.note(description)]

        facts: list[str] = []
        if usage:
            facts.append(f"{ui.bold(t('help.usage', 'Usage'))} {code(prefix + command.name + ' ' + usage)}")
        if command.aliases:
            aliases = ", ".join(prefix + alias for alias in command.aliases)
            facts.append(f"{ui.bold(t('help.aliases', 'Aliases'))} {code(aliases)}")
        if command.parameters:
            facts.append(ui.bold(t("help.parameters", "Parameters")))
            facts.extend(f"  {ui.note('•')} {escape(p)}" for p in command.parameters)
        if facts:
            blocks.append(ui.panel(facts))

        examples = [locales.t(f"{command.name}.{index}", language, default="") for index in range(1)]
        example_list = [example for example in examples if example]
        if command.examples:
            example_list.extend(prefix + example for example in command.examples)
        if example_list:
            blocks.append(
                ui.section(
                    "❯",
                    t("help.examples", "Examples"),
                    [f"  {code(example)}" for example in example_list],
                )
            )

        flags: list[str] = []
        if command.feature:
            feature_enabled = bool((features or {}).get(command.feature))
            if features is None:
                feature_enabled = command.feature in getattr(locales, "enabled_features", set())
            state = ui.state(feature_enabled, on=t("common.on", "on"), off=t("common.off", "off"))
            flags.append(f"{ui.bold(t('help.feature', 'Feature'))} {code(command.feature)} {ui.note(ui.DOT)} {state}")
        if command.needs_confirmation:
            flags.append(f"🔒 {ui.note(t('help.needs_confirmation', 'Asks for confirmation before running.'))}")
        if command.requires_reply:
            flags.append(f"↩️ {ui.note(t('help.requires_reply', 'Must be used as a reply to a message.'))}")
        if flags:
            blocks.append(ui.panel(flags))
        return "\n\n".join(blocks)

    #: Telegram refuses messages over 4096 characters, and 70+ commands with
    #: descriptions do not fit in one. Split at category boundaries instead of
    #: letting the transport cut a line in half.
    HELP_CHUNK_LIMIT = 3600

    def help_sections(
        self, locales: LocaleManager, language: str, prefix: str, features: dict[str, bool] | None = None
    ) -> list[str]:
        """The help index, as one string per message, split between categories."""
        locales.enabled_features = {name for name, on in (features or {}).items() if on}
        # The index opens like every other screen: a mark, the name, then a drawn
        # line. One screen that looks designed and forty that look dumped is worse
        # than neither, so the header is the same shape as an action card.
        heading = ui.title(
            "❥", locales.t("help.header", language, prefix=prefix).replace("— prefix", ui.DOT + " prefix")
        )
        header = f"{heading}\n{ui.drawn_rule(locales.t('help.index', language), glyph=ui.mark())}"
        sections: list[str] = []
        for category, commands in self.by_category().items():
            icon = ui.CATEGORY_ICONS.get(category, "◇")
            rows: list[str] = []
            for command in commands:
                description = locales.t(
                    command.description_key, language, default=command.description or command.name
                )
                marker = "●" if command.feature and (features or {}).get(command.feature) else ""
                rows.append(ui.item(prefix + command.name, description, marker=marker))
            sections.append(
                ui.section(icon, locales.t(f"help.category.{category}", language, default=category.title()), rows)
            )
        footer = ui.footer(locales.t("help.footer", language, prefix=prefix, count=len(self.visible())))

        chunks: list[str] = []
        current: list[str] = [header]
        for section in sections:
            candidate = "\n\n".join([*current, section])
            if len(candidate) > self.HELP_CHUNK_LIMIT and len(current) > 1:
                chunks.append("\n\n".join(current))
                current = [section]
            else:
                current.append(section)
        current.append(footer)
        # The signature: one glyph, on the last screen only.
        chunks.append("\n\n".join(current) + "\n" + ui.end_mark())
        return chunks

    def help_text(
        self, locales: LocaleManager, language: str, prefix: str, features: dict[str, bool] | None = None
    ) -> str:
        return "\n\n".join(self.help_sections(locales, language, prefix, features))

    def search(self, text: str) -> list[Command]:
        needle = text.lower().strip()
        if not needle:
            return []
        matches: list[Command] = []
        for command in self.visible():
            haystack = " ".join([command.name, *command.aliases, command.category, command.feature or ""])
            if needle in haystack:
                matches.append(command)
        return matches


class Dispatcher:
    """Executes commands with permissions, dry-run, confirmation and errors."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    # ------------------------------------------------------------------
    async def handle(self, event: Any, message: Any, parsed: ParsedCommand, language: str) -> bool:
        """Dispatch one parsed command. Returns True when something ran."""
        runtime = self.runtime
        name = parsed.name
        command = runtime.registry.resolve(name)

        # Database aliases (user-defined) are resolved after built-ins so a
        # stored alias can never shadow a real command.
        if command is None:
            target = await runtime.store.resolve_alias(name)
            if target:
                command = runtime.registry.resolve(target)
                if command is not None:
                    parsed = ParsedCommand(
                        prefix=parsed.prefix,
                        name=command.name,
                        subcommand=parsed.subcommand,
                        args=parsed.args,
                        flags=parsed.flags,
                        raw_args=parsed.raw_args,
                        raw=parsed.raw,
                        rest=parsed.rest,
                        rest_raw=parsed.rest_raw,
                    )
                    name = command.name

        sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        # ``is_outgoing`` is the same claim as ``out``: this line came from the
        # account itself, which is also what her own console produces when she runs
        # one of her commands for the owner.
        is_owner = (
            runtime.permissions.is_owner(sender_id)
            or bool(getattr(event, "out", False))
            or bool(getattr(event, "is_outgoing", False))
        )
        base_lang = language

        if command is None:
            await runtime.sender.respond(
                event,
                runtime.t("errors.unknown_command", base_lang, name=escape(name), prefix=runtime.prefix),
                reply=True,
            )
            return False

        if command.owner_only and not is_owner:
            await runtime.sender.respond(event, runtime.t("errors.permission_denied", base_lang))
            return False

        # Switched off by name ("turn off fal"). ``enable`` itself must always be
        # reachable, or a switched-off account would have no way back on.
        if command.name not in {"enable", "help", "commands", "features", "status"} and command.name in await disabled_commands(runtime):
            await runtime.sender.respond(
                event,
                runtime.t("errors.command_disabled", base_lang, name=escape(command.name), prefix=runtime.prefix),
                reply=True,
                dry_run=False,
            )
            return False

        if parsed.subcommand is not None and parsed.subcommand not in command.subcommands:
            parsed = demote_subcommand(parsed)

        context = CommandContext(
            runtime=runtime,
            event=event,
            message=message,
            parsed=parsed,
            language=language,
            command=command,
            dry_run=await runtime.permissions.dry_run_active(),
            is_owner=is_owner,
        )

        # -- confirmation flow -------------------------------------
        if command.needs_confirmation and not context.has_flag("yes") and not context.has_flag("confirm"):
            await runtime.store.set_interactive(
                int(sender_id or 0),
                "confirm",
                command.name,
                {"args": list(parsed.args), "subcommand": parsed.subcommand, "flags": dict(parsed.flags), "chat_id": getattr(event, "chat_id", None), "message_id": getattr(message, "id", None)},
            )
            await runtime.sender.respond(
                event,
                runtime.t("confirm.prompt", base_lang, command=escape(command.name), prefix=runtime.prefix),
                reply=True,
                dry_run=False,
            )
            return False

        # -- per-command cooldown ------------------------------------
        # Checked here, *started* after the handler succeeds. Touching it before
        # the run meant a command that failed on a typo locked itself for its whole
        # cooldown — the stale lock behind "why is it refusing me now?".
        cooldown_key = f"command:{command.name}:{sender_id}"
        if command.cooldown > 0:
            remaining = runtime.limiter.cooldown_remaining(cooldown_key, command.cooldown)
            if remaining > 0:
                await runtime.sender.respond(
                    event,
                    runtime.t("errors.cooldown", base_lang, seconds=f"{remaining:.0f}"),
                    reply=True,
                    dry_run=False,
                )
                return False

        # -- emergency stop ------------------------------------------
        # ``stop`` itself must stay usable, and read-only commands keep working
        # so the user can still inspect state while automation is frozen.
        if command.changes_state and command.name != "stop" and await runtime.store.emergency_stop():
            await runtime.sender.respond(
                event,
                runtime.t("errors.emergency_stop", base_lang, prefix=runtime.prefix),
                reply=True,
                dry_run=False,
            )
            return False

        # -- dry run -------------------------------------------------
        # The switches themselves are exempt: a dry run that also blocks ``.dryrun
        # off`` is a lock with the key inside it.
        if (
            context.dry_run
            and command.changes_state
            and command.name not in {"dryrun", "stop"}
        ):
            await runtime.sender.respond(
                event,
                runtime.t(
                    "dry_run.preview_command",
                    base_lang,
                    command=escape(command.name),
                    args=escape(" ".join(parsed.args) or "—"),
                    prefix=runtime.prefix,
                ),
                reply=True,
                dry_run=False,
            )
            await runtime.store.bump_usage("dry_run", "commands")
            return True

        # -- feature gate --------------------------------------------
        if command.requires_feature and command.feature:
            decision = await runtime.permissions.can_act(context.chat_id, command.feature, is_private=context.is_private)
            if not decision.allowed:
                await runtime.sender.respond(
                    event,
                    runtime.t(
                        "errors.feature_blocked",
                        base_lang,
                        feature=escape(command.feature),
                        reason=runtime.t(f"reason.{decision.code}", base_lang, default=decision.reason),
                    ),
                    reply=True,
                    dry_run=False,
                )
                return False

        # -- reply requirement ---------------------------------------
        if command.requires_reply:
            reply = await runtime.telegram.get_reply_message(message)
            if reply is None:
                await runtime.sender.respond(
                    event,
                    runtime.t("errors.reply_required", base_lang, prefix=runtime.prefix, name=escape(command.name)),
                    reply=True,
                    dry_run=False,
                )
                return False
            message._havoc_reply = reply

        ran = await self.execute(context)
        if ran and command.cooldown > 0:
            runtime.limiter.touch(cooldown_key)
        return ran

    # ------------------------------------------------------------------
    async def execute(self, context: CommandContext) -> bool:
        runtime = self.runtime
        command = context.command
        language = context.language
        start = time.monotonic()
        sent_before = int(getattr(runtime.sender, "sent_count", 0) or 0)
        try:
            await command.handler(context)
            runtime.stats["commands_ok"] += 1
            await runtime.store.bump_usage(command.feature or command.name, "calls")
            # A command that answers nothing is a dead end: the person typed
            # `.calc`, pressed send and got silence. If the handler said nothing and
            # the command has arguments, show what it wants. One net here beats
            # remembering to add a usage line to every handler that forgets.
            if (
                int(getattr(runtime.sender, "sent_count", 0) or 0) == sent_before
                and (command.usage or "").strip()
            ):
                await runtime.sender.respond(
                    context.event,
                    runtime.t(
                        "errors.needs_arguments",
                        language,
                        usage=escape(context.usage_text()),
                        prefix=escape(runtime.prefix),
                        name=escape(command.name),
                    ),
                    reply=True,
                    dry_run=False,
                )
            return True
        except CommandError as exc:
            await runtime.sender.respond(context.event, runtime.t(exc.key, language, **exc.variables), reply=True)
            runtime.stats["commands_failed"] += 1
            return False
        except ValidationError as exc:
            await runtime.sender.respond(
                context.event, runtime.t(exc.code, language, **exc.params), reply=True
            )
            runtime.stats["commands_failed"] += 1
            return False
        except ParseError as exc:
            await runtime.sender.respond(
                context.event,
                runtime.t("errors.parse_error", language, detail=escape(str(exc)), usage=escape(context.usage_text())),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        except TimeParseError as exc:
            await runtime.sender.respond(
                context.event,
                runtime.t("errors.invalid_time", language, detail=escape(str(exc)), example="30m"),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        except RateLimited as exc:
            await runtime.sender.respond(
                context.event,
                runtime.t("errors.rate_limited", language, seconds=f"{exc.retry_after:.0f}"),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        except SendError as exc:
            await runtime.sender.respond(
                context.event,
                runtime.t("errors.send_failed", language, detail=escape(str(exc))),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        except (AIError, TranslationError) as exc:
            kind = getattr(exc, "kind", "provider_error")
            await runtime.sender.respond(
                context.event,
                runtime.t(f"provider.{kind}", language, detail=escape(str(exc)[:200])),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        except DatabaseError as exc:
            runtime.logger.exception("database error while running %s", command.name)
            await runtime.sender.respond(
                context.event,
                runtime.t("errors.database_error", language, detail=escape(str(exc)[:200])),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        except (MissingTranslationError, LocaleFormatError) as exc:
            runtime.logger.error("localization error: %s", exc)
            await runtime.sender.respond(context.event, runtime.t("errors.localization_error", language), reply=True)
            runtime.stats["commands_failed"] += 1
            return False
        except Exception as exc:  # noqa: BLE001 - isolated, reported, never swallowed silently
            runtime.logger.exception("command %s failed", command.name)
            runtime.record_error(f"command:{command.name}", exc)
            await runtime.sender.respond(
                context.event,
                runtime.t("errors.unexpected", language, detail=escape(type(exc).__name__)),
                reply=True,
            )
            runtime.stats["commands_failed"] += 1
            return False
        finally:
            elapsed = (time.monotonic() - start) * 1000
            runtime.metrics.observe_command(command.name, elapsed)

    # ------------------------------------------------------------------
    async def handle_confirmation(self, event: Any, message: Any, text: str, language: str) -> bool:
        """Resolve a pending ``confirm`` flow for the owner."""
        runtime = self.runtime
        sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        if not sender_id:
            return False
        session = await runtime.store.get_interactive(int(sender_id))
        if session is None:
            return False
        # Other flows (the settings menu) own their own state machine; the
        # router only knows to hand them the reply while one is open.
        if session["flow"] == "ai":
            menu = getattr(runtime, "ai_menu", None)
            if menu is None:
                return False
            return await menu.handle(event, message, text, language, session)
        if session["flow"] == "settings":
            menu = getattr(runtime, "settings_menu", None)
            if menu is None:
                return False
            return await menu.handle(event, message, text, language, session)
        if session["flow"] != "confirm":
            return False
        answers_yes = runtime.t("confirm.yes_words", language).split("|")
        answers_no = runtime.t("confirm.no_words", language).split("|")
        normalized = text.strip().lower()
        if normalized in {word.strip().lower() for word in answers_yes}:
            data = runtime.decode_json(session["data"])
            await runtime.store.clear_interactive(int(sender_id))
            command = runtime.registry.get(session["step"])
            if command is None:
                return False
            parsed = ParsedCommand(
                prefix=runtime.prefix,
                name=command.name,
                subcommand=data.get("subcommand"),
                args=tuple(data.get("args") or ()),
                flags={**(data.get("flags") or {}), "yes": "true"},
                raw_args=" ".join(data.get("args") or ()),
                raw=text,
                rest=tuple(data.get("args") or ()),
                rest_raw=" ".join(data.get("args") or ()),
            )
            context = CommandContext(
                runtime=runtime,
                event=event,
                message=message,
                parsed=parsed,
                language=language,
                command=command,
                dry_run=await runtime.permissions.dry_run_active(),
            )
            return await self.execute(context)
        if normalized in {word.strip().lower() for word in answers_no}:
            await runtime.store.clear_interactive(int(sender_id))
            await runtime.sender.respond(event, runtime.t("confirm.cancelled", language), reply=True)
            return True
        return False
