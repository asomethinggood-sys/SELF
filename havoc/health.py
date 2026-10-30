"""Health, diagnostics and self-check service.

Collects the operational picture the owner needs: uptime, connection state,
database health, scheduler health, queue/latency metrics, recent sanitized
errors and integration availability. It also powers ``.selfcheck``, which
runs real checks (writable directories, schema version, locale parity, command
documentation, plugin load state) — never a hard-coded "all good".

Feature areas: 10, 81, 83, 88, 89, 100.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .localization import LocaleManager
from .utils.security import redact_text
from .utils.time import iso, utc_now


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"  # error | warning

    def render(self, mark_ok: str = "✓", mark_bad: str = "✗", mark_warn: str = "⚠️") -> str:
        mark = mark_ok if self.ok else (mark_warn if self.severity == "warning" else mark_bad)
        return f"{mark} {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class Metrics:
    command_latencies: dict[str, deque] = field(default_factory=dict)
    counters: Counter = field(default_factory=Counter)
    started_at: float = field(default_factory=time.monotonic)

    def observe_command(self, name: str, milliseconds: float) -> None:
        series = self.command_latencies.setdefault(name, deque(maxlen=200))
        series.append(milliseconds)
        self.counters["commands"] += 1

    def count(self, key: str, amount: int = 1) -> None:
        self.counters[key] += amount

    def average(self, name: str) -> float:
        series = self.command_latencies.get(name)
        if not series:
            return 0.0
        return round(sum(series) / len(series), 2)

    def slowest(self, limit: int = 3) -> list[tuple[str, float]]:
        averages = [(name, self.average(name)) for name in self.command_latencies]
        return sorted(averages, key=lambda item: item[1], reverse=True)[:limit]

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "commands_measured": len(self.command_latencies),
            "slowest": self.slowest(),
        }


@dataclass
class Health:
    config: Any
    logger: Any
    store: Any
    db: Any
    locales: LocaleManager
    debug: bool = False
    connected: bool = False
    started_at: datetime = field(default_factory=utc_now)
    errors: deque = field(default_factory=lambda: deque(maxlen=25))
    error_counts: Counter = field(default_factory=Counter)
    metrics: Metrics = field(default_factory=Metrics)
    reconnects: int = 0
    disconnects: int = 0
    flood_waits: int = 0
    last_error_at: datetime | None = None
    _last_db_check: float = 0.0
    _db_ok: bool = True
    _db_latency_ms: float = 0.0

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def mark_connected(self) -> None:
        self.connected = True

    def mark_disconnected(self) -> None:
        self.connected = False
        self.disconnects += 1

    def mark_reconnect(self) -> None:
        self.connected = True
        self.reconnects += 1

    def mark_flood_wait(self) -> None:
        self.flood_waits += 1

    def record_error(self, source: str, exc: BaseException | str) -> None:
        message = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        entry = {
            "at": iso(utc_now()),
            "source": source,
            "message": redact_text(message)[:300],
        }
        self.errors.append(entry)
        self.error_counts[source] += 1
        self.last_error_at = utc_now()
        try:
            self.logger.error("health error [%s]: %s", source, entry["message"])
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @property
    def uptime_seconds(self) -> float:
        return max(0.0, (utc_now() - self.started_at).total_seconds())

    @property
    def uptime_human(self) -> str:
        seconds = int(self.uptime_seconds)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        if minutes or hours or days:
            parts.append(f"{minutes}m")
        parts.append(f"{secs}s")
        return " ".join(parts)

    async def database_health(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and now - self._last_db_check < 30:
            return {"ok": self._db_ok, "latency_ms": self._db_latency_ms}
        started = time.monotonic()
        try:
            await self.db.scalar("SELECT 1")
            self._db_ok = True
        except Exception as exc:  # noqa: BLE001
            self._db_ok = False
            self.record_error("database", exc)
        self._db_latency_ms = round((time.monotonic() - started) * 1000, 2)
        self._last_db_check = now
        return {"ok": self._db_ok, "latency_ms": self._db_latency_ms}

    async def snapshot(self, runtime: Any = None) -> dict[str, Any]:
        db_health = await self.database_health()
        data: dict[str, Any] = {
            "uptime": self.uptime_human,
            "uptime_seconds": int(self.uptime_seconds),
            "connected": self.connected,
            "reconnects": self.reconnects,
            "disconnects": self.disconnects,
            "flood_waits": self.flood_waits,
            "database": db_health,
            "db_size": await self.db.db_size_bytes(),
            "schema_version": await self.db.schema_version(),
            "errors": list(self.errors)[-5:],
            "error_counts": dict(self.error_counts),
            "metrics": self.metrics.snapshot(),
            "dry_run": getattr(self.config, "dry_run", False),
            "name": "havoc",
        }
        if runtime is not None:
            data["event_engine"] = runtime.event_engine.snapshot()
            data["scheduler"] = runtime.scheduler.snapshot()
            data["rate_limiter"] = runtime.limiter.snapshot()
            data["features"] = await runtime.feature_manager.refresh()
            data["ai"] = runtime.ai.describe()
            data["translation"] = runtime.translator.describe()
        return data

    # ------------------------------------------------------------------
    # Self check
    # ------------------------------------------------------------------
    async def self_check(self, runtime: Any = None) -> list[CheckResult]:
        results: list[CheckResult] = []
        config = self.config

        # Directories
        for label, path in (
            ("data", config.data_dir),
            ("logs", config.log_dir),
            ("backups", config.backup_dir),
        ):
            results.append(_check_writable(label, path))

        # Session file permissions
        session = config.session_file
        if session.exists():
            mode = session.stat().st_mode & 0o777
            results.append(
                CheckResult(
                    "session file",
                    ok=(mode & 0o077) == 0,
                    detail=f"{session} mode {oct(mode)}",
                    severity="warning",
                )
            )
        elif config.session_string:
            results.append(CheckResult("session file", True, "using SESSION_STRING"))
        else:
            results.append(CheckResult("session file", False, "not created yet (first login pending)", "warning"))

        # Database
        db_health = await self.database_health(force=True)
        results.append(CheckResult("database", ok=db_health["ok"], detail=f"{config.database_path} ({db_health['latency_ms']} ms)"))
        if db_health["ok"]:
            integrity = await self.db.integrity_check()
            results.append(CheckResult("sqlite integrity", integrity == "ok", detail=integrity))
            version = await self.db.schema_version()
            from .migrations import LATEST_VERSION

            results.append(
                CheckResult(
                    "schema",
                    version == LATEST_VERSION,
                    detail=f"version {version} (expected {LATEST_VERSION})",
                )
            )

        # Localization
        problems = self.locales.validate()
        results.append(
            CheckResult(
                "localization",
                not problems,
                "; ".join(problems[:2]) if problems else f"{len(self.locales.languages)} languages, {len(self.locales.keys('en'))} keys",
            )
        )

        # Commands documented in both languages
        if runtime is not None:
            missing_docs: list[str] = []
            for command in runtime.registry.visible():
                for language in ("en", "fa"):
                    if not self.locales.has(command.description_key, language):
                        missing_docs.append(f"{command.name}:{language}")
            results.append(
                CheckResult(
                    "command documentation",
                    not missing_docs,
                    ", ".join(missing_docs[:5]) if missing_docs else f"{len(runtime.registry.visible())} commands",
                )
            )

            # Features whose dependencies are missing
            feature_problems = await runtime.feature_manager.validate()
            results.append(
                CheckResult("features", not feature_problems, "; ".join(feature_problems[:3]) if feature_problems else "consistent")
            )

            # Scheduler
            results.append(
                CheckResult(
                    "scheduler",
                    runtime.scheduler.running,
                    f"{runtime.scheduler.stats.ticks} ticks, {runtime.scheduler.stats.failures} failures",
                    severity="warning",
                )
            )

            # Plugins
            try:
                plugins = await runtime.store.list_plugins()
                failed = [row["name"] for row in plugins if row["error"]]
                results.append(
                    CheckResult(
                        "plugins",
                        not failed,
                        f"{len(plugins)} loaded" + (f"; failed: {', '.join(failed)}" if failed else ""),
                        severity="warning",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                results.append(CheckResult("plugins", False, str(exc), "warning"))

            # Telethon availability
            results.append(
                CheckResult(
                    "telethon",
                    runtime.telethon_available,
                    "installed" if runtime.telethon_available else "not installed",
                )
            )

            # Integrations are optional: report, never fail the check
            results.append(
                CheckResult(
                    "ai provider",
                    True,
                    "configured" if runtime.ai.available else "disabled (optional)",
                    severity="warning",
                )
            )
            results.append(
                CheckResult(
                    "translation provider",
                    True,
                    "configured" if runtime.translator.available else "disabled (optional)",
                    severity="warning",
                )
            )

        # Config leftovers
        issues = [i for i in getattr(config, "_issues", [])]
        if issues:
            results.append(CheckResult("configuration", False, "; ".join(str(i) for i in issues[:3])))

        return results

    # ------------------------------------------------------------------
    def render_errors(self, limit: int = 5) -> str:
        if not self.errors:
            return ""
        lines = []
        for entry in list(self.errors)[-limit:]:
            lines.append(f"{entry['at']} [{entry['source']}] {entry['message']}")
        return "\n".join(lines)


def _check_writable(label: str, path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".havoc-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult(f"{label} directory", True, str(path))
    except OSError as exc:
        return CheckResult(f"{label} directory", False, f"{path}: {exc}")


def summarize_checks(results: Iterable[CheckResult]) -> tuple[bool, int, int]:
    """Return ``(all_ok, failures, warnings)``."""
    failures = sum(1 for r in results if not r.ok and r.severity == "error")
    warnings = sum(1 for r in results if not r.ok and r.severity == "warning")
    return failures == 0, failures, warnings






