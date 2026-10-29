"""Command-line entry point.

Commands
--------
``havoc run``      start the userbot (default)
``havoc login``    authenticate once and store the session
``havoc check``    validate configuration and run the self-check
``havoc migrate``  apply pending database migrations
``havoc version``  print version information

Everything is explicit and non-interactive by default except the login step,
which is the only place HAVOC reads from the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__, snapshot
from .client import (
    AuthError,
    LoginChallenge,
    TelegramService,
    mask_phone,
    session_permissions_warning,
    telethon_available,
)
from .config import Config, ConfigIssue, format_issues, has_blocking, load_config
from .database import Database, DatabaseError
from .health import summarize_checks
from .localization import LocaleManager
from .logging_setup import get_logger, setup_logging
from .migrations import LATEST_VERSION, apply_migrations, current_version
from .runtime import Runtime
from .version import CODENAME

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_RUNTIME = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def project_env_file() -> Path:
    return Path(os.environ.get("HAVOC_ENV_FILE", ".env")).expanduser()


def load_config_or_report(require_credentials: bool = False) -> tuple[Config, list[ConfigIssue]]:
    config, issues = load_config(dotenv_path=project_env_file())
    blocking = [issue for issue in issues if issue.severity == "error"]
    if require_credentials and not config.has_credentials and not config.session_string:
        blocking.append(
            ConfigIssue(
                "API_ID/API_HASH",
                "missing — create an app at https://my.telegram.org and copy the values into .env",
            )
        )
    if blocking:
        print(f"{CODENAME} cannot start because the configuration is incomplete:", file=sys.stderr)
        print(format_issues(blocking), file=sys.stderr)
        print(
            "\nتنظیمات ناقص است. فایل .env را از .env.example بسازید و API_ID و API_HASH را وارد کنید.",
            file=sys.stderr,
        )
    return config, issues


def make_logger(config: Config):
    return setup_logging(
        log_dir=config.log_dir,
        level=config.log_level,
        debug=config.debug,
        secrets=[config.api_hash, config.ai_api_key, config.libretranslate_api_key, config.session_string],
    )


async def guarded_shutdown(runtime: Runtime, stop_event: asyncio.Event, run_task: asyncio.Task[Any]) -> None:
    done, _pending = await asyncio.wait({run_task, asyncio.create_task(stop_event.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if run_task in done:
        exc = run_task.exception()
        if exc is not None:
            raise exc
    runtime.logger.info("stop requested; shutting down")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def command_run(args: argparse.Namespace) -> int:
    config, issues = load_config_or_report(require_credentials=True)
    if has_blocking(issues):
        return EXIT_CONFIG
    logger = make_logger(config)
    logger.info("%s %s starting", CODENAME, __version__)

    if not telethon_available():
        logger.error("Telethon is not installed. Install it with: pip install -e .")
        return EXIT_CONFIG
    for warning in (session_permissions_warning(config.session_file),):
        if warning:
            logger.warning(warning)

    runtime = await Runtime.create(config, logger)
    runtime.load_features()

    # Report configuration warnings and localization gaps before going live.
    for issue in issues:
        logger.warning("config: %s — %s", issue.field, issue.message)
    locale_problems = runtime.locales.validate()
    if locale_problems:
        logger.warning("localization problems: %s", "; ".join(locale_problems[:3]))

    telegram = TelegramService(config, logger)
    try:
        info = await telegram.authorize(interactive=True)
    except AuthError as exc:
        logger.error("authentication failed: %s", exc)
        if exc.hint:
            logger.error("hint: %s", exc.hint)
        return EXIT_CONFIG
    runtime.attach_client(telegram, telegram.client, info)
    runtime.permissions.set_self_id(info.user_id)
    runtime.health.mark_connected()

    from .handlers import SystemEventBridge

    router = runtime.handlers
    router.register(telegram.client)
    bridge = SystemEventBridge(runtime)

    if args.no_plugins:
        runtime.config = config.with_overrides(plugins_enabled=False)
    from .plugin_loader import PluginLoader

    runtime.plugin_loader = PluginLoader(
        plugin_dir=config.plugin_dir,
        runtime=runtime,
        logger=get_logger("plugins"),
        enabled=config.plugins_enabled and not args.no_plugins,
    )

    await runtime.start_background()

    # If the cache did not bring anything back, the committed snapshot is the
    # fallback. It is checked before the bot starts so handlers never see a
    # half-restored state.
    try:
        restored = await _restore_from_snapshot(runtime, logger)
        if restored:
            logger.info("restored from the committed snapshot: %s", restored)
    except Exception as exc:  # noqa: BLE001 - a snapshot must never block boot
        logger.warning("snapshot restore skipped: %s", exc)

    # Say out loud what the database holds. On a hosted runner this is the
    # difference between "it probably restored" and "it restored 13 features".
    try:
        counts = await runtime.db.row_counts()
        total = sum(count for name, count in counts.items() if not name.startswith("sqlite_"))
        logger.info(
            "database: %s (%d tables, %d rows, schema %s)",
            config.database_path,
            len(counts),
            total,
            await runtime.db.schema_version(),
        )
        if total == 0:
            logger.warning("database is empty — nothing carried over from a previous run")
    except Exception as exc:  # noqa: BLE001 - a report must never stop the bot
        logger.warning("could not summarise the database: %s", exc)

    # Startup notification (localized, includes only non-secret info).
    language = config.default_language
    banner = "\n".join(
        [
            runtime.t("startup.title", language, version=__version__),
            runtime.t("startup.prefix", language, prefix=config.command_prefix),
            runtime.t("startup.language", language, language=runtime.locales.language_name(language, language)),
            runtime.t("startup.dry_run", language, state=runtime.t("common.on" if config.dry_run else "common.off", language)),
            runtime.t("startup.help", language, prefix=config.command_prefix),
        ]
    )
    await runtime.notify_owner(banner)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(*_: Any) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, _request_stop)

    snapshot_task = asyncio.create_task(_snapshot_loop(runtime, logger), name="havoc-snapshot")
    run_task = asyncio.create_task(telegram.run_until_disconnected(), name="havoc-telegram")
    bridge_task = asyncio.create_task(_watch_connection(runtime, telegram, bridge), name="havoc-connection-watch")
    try:
        await guarded_shutdown(runtime, stop_event, run_task)
    except asyncio.CancelledError:  # pragma: no cover
        pass
    finally:
        bridge_task.cancel()
        snapshot_task.cancel()
        if not run_task.done():
            run_task.cancel()
        # One last snapshot on the way out, while the database still exists.
        try:
            await _snapshot_once(runtime, logger, final=True)
        except Exception as exc:  # noqa: BLE001 - shutdown must not fail on this
            logger.warning("final snapshot failed: %s", exc)
        await runtime.shutdown()
    return EXIT_OK


async def _restore_from_snapshot(runtime: Any, logger: Any) -> str:
    """Import the committed snapshot when the local database has nothing."""
    settings = snapshot.SnapshotSettings.from_env()
    if not settings.enabled:
        return ""
    stored = await runtime.store.export_tables()
    remote = await snapshot.fetch(settings)
    if remote is None:
        return ""
    payload, _sha = remote
    if not snapshot.adds_data(stored, payload["tables"]):
        return ""
    counts = await runtime.store.import_tables(payload["tables"])
    total = sum(counts.values())
    if not total:
        return ""
    runtime.metrics.count("snapshot_restores")
    report = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()) if count)
    logger.warning("database was empty; recovered from the committed snapshot: %s", report)
    # Carried in the next snapshot as well as the report file below, because a
    # separate write is a separate chance to fail.
    state = getattr(runtime, "snapshot_state", None)
    if state is None:
        state = {"digest": ""}
        runtime.snapshot_state = state
    state["last_restore"] = {
        "restored_at": snapshot.iso(snapshot.utc_now()),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "source_created_at": payload.get("created_at", ""),
        "counts": {name: count for name, count in counts.items() if count},
    }
    # Leave a trace in the repository: a restore that leaves no evidence is
    # indistinguishable from a boot that lost everything.
    try:
        report_settings = replace(settings, path=settings.path.replace("snapshot.json", "restore-report.json"))
        await snapshot.push(
            report_settings,
            {
                "app": "havoc",
                "kind": "restore-report",
                "restored_at": snapshot.iso(snapshot.utc_now()),
                "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
                "source_created_at": payload.get("created_at", ""),
                "counts": {name: count for name, count in counts.items() if count},
            },
        )
    except Exception as exc:  # noqa: BLE001 - the report is a nicety
        logger.warning("could not record the restore report: %s", exc)
    return report


async def _snapshot_once(runtime: Any, logger: Any, *, final: bool = False) -> bool:
    """Export every table and commit it, skipping identical documents."""
    settings = snapshot.SnapshotSettings.from_env()
    if not settings.enabled:
        return False
    tables = snapshot.strip_diagnostics(await runtime.store.export_tables())
    payload = snapshot.build_payload(tables)
    last_restore = (getattr(runtime, "snapshot_state", None) or {}).get("last_restore")
    if last_restore:
        payload["last_restore"] = last_restore
    fingerprint = snapshot.digest(payload)
    state = getattr(runtime, "snapshot_state", None)
    if state is None:
        state = {"digest": ""}
        runtime.snapshot_state = state
    if not final and fingerprint == state["digest"]:
        return False
    await snapshot.push(settings, payload)
    state["digest"] = fingerprint
    runtime.metrics.count("snapshots")
    logger.info("snapshot committed to %s (%d tables)", settings.path, len(tables))
    return True


async def _snapshot_loop(runtime: Any, logger: Any) -> None:
    settings = snapshot.SnapshotSettings.from_env()
    if not settings.enabled:
        logger.info("snapshots disabled (no token or repository in the environment)")
        return
    logger.info("snapshots every %d minute(s) to %s", settings.minutes, settings.path)
    while True:
        await asyncio.sleep(max(30, settings.minutes * 60))
        try:
            await _snapshot_once(runtime, logger)
        except Exception as exc:  # noqa: BLE001 - keep trying next time
            logger.warning("snapshot failed: %s", exc)


async def _watch_connection(runtime: Runtime, telegram: TelegramService, bridge: Any) -> None:
    """Turn connection state changes into events (best effort)."""
    was_connected = telegram.connected
    while True:
        await asyncio.sleep(5)
        now_connected = bool(getattr(telegram, "connected", False))
        if was_connected and not now_connected:
            await bridge.disconnected()
        elif not was_connected and now_connected:
            await bridge.reconnected()
        was_connected = now_connected


def login_state_path(args: argparse.Namespace, config: Config) -> Path:
    if getattr(args, "state", None):
        return Path(args.state).expanduser()
    return Path(config.data_dir) / "login-state.json"


def save_login_challenge(path: Path, challenge: LoginChallenge) -> None:
    """Persist only what phase two needs, readable by its owner alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(challenge.to_dict(), ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - exotic filesystems
        pass


def load_login_challenge(path: Path) -> LoginChallenge | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    challenge = LoginChallenge.from_dict(data)
    return challenge if challenge.phone and challenge.phone_code_hash else None


def write_exported_session(path: Path, value: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions before the secret touches the disk.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(value.strip() + "\n")


async def command_login(args: argparse.Namespace) -> int:
    config, issues = load_config_or_report(require_credentials=True)
    if has_blocking(issues):
        return EXIT_CONFIG
    logger = make_logger(config)
    telegram = TelegramService(config, logger)
    state_path = login_state_path(args, config)
    code = getattr(args, "code", None)
    password = getattr(args, "password", None)
    phone = getattr(args, "phone", None)
    try:
        if getattr(args, "request", False):
            challenge = await telegram.request_code(phone)
            save_login_challenge(state_path, challenge)
            print(f"Login code requested for {mask_phone(challenge.phone)}.")
            print("  Telegram is sending the code now; it expires in a couple of minutes.")
            print(f"  Pending challenge saved to: {state_path}")
            print("Next: run `havoc login --code <code>` (add --password ... if 2FA is on).")
            return EXIT_OK

        if code:
            challenge = load_login_challenge(state_path)
            if challenge is None:
                print(
                    "No pending login challenge. Run `havoc login --request` first.",
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            info = await telegram.submit_code(challenge, code, password=password)
            try:
                state_path.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
        else:
            info = await telegram.authorize(interactive=True)

        print("\nLogged in successfully / ورود با موفقیت انجام شد")
        print(f"  user id : {info.user_id}")
        print(f"  username: @{info.username}" if info.username else "  username: (none)")
        print(f"  session : {'SESSION_STRING' if config.session_string else config.session_file}")

        export_path = getattr(args, "export_session", None)
        if export_path:
            value = telegram.export_session_string()
            write_exported_session(Path(export_path), value)
            print(f"\nSESSION_STRING written to {export_path} (mode 600, {len(value)} characters).")
            print("  Store it as a secret; never commit or paste it into a chat.")

        warning = session_permissions_warning(config.session_file)
        if warning:
            print(f"\nWARNING: {warning}")
            print("هشدار: دسترسی فایل نشست را محدود کنید (chmod 600).")
        print("\nStarting HAVOC is now as simple as: havoc run")
        return EXIT_OK
    except AuthError as exc:
        print(f"\nLogin failed: {exc}", file=sys.stderr)
        print(f"ورود ناموفق بود: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"Hint: {exc.hint}", file=sys.stderr)
        return EXIT_CONFIG
    finally:
        await telegram.disconnect()


async def command_check(args: argparse.Namespace) -> int:
    config, issues = load_config(dotenv_path=project_env_file())
    print(f"{CODENAME} {__version__} — configuration check")
    print("=" * 48)
    if issues:
        print("Configuration issues:")
        print(format_issues(issues))
        print("مشکلات تنظیمات در بالا آمده است.")
    else:
        print("Configuration: OK")

    locale_problems: list[str] = []
    try:
        locales = LocaleManager().load()
        locale_problems = locales.validate()
        print(f"Locales: {', '.join(locales.languages)} ({len(locales.keys('en'))} English keys)")
        if locale_problems:
            print("Localization problems: " + "; ".join(locale_problems[:5]))
    except Exception as exc:  # noqa: BLE001
        print(f"Locales: FAILED — {exc}")
        locale_problems = [str(exc)]

    db = Database(config.database_path)
    try:
        await db.connect()
        applied = await apply_migrations(db)
        version = await current_version(db)
        print(f"Database: {config.database_path} (schema v{version}, expected v{LATEST_VERSION})")
        if applied:
            print(f"Applied migrations: {', '.join(applied)}")
        integrity = await db.integrity_check()
        print(f"Integrity: {integrity}")
    except DatabaseError as exc:
        print(f"Database: FAILED — {exc}")
        return EXIT_RUNTIME

    runtime = None
    try:
        runtime = await Runtime.create(config, get_logger("check"))
        runtime.load_features()
        results = await runtime.self_check()
        print("\nSelf-check:")
        for result in results:
            print("  " + result.render())
        ok, failures, warnings = summarize_checks(results)
        print(f"\nResult: {'PASS' if ok else 'FAIL'} ({failures} failures, {warnings} warnings)")
        if not ok:
            print("نتیجه: برخی بررسی‌ها ناموفق بودند؛ جزئیات بالا آمده است.")
    except Exception as exc:  # noqa: BLE001
        print(f"Self-check crashed: {exc}")
        return EXIT_RUNTIME
    finally:
        if runtime is not None:
            await runtime.db.close()
        else:  # pragma: no cover - runtime creation failed before assignment
            await db.close()

    blocking = [issue for issue in issues if issue.severity == "error"]
    if blocking or locale_problems:
        return EXIT_CONFIG
    return EXIT_OK


async def command_migrate(args: argparse.Namespace) -> int:
    config, issues = load_config(dotenv_path=project_env_file())
    if has_blocking(issues) and not config.session_string:
        print(format_issues([i for i in issues if i.severity == "error"]), file=sys.stderr)
    db = Database(config.database_path)
    try:
        await db.connect()
        before = await current_version(db)
        applied = await apply_migrations(db)
        after = await current_version(db)
    except DatabaseError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        await db.close()
    print(f"Schema: {before} → {after}")
    print("Applied: " + (", ".join(applied) if applied else "nothing to do"))
    return EXIT_OK


def _run_id() -> str:
    return os.environ.get("GITHUB_RUN_ID", "local")


def _restored_from() -> dict[str, str]:
    """What the cache step reported, passed down as environment by the runner."""
    keys = {
        "cache_hit": "HAVOC_CACHE_HIT",
        "cache_key": "HAVOC_CACHE_KEY",
        "run_id": "GITHUB_RUN_ID",
    }
    return {name: os.environ.get(env, "") for name, env in keys.items() if os.environ.get(env)}


async def _data_report(args: argparse.Namespace) -> int:
    config, _issues = load_config(dotenv_path=project_env_file())
    path = Path(config.database_path)
    if not path.exists():
        print(f"No database at {path} — this would be a first boot.")
        out = getattr(args, "json_out", None)
        if out:
            payload = {
                "path": str(path),
                "exists": False,
                "boots": 0,
                "restored_from": _restored_from(),
                "populated": {},
                "tables": {},
            }
            Path(out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"wrote {out}")
        return EXIT_OK

    db = Database(path)
    await db.connect()
    try:
        if getattr(args, "boot_probe", False):
            # A deliberately observable write: if the number grows across
            # restarts, the database really survived them.
            from .utils.time import iso, utc_now

            row = await db.fetch_one("SELECT value FROM settings WHERE key = 'diagnostics.boots'")
            try:
                boots = int(json.loads(row["value"])) if row else 0
            except (TypeError, ValueError):  # a hand-edited row must not crash a boot
                boots = 0
            boots += 1
            await db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES('diagnostics.boots', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (json.dumps(boots), iso(utc_now())),
            )
            # A plain-text trail next to the database: it proves whether the
            # directory came back even when the database itself is suspect.
            log = path.parent / "boots.log"
            stamps = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            stamps.append(
                f"{iso(utc_now())} run={_run_id()} boots={boots} db={path.stat().st_size}B"
            )
            try:
                log.write_text("\n".join(stamps[-50:]) + "\n", encoding="utf-8")
            except OSError:  # pragma: no cover - read-only data dir
                pass
            print(f"boot #{boots}")
            if boots == 1:
                print("  first boot on this database: nothing to compare against yet")
            else:
                print(f"  the database survived {boots - 1} restart(s)")

        counts = await db.row_counts()
        tables = list(counts)
        populated = {name: count for name, count in counts.items() if count > 0}
        print(f"database: {path} ({path.stat().st_size / 1024:.1f} KB)")
        print(f"schema version: {await db.schema_version()}")
        print(f"tables: {len(tables)}, populated: {len(populated)}")
        for name, count in sorted(populated.items()):
            print(f"  {name}: {count}")
        print(f"integrity: {await db.integrity_check()}")
        report = {
            "path": str(path),
            "exists": True,
            "schema_version": await db.schema_version(),
            "integrity": await db.integrity_check(),
            "populated": populated,
            "tables": counts,
        }
        out = getattr(args, "json_out", None)
        if out:
            payload = dict(report)
            payload["restored_from"] = _restored_from()
            boot_log = path.parent / "boots.log"
            if boot_log.exists():
                lines = boot_log.read_text(encoding="utf-8").splitlines()
                payload["boot_log_lines"] = len(lines)
                payload["boot_log_tail"] = lines[-3:]
            row = await db.fetch_one("SELECT value FROM settings WHERE key = 'diagnostics.boots'")
            if row:
                try:
                    payload["boots"] = int(json.loads(row["value"]))
                except (TypeError, ValueError):
                    pass
            Path(out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"wrote {out}")
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
    finally:
        await db.close()
    return EXIT_OK


def command_data(args: argparse.Namespace) -> int:
    return asyncio.run(_data_report(args))


def command_version(_args: argparse.Namespace) -> int:
    print(f"{CODENAME} {__version__}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Telethon installed: {'yes' if telethon_available() else 'no'}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="havoc",
        description=f"{CODENAME} — bilingual (English/Farsi) Telegram userbot",
        epilog="Docs: README.md | Support: check `.selfcheck` inside Telegram",
    )
    parser.add_argument("--version", action="version", version=f"{CODENAME} {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="start the userbot")
    run.add_argument("--no-plugins", action="store_true", help="start without loading local plugins")
    run.set_defaults(func=command_run)

    login = subparsers.add_parser("login", help="authenticate and store a session")
    login.add_argument(
        "--request",
        action="store_true",
        help="only ask Telegram to send the code, then exit (scripted login, step 1)",
    )
    login.add_argument("--code", help="the login code from Telegram (scripted login, step 2)")
    login.add_argument(
        "--password",
        help="two-factor password, if the account has 2FA enabled (never prompted in scripted mode)",
    )
    login.add_argument("--phone", help="override PHONE for this login")
    login.add_argument(
        "--state",
        help="where to keep the pending login challenge (default: data/login-state.json)",
    )
    login.add_argument(
        "--export-session",
        metavar="PATH",
        help="write a SESSION_STRING to PATH after a successful login (chmod 600)",
    )
    login.set_defaults(func=command_login)

    check = subparsers.add_parser("check", help="validate configuration and run diagnostics")
    check.set_defaults(func=command_check)

    migrate = subparsers.add_parser("migrate", help="apply database migrations")
    migrate.set_defaults(func=command_migrate)

    data = subparsers.add_parser("data", help="report what the local database holds")
    data.add_argument("--json", action="store_true", help="also print machine-readable counts")
    data.add_argument("--json-out", metavar="PATH", help="write the report to a file")
    data.add_argument(
        "--boot-probe",
        action="store_true",
        help="increment a boot counter first (used to prove persistence across restarts)",
    )
    data.set_defaults(func=command_data)

    version = subparsers.add_parser("version", help="show version information")
    version.set_defaults(func=command_version)
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        args.func = command_run
        args.no_plugins = False
    func = args.func
    if asyncio.iscoroutinefunction(func):
        try:
            return asyncio.run(func(args))
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001 - top-level guard
            print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
            print("خطای غیرمنتظره؛ فایل‌های logs/ را بررسی کنید.", file=sys.stderr)
            return EXIT_RUNTIME
    try:
        return func(args)
    except KeyboardInterrupt:
        return 130


def main() -> None:  # pragma: no cover - console entry point
    raise SystemExit(cli())


if __name__ == "__main__":  # pragma: no cover
    main()
