"""The runtime container that wires every subsystem together.

There is exactly one :class:`Runtime` per process. Features receive it through
their command context instead of importing globals, so tests can build a
runtime without Telegram and swap any dependency.

Startup order matters and is explicit:
config → logging → directories → database + migrations → defaults → locales →
providers → registry → features → permissions → scheduler → event engine →
plugins → Telegram client → handlers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .command_registry import CommandRegistry, Dispatcher
from .config import Config, ensure_runtime_dirs
from .database import Database
from .event_engine import EventEngine, EventType, now_event
from .feature_manager import FeatureManager, builtin_features
from .health import Health, Metrics
from .language_detection import detect_language, resolve_language
from .localization import LocaleManager, MissingTranslationError, normalize_language_code
from .migrations import apply_migrations
from .permissions import Permissions
from .plugin_loader import PluginLoader
from .scheduler import Scheduler
from .services.ai_provider import AIProvider
from .services.ai_router import AIRouter
from .services.fight_loop import FightLoop
from .services.memory import MemoryStore
from .services.rate_limiter import RateLimiter
from .services.sender import Sender
from .services.storage import Store
from .services.translation_provider import build_translation_provider
from .services.yura_chat import YuraChat
from .utils.security import redact_text
from .utils.time import utc_now


class RuntimeError_(Exception):
    """Fatal runtime initialisation problem."""


@dataclass
class Runtime:
    config: Config
    logger: Any
    db: Database
    locales: LocaleManager
    store: Store
    limiter: RateLimiter
    permissions: Permissions
    features: FeatureManager
    registry: CommandRegistry
    dispatcher: Dispatcher
    event_engine: EventEngine
    scheduler: Scheduler
    health: Health
    metrics: Metrics
    ai: AIProvider
    translator: Any
    sender: Sender
    telegram: Any = None
    client: Any = None
    memory: MemoryStore = None  # type: ignore[assignment] - set in create()
    yura_chat: YuraChat = None  # type: ignore[assignment] - set in create()
    fight_loop: FightLoop = None  # type: ignore[assignment] - set in create()
    plugin_loader: PluginLoader | None = None
    stats: dict[str, int] = field(default_factory=dict)
    started_at: Any = field(default_factory=utc_now)
    feature_modules: tuple[Any, ...] = ()
    _client_info: Any = None
    _prefix_override: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    async def create(cls, config: Config, logger: Any, *, connect_db: bool = True) -> Runtime:
        ensure_runtime_dirs(config)

        db = Database(config.database_path)
        if connect_db:
            await db.connect()
            await apply_migrations(db)

        store = Store(db)
        await store.ensure_features()

        locales = LocaleManager().load()
        limiter = RateLimiter(outbound_limit=config.outbound_rate_limit)
        permissions = Permissions(config, store)

        # Several keys across several providers, rotated with persisted budgets
        # and health. The router duck-types AIProvider, so every feature that
        # asked `runtime.ai` for a sentence keeps working without knowing about
        # the second key.
        ai: Any = await AIRouter.load(config, store, logger)
        if not ai.available:
            # A plain single-provider setup still works, and so does a lone key
            # pasted into the chat (`.yura key …`), which lives in the database.
            ai = AIProvider.from_config(config)
            stored_key = await store.get_setting("ai.api_key", None)
            if not ai.available and stored_key and str(config.ai_provider) in {"openai_compatible", "none"}:
                ai = AIProvider(
                    base_url=config.ai_base_url,
                    api_key=str(stored_key),
                    model=config.ai_model,
                    timeout=config.ai_timeout,
                    enabled=True,
                )
        translator = build_translation_provider(config)
        feature_manager = FeatureManager(
            store,
            config=config,
            ai_available=ai.available,
            translation_available=translator.available,
        )
        feature_manager.register_many(builtin_features(config))
        await feature_manager.ensure_defaults()

        registry = CommandRegistry()
        event_engine = EventEngine(logger=logger, debug=config.debug)
        health = Health(
            config=config,
            logger=logger,
            store=store,
            db=db,
            locales=locales,
            debug=config.debug,
        )
        health.connected = False

        # The sender is created without a client; ``attach_client`` fills it in.
        sender = Sender(None, config, limiter, logger)

        async def _scheduler_error(kind: str, payload: dict[str, Any], exc: BaseException) -> None:
            health.record_error(f"scheduler:{payload.get('scope', kind) if payload else kind}", exc)

        scheduler = Scheduler(
            store=store,
            logger=logger,
            config=config,
            on_error=_scheduler_error,
            missed_policy=config.missed_job_policy,
        )

        runtime = cls(
            config=config,
            logger=logger,
            db=db,
            locales=locales,
            store=store,
            limiter=limiter,
            permissions=permissions,
            features=feature_manager,
            registry=registry,
            dispatcher=None,  # type: ignore[arg-type] - installed right below
            event_engine=event_engine,
            scheduler=scheduler,
            health=health,
            metrics=health.metrics,
            ai=ai,
            translator=translator,
            sender=sender,
            stats={"commands_ok": 0, "commands_failed": 0, "events": 0},
        )
        runtime.dispatcher = Dispatcher(runtime)
        runtime.memory = MemoryStore(db)
        runtime.yura_chat = YuraChat(runtime)
        runtime.fight_loop = FightLoop(runtime)
        from .handlers import Router

        # The router is stateless apart from counters; attaching it here keeps
        # tests and the CLI using the exact same handler object.
        runtime.handlers = Router(runtime)
        stored_prefix = await store.get_setting("command_prefix", None)
        if stored_prefix:
            runtime._prefix_override = str(stored_prefix)
        return runtime

    # ------------------------------------------------------------------
    # Feature wiring
    # ------------------------------------------------------------------
    def load_features(self) -> None:
        from . import features as feature_package

        modules = []
        for module in feature_package.all_feature_modules():
            register = getattr(module, "register", None)
            if register is None:
                self.logger.warning("feature module %s has no register()", module.__name__)
                continue
            register(self)
            modules.append(module)
        self.feature_modules = tuple(modules)
        self.logger.info("registered %s commands from %s feature modules", len(self.registry), len(modules))

    def attach_client(self, telegram: Any, client: Any, info: Any = None) -> None:
        self.telegram = telegram
        self.client = client
        self._client_info = info
        self.sender.client = client
        if telegram is not None and getattr(telegram, "me", None) is not None:
            self.permissions.set_self_id(getattr(telegram.me, "id", None))

    # ------------------------------------------------------------------
    # Localization
    # ------------------------------------------------------------------
    def t(self, key: str, language: str = "en", /, *, digits: bool = False, default: str | None = None, **variables: Any) -> str:
        """Translate with graceful degradation and no silent key loss."""
        try:
            return self.locales.t(key, normalize_language_code(language), localize_digits=digits, **variables)
        except MissingTranslationError:
            self.logger.warning("missing translation key: %s (%s)", key, language)
            if default is not None:
                return default
            return key
        except Exception as exc:  # noqa: BLE001 - interpolation bug
            self.logger.error("localization failure for %s: %s", key, exc)
            self.health.record_error("localization", exc)
            return key

    async def resolve_language(
        self,
        text: str | None = None,
        *,
        chat_id: int | None = None,
        sender_id: int | None = None,
    ) -> str:
        """Choose the reply language: chat → user → global → detection."""
        if chat_id is not None:
            pref = await self.store.get_language("chat", str(chat_id))
            if pref and pref != "auto":
                return normalize_language_code(pref, self.config.default_language)
        if sender_id is not None:
            pref = await self.store.get_language("user", str(sender_id))
            if pref and pref != "auto":
                return normalize_language_code(pref, self.config.default_language)
        pref = await self.store.get_language("global", "") or self.config.default_language
        if pref in ("en", "fa"):
            return pref
        # auto (or unset): detect from the message, keep the configured fallback
        fallback = self.config.default_language
        return resolve_language(text, preference="auto", fallback=fallback)

    async def language_estimate(self, text: str | None) -> Any:
        return detect_language(text)

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    @property
    def prefix(self) -> str:
        return self._prefix_override or self.config.command_prefix

    # ``features`` is the manager instance; features and health code often use
    # the longer name, so keep both spellings working.
    @property
    def feature_manager(self) -> Any:
        return self.features

    async def set_prefix(self, value: str | None) -> str:
        """Change the command prefix at runtime and persist it.

        ``None`` resets to the configured default. Raises ``ValueError`` for
        unusable prefixes so the command layer can localize the message.
        """
        if value is None:
            self._prefix_override = None
            await self.store.delete_setting("command_prefix")
            return self.prefix
        candidate = value.strip()
        if not candidate or len(candidate) > 4 or any(ch.isspace() for ch in candidate):
            raise ValueError("prefix must be 1-4 characters without spaces")
        if any(ch.isalnum() for ch in candidate):
            raise ValueError("prefix should be punctuation so normal words do not trigger commands")
        self._prefix_override = candidate
        await self.store.set_setting("command_prefix", candidate)
        return candidate

    def decode_json(self, raw: Any, default: Any = None) -> Any:
        if raw is None:
            return default if default is not None else {}
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default if default is not None else {}

    def record_error(self, source: str, exc: BaseException | str) -> None:
        self.health.record_error(source, exc)

    async def notify_owner(self, text: str, **kwargs: Any) -> Any:
        try:
            return await self.sender.notify_owner(text, **kwargs)
        except Exception as exc:  # noqa: BLE001 - notifications must not cascade
            self.logger.warning("could not notify owner: %s", redact_text(str(exc)))
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start_background(self) -> None:
        await self.event_engine.start()
        await self.scheduler.start()
        if self.plugin_loader is not None:
            records = await self.plugin_loader.load_all()
            failed = [record for record in records if not record.loaded]
            if failed:
                self.logger.warning("%s plugin(s) failed to load", len(failed))
        await self.event_engine.emit(now_event(EventType.STARTUP, extra={"version": "2.0.0"}))

    async def shutdown(self) -> None:
        self.logger.info("shutting down")
        try:
            await self.event_engine.emit(now_event(EventType.SHUTDOWN))
        except Exception:  # pragma: no cover
            pass
        if self.fight_loop is not None:
            try:
                await self.fight_loop.stop_all()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("error stopping fight loops: %s", exc)
        await self.scheduler.stop()
        await self.event_engine.stop()
        if self.telegram is not None:
            try:
                await self.telegram.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("error while disconnecting: %s", exc)
        try:
            await self.db.close()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("error closing database: %s", exc)
        self.logger.info("shutdown complete")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    @property
    def telethon_available(self) -> bool:
        from .client import telethon_available

        return telethon_available()

    async def snapshot(self) -> dict[str, Any]:
        return await self.health.snapshot(self)

    async def self_check(self) -> list[Any]:
        return await self.health.self_check(self)
