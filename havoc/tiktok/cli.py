"""``havoc tt …`` — the account on your desk, then in a workflow.

Two audiences, one parser:

* **On your machine**, with a visible window: ``login`` is the only command that
  needs you. It opens TikTok's own login page and waits. No password is typed by
  this tool; what is saved afterwards is the cookie jar.
* **In the workflow**, with no window and no human: ``run`` restores that jar and
  behaves like the userbot — one long run, events in, answers out, jar re-saved
  before it exits.

Every write is confirmed unless ``--yes``. ``--dry-run`` says what would happen
and spends nothing. Exit codes are stable so a workflow can branch on them:

    0 ok · 1 refused/limit/challenge · 2 bad usage · 3 TikTok's page changed
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import SessionError, TikTokSessionStore, redact
from .browser import browser_available, install_hint

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_DRIFT = 3

DEFAULT_DIR = ".auth/tiktok"
#: Where a status file is written, so the workflow can publish it and the owner
#: can see from the repo whether the last run worked.
STATUS_PATH = ".reports/tiktok-status.json"


def _dir(args: argparse.Namespace) -> Path:
    return Path(args.dir or os.environ.get("HAVOC_TIKTOK_DIR", DEFAULT_DIR))


def _store(args: argparse.Namespace) -> TikTokSessionStore:
    return TikTokSessionStore(_dir(args), account=args.account)


def _confirm(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} — refusing: stdin is not a terminal (pass --yes).", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def _write_status(payload: dict[str, Any]) -> None:
    try:
        path = Path(STATUS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(redact(payload), indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Commands that touch no page
# ---------------------------------------------------------------------------
def command_status(args: argparse.Namespace) -> int:
    store = _store(args)
    out: dict[str, Any] = {
        "session_dir": str(store.directory),
        "browser_installed": browser_available(),
    }
    try:
        account = store.load()
        out["account"] = account.summary()
        out["age_warning"] = account.age_days() > 150
    except SessionError as exc:
        out["account"] = {"logged_in": False, "reason": str(exc), "hint": exc.hint}
    from .limits import DEFAULT_LIMITS, budget_for

    out["today"] = {
        action: {"used": store.state().get("counts", {}).get("spent", {}).get(action, 0), "limit": budget_for(action)}
        for action in DEFAULT_LIMITS
    }
    if args.json:
        print(json.dumps(redact(out), indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"session dir : {store.directory}")
    print(f"browser     : {'installed' if browser_available() else 'MISSING — ' + install_hint()}")
    account = out["account"]
    if account.get("logged_in"):
        print(f"logged in   : @{account['handle']} · {account['cookies']} cookies · {account['age_days']} days old")
        if out["age_warning"]:
            print("  ! the jar is old — a 401 anywhere means it is time to log in again")
        if account.get("surfaces"):
            print(f"surfaces    : {account['surfaces']}")
    else:
        print(f"logged in   : no ({account.get('reason')})")
        if account.get("hint"):
            print(f"  → {account['hint']}")
    print("today (UTC):")
    for action, counts in out["today"].items():
        print(f"  {action:<9} {counts['used']:>4}/{counts['limit']}")
    return EXIT_OK


def command_logout(args: argparse.Namespace) -> int:
    store = _store(args)
    if not _confirm(f"Delete the saved TikTok session in {store.directory}?", assume_yes=args.yes):
        return EXIT_REFUSED
    removed = False
    for path in (store.path, store.state_path):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
    print("session deleted from this workspace" if removed else "there was no session to delete")
    print("this does not sign the device out on TikTok's side — Settings → Security does that.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Login: the window
# ---------------------------------------------------------------------------
async def command_login(args: argparse.Namespace) -> int:
    if not browser_available():
        print(f"the browser engine is not installed.\n  {install_hint()}", file=sys.stderr)
        return EXIT_REFUSED
    from .browser import TikTokBrowser
    from .limits import Pace

    store = _store(args)
    store.directory.mkdir(parents=True, exist_ok=True)
    print("Opening TikTok's login page in a real Chromium window.")
    print("Sign in yourself — password, QR, whatever you normally use. Nothing is typed by this tool.")
    print(f"The window waits up to {int(args.timeout)}s. Close it early and nothing is saved.\n")
    browser = TikTokBrowser(store, headless=False, pace=Pace())
    try:
        await browser.start()
        result = await browser.login(timeout=args.timeout)
        if not result.ok:
            print(f"login: failed — {result.detail}", file=sys.stderr)
            return EXIT_REFUSED
        account = store.load()
        print(f"\nlogged in as @{account.handle or '(handle unreadable)'}")
        print(f"jar saved : {store.path}  ({len(account.cookies)} cookies)")
        print("\nnext: commit that file so the workflow can use it, or run `havoc tt run` here.")
        _write_status({"event": "login", "account": account.summary()})
        return EXIT_OK
    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
async def command_run(args: argparse.Namespace) -> int:
    if not browser_available():
        print(f"the browser engine is not installed.\n  {install_hint()}", file=sys.stderr)
        return EXIT_REFUSED
    store = _store(args)
    try:
        account = store.load()
    except SessionError as exc:
        print(f"{exc.kind}: {exc}\n  → {exc.hint}", file=sys.stderr)
        _write_status({"event": "run", "ok": False, "reason": exc.kind, "detail": str(exc)})
        return EXIT_REFUSED

    runtime = None
    if not args.no_runtime:
        runtime = await _runtime(args)

    from .bot import BotConfig, TikTokBot

    config = BotConfig(
        owner_id=args.owner or account.user_id,
        owner_handle=args.owner_handle or account.handle,
        run_seconds=args.minutes * 60,
        answer_strangers=not args.owner_only,
        headless=not args.window,
        work_dir=Path(".runtime/tiktok/work"),
    )
    bot = TikTokBot(store, config, runtime=runtime, logger=_logger())
    print(f"run: @{account.handle or '?'} · {args.minutes:.0f} min · {'visible' if args.window else 'headless'}")
    report = await bot.run(minutes=args.minutes)
    report["account"] = {"handle": account.handle}
    _write_status({"event": "run", **report})
    print(json.dumps(redact(report), indent=2, ensure_ascii=False))
    if report.get("challenge"):
        # 1 and not 0: the workflow must not queue a successor into a captcha.
        return EXIT_REFUSED
    return EXIT_OK


async def _runtime(args: argparse.Namespace) -> Any:
    """The Telegram-side runtime, reused for her mind only.

    Deliberately *not* connected to Telegram: this process has no MTProto
    session. It builds the database, the AI router, memory and the persona, and
    leaves the client unattached — which is exactly what ``Runtime.create``
    already supports for tests and for probes.
    """
    try:
        from ..config import load_config
        from ..logging_setup import setup_logging
        from ..runtime import Runtime
    except Exception:  # noqa: BLE001
        return None
    try:
        config, _issues = load_config()
        logger = setup_logging(config)
        return await Runtime.create(config, logger)
    except Exception as exc:  # noqa: BLE001 - her mind is a bonus, not the run
        print(f"note: her full runtime is unavailable ({exc}); answering from the offline bank", file=sys.stderr)
        return None


def _logger() -> Any:
    import logging

    level = os.environ.get("HAVOC_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    return logging.getLogger("havoc.tiktok")


# ---------------------------------------------------------------------------
# One-off actions, for a human at a keyboard
# ---------------------------------------------------------------------------
async def _one(args: argparse.Namespace, action: str) -> int:
    if not browser_available():
        print(install_hint(), file=sys.stderr)
        return EXIT_REFUSED
    from .actions import AccountActions
    from .browser import TikTokBrowser
    from .limits import Budget, Pace

    store = _store(args)
    try:
        store.load()
    except SessionError as exc:
        print(f"{exc.kind}: {exc}\n  → {exc.hint}", file=sys.stderr)
        return EXIT_REFUSED
    budget = Budget(store)
    target = args.target
    if args.dry_run:
        print(f"would {action} {target} (left today: {budget.remaining(action.split()[0])})")
        return EXIT_OK
    if not _confirm(f"{action} {target} from your account?", assume_yes=args.yes):
        return EXIT_REFUSED
    browser = TikTokBrowser(store, headless=not args.window, pace=Pace())
    try:
        await browser.start()
        hands = AccountActions(browser, budget, logger=_logger())
        result = await getattr(hands, action)(target)
        print(result)
        if result.data:
            print(json.dumps(redact(result.data), indent=2, ensure_ascii=False))
        return EXIT_OK if result.ok else (EXIT_DRIFT if result.data.get("drift") else EXIT_REFUSED)
    finally:
        try:
            await browser.save_jar()
        except Exception:  # noqa: BLE001
            pass
        await browser.close()


def command_probe(args: argparse.Namespace) -> int:
    """Answer the only question that matters before building further: what does
    this account's web session actually show? Run locally, with a window."""
    return asyncio.run(_probe(args))


async def _probe(args: argparse.Namespace) -> int:
    if not browser_available():
        print(install_hint(), file=sys.stderr)
        return EXIT_REFUSED
    from .browser import MESSAGES_URL, SELECTORS, TikTokBrowser
    from .limits import Pace

    store = _store(args)
    browser = TikTokBrowser(store, headless=False, pace=Pace())
    out: dict[str, Any] = {}
    try:
        await browser.start()
        await browser.goto(MESSAGES_URL)
        out["url"] = browser.page.url
        found = {}
        for key, selectors in SELECTORS.items():
            for selector in selectors:
                try:
                    count = await browser.page.locator(selector).count()
                except Exception:  # noqa: BLE001
                    count = 0
                if count:
                    found[key] = {"selector": selector, "count": count}
                    break
        out["selectors_found"] = found
        out["frames_seen"] = len(browser._frames)
        out["title"] = await browser.page.title()
    finally:
        await browser.close()
    print(json.dumps(redact(out), indent=2, ensure_ascii=False))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="havoc tt",
        description="Operate your own TikTok account through a real browser session — no API key.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", help=f"session directory (default: {DEFAULT_DIR})")
    common.add_argument("--account", default="default", help="which saved account to use")
    common.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    common.add_argument("--dry-run", action="store_true", help="say what would happen, spend nothing")
    common.add_argument("--window", action="store_true", help="show the browser window")

    sub = parser.add_subparsers(dest="tt_command")

    login = sub.add_parser("login", parents=[common], help="sign in once, in a real browser window")
    login.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for the sign-in")
    login.set_defaults(func=command_login)

    run = sub.add_parser("run", parents=[common], help="watch the inbox and answer as Yura")
    run.add_argument("--minutes", type=float, default=300.0, help="how long this run may live")
    run.add_argument("--owner", default="", help="the owner's TikTok id (default: from the jar)")
    run.add_argument("--owner-handle", default="", help="the owner's @handle")
    run.add_argument("--owner-only", action="store_true", help="do not answer strangers")
    run.add_argument("--no-runtime", action="store_true", help="skip her mind; offline lines only")
    run.set_defaults(func=command_run)

    status = sub.add_parser("status", parents=[common], help="session, surfaces and today's allowance")
    status.add_argument("--json", action="store_true", help="machine-readable")
    status.set_defaults(func=command_status)

    probe = sub.add_parser("probe", parents=[common], help="what this account's web session shows")
    probe.set_defaults(func=command_probe)

    for name, help_text in (
        ("like", "like one video"),
        ("unlike", "take a like back"),
        ("follow", "follow someone"),
        ("unfollow", "unfollow someone"),
        ("block", "block someone"),
    ):
        one = sub.add_parser(name, parents=[common], help=help_text)
        one.add_argument("target")
        one.set_defaults(func=lambda ns, action=name: asyncio.run(_one(ns, action)))

    logout = sub.add_parser("logout", parents=[common], help="delete the local session")
    logout.set_defaults(func=command_logout)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        if inspect.iscoroutinefunction(args.func):
            return asyncio.run(args.func(args))
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted — nothing else was sent.", file=sys.stderr)
        return EXIT_REFUSED
    except SessionError as exc:
        print(f"{exc.kind}: {exc}" + (f"\n  → {exc.hint}" if exc.hint else ""), file=sys.stderr)
        return EXIT_REFUSED


__all__ = ["build_parser", "main"]
