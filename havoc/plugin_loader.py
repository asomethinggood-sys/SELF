"""Loader for *local, trusted* plugins.

Security stance (spec §9)
-------------------------
* Plugins are plain Python files the owner places in ``PLUGIN_DIR``. They are
  code running with full process privileges — there is no sandbox. The loader
  therefore never downloads anything, never evaluates plugin metadata as code,
  rejects symlinks pointing outside the plugin directory, and refuses files
  that fail validation or define ``setup`` incorrectly.
* A failing plugin is isolated: it is recorded as failed, the rest still load,
  and the error is visible through ``.plugins`` and ``.selfcheck``.

Feature areas: 88, 99.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils.security import ensure_within, redact_text

PLUGIN_FILENAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\.py$")
MAX_PLUGIN_BYTES = 512 * 1024
REQUIRED_ATTRIBUTES = ("PLUGIN_NAME", "PLUGIN_VERSION")
FORBIDDEN_MODULES = ("ctypes", "multiprocessing", "pty")


class PluginError(Exception):
    pass


@dataclass
class PluginAPI:
    """The stable surface plugins are allowed to use."""

    runtime: Any
    name: str
    logger: Any
    version: str = "0"

    # -- commands ----------------------------------------------------
    def register_command(self, **kwargs: Any) -> Any:
        from .command_registry import Command

        kwargs.setdefault("category", "management")
        kwargs["name"] = str(kwargs["name"]).lower()
        command = Command(**kwargs)
        self.runtime.registry.register(command)
        tracked = getattr(self, "tracked_commands", None)
        if isinstance(tracked, list):
            tracked.append(command.name)
        self.logger.info("plugin %s registered command %s", self.name, command.name)
        return command

    # -- events ------------------------------------------------------
    def subscribe(self, name: str, handler: Callable[..., Awaitable[None]], event_types: Iterable[Any] | None = None, priority: int = 200) -> Any:
        full_name = f"plugin:{self.name}:{name}"
        subscription = self.runtime.event_engine.subscribe(
            name=full_name,
            handler=handler,
            event_types=event_types,
            priority=priority,
            description=f"plugin {self.name}",
        )
        tracked = getattr(self, "tracked_subscriptions", None)
        if isinstance(tracked, list):
            tracked.append(full_name)
        return subscription

    # -- localization ------------------------------------------------
    def add_strings(self, language: str, mapping: dict[str, Any]) -> None:
        self.runtime.locales.add_catalog(language, mapping)

    # -- storage -----------------------------------------------------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        return await self.runtime.store.get_setting(f"plugin.{self.name}.{key}", default)

    async def set_setting(self, key: str, value: Any) -> None:
        await self.runtime.store.set_setting(f"plugin.{self.name}.{key}", value)

    # -- helpers -----------------------------------------------------
    @property
    def prefix(self) -> str:
        return self.runtime.prefix

    def t(self, key: str, language: str = "en", **variables: Any) -> str:
        return self.runtime.locales.t(key, language, **variables)


@dataclass
class PluginRecord:
    name: str
    version: str
    path: Path
    loaded: bool = False
    error: str = ""


@dataclass
class PluginLoader:
    plugin_dir: Path
    runtime: Any
    logger: Any
    enabled: bool = True
    records: dict[str, PluginRecord] = field(default_factory=dict)
    _modules: dict[str, Any] = field(default_factory=dict)
    _commands: dict[str, list[str]] = field(default_factory=dict)
    _subscriptions: dict[str, list[str]] = field(default_factory=dict)

    async def load_all(self) -> list[PluginRecord]:
        if not self.enabled:
            self.logger.info("plugin loading is disabled by configuration")
            return []
        directory = self.plugin_dir
        if not directory.exists():
            self.logger.info("plugin directory %s does not exist; skipping", directory)
            return []
        try:
            directory = ensure_within(directory, directory)
        except ValueError as exc:  # pragma: no cover - defensive
            raise PluginError(str(exc)) from exc

        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            if not PLUGIN_FILENAME_RE.match(path.name):
                self.logger.warning("skipping plugin with unsafe filename: %s", path.name)
                continue
            try:
                ensure_within(directory, path)
            except ValueError:
                self.logger.warning("skipping plugin outside the plugin directory: %s", path)
                continue
            record = await self._load_one(path)
            self.records[record.name] = record
        return list(self.records.values())

    async def _load_one(self, path: Path) -> PluginRecord:
        name = path.stem
        try:
            size = path.stat().st_size
            if size > MAX_PLUGIN_BYTES:
                raise PluginError(f"plugin is larger than {MAX_PLUGIN_BYTES} bytes")
            metadata = _read_metadata(path)
            plugin_name = str(metadata.get("PLUGIN_NAME") or name)
            version = str(metadata.get("PLUGIN_VERSION") or "0")
            for forbidden in FORBIDDEN_MODULES:
                if re.search(rf"^\s*(?:import|from)\s+{forbidden}\b", path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE):
                    raise PluginError(f"plugin imports disallowed module {forbidden!r}")

            module = await asyncio.to_thread(_import_module, path, name)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise PluginError("plugin must define a callable setup(api)")
            api = PluginAPI(runtime=self.runtime, name=plugin_name, logger=self.logger, version=version)
            api.tracked_commands = self._commands.setdefault(plugin_name, [])
            api.tracked_subscriptions = self._subscriptions.setdefault(plugin_name, [])
            result = setup(api)
            if asyncio.iscoroutine(result):
                await result
            self._modules[plugin_name] = module
            await self.runtime.store.record_plugin(plugin_name, version, True, None)
            self.logger.info("plugin loaded: %s v%s (%s)", plugin_name, version, path.name)
            return PluginRecord(name=plugin_name, version=version, path=path, loaded=True)
        except Exception as exc:  # noqa: BLE001 - isolation by design
            message = redact_text(f"{type(exc).__name__}: {exc}")[:300]
            self.logger.error("plugin %s failed to load: %s", path.name, message)
            try:
                await self.runtime.store.record_plugin(name, None, False, message)
            except Exception:  # pragma: no cover
                self.logger.exception("could not record plugin failure")
            return PluginRecord(name=name, version="0", path=path, loaded=False, error=message)

    async def unload(self, name: str) -> bool:
        """Remove a plugin and everything it registered.

        Leaving its commands and event subscribers behind would keep calling
        code from a module the user asked to disable.
        """
        module = self._modules.pop(name, None)
        if module is None:
            return False
        sys.modules.pop(getattr(module, "__name__", ""), None)
        for command_name in self._commands.pop(name, []):
            self.runtime.registry.unregister(command_name)
        for subscription_name in self._subscriptions.pop(name, []):
            self.runtime.event_engine.unsubscribe(subscription_name)
        record = self.records.pop(name, None)
        if record:
            record.loaded = False
        await self.runtime.store.record_plugin(name, getattr(record, "version", None), False, "unloaded")
        return True

    @property
    def loaded(self) -> list[PluginRecord]:
        return [record for record in self.records.values() if record.loaded]

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": record.name,
                "version": record.version,
                "loaded": record.loaded,
                "error": record.error,
                "file": record.path.name,
            }
            for record in self.records.values()
        ]


def _import_module(path: Path, name: str) -> Any:
    module_name = f"havoc_plugin_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError("could not create module spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _read_metadata(path: Path) -> dict[str, Any]:
    """Read UPPER_CASE literals without executing the module.

    A tiny ``ast`` walk keeps metadata parsing side-effect free even if the
    plugin body would do something on import.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise PluginError(f"syntax error: {exc}") from exc
    metadata: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            key = target.id
            # Accept both ``PLUGIN_NAME`` and the more Pythonic
            # ``__plugin_name__`` spelling; normalise to the upper form.
            if key.startswith("__") and key.endswith("__"):
                key = key.strip("_").upper()
            if key not in REQUIRED_ATTRIBUTES and not key.startswith("PLUGIN_"):
                continue
            try:
                metadata[key] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
    missing = [attr for attr in REQUIRED_ATTRIBUTES if attr not in metadata]
    if missing and not _has_setup(tree):
        raise PluginError(f"missing metadata: {', '.join(missing)}")
    return metadata


def _has_setup(tree: Any) -> bool:
    import ast

    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setup" for node in tree.body)
