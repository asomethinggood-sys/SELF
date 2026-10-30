"""The bot boots.

This file exists because 1.7 shipped a build that logged in and then died one
second later: `Runtime.start_background` still called `event_engine.start()`,
a method that had been deleted with the queue it belonged to. Every unit test
passed, because no test ever ran the boot path — the failure lived in the seam
between `main`, `Runtime` and the engine.

The boot itself is exercised by `scripts/boot_probe.py` (one implementation, so
the audit and the suite can never disagree about what "boots" means); this file
adds the assertions that need a live runtime object.
"""

from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from havoc.event_engine import EventType
from havoc.runtime import Runtime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import boot_probe  # noqa: E402  (path inserted above)


@pytest.fixture(scope="module")
def booted() -> dict[str, Any]:
    import asyncio

    data = Path(tempfile.mkdtemp()) / "data"
    boot_probe.prepare_environment(data)
    return asyncio.run(boot_probe.boot())


def test_the_bot_starts_and_stops_cleanly(booted: dict[str, Any]) -> None:
    assert booted["ok"], booted["problems"]


def test_the_owner_is_told_that_she_is_awake(booted: dict[str, Any]) -> None:
    """The startup banner is a sentence, never a raw locale key."""
    banner = booted["said"]
    assert booted["owner_told"]
    assert "1.7" in banner
    assert "{language}" not in banner
    assert "startup." not in banner, f"a locale key leaked into the banner: {banner!r}"
    assert "None" not in banner


async def test_background_start_and_shutdown_do_not_raise(runtime: Runtime) -> None:
    """The lifecycle pair, called directly: no method that is not there."""
    await runtime.start_background()
    await runtime.shutdown()


async def test_startup_event_reaches_a_subscriber(runtime: Runtime) -> None:
    """`start_background` announces itself, and the engine delivers it."""
    seen: list[EventType] = []

    async def watcher(event: Any) -> None:
        seen.append(event.type)

    runtime.event_engine.subscribe("boot.watch", watcher, {EventType.STARTUP})
    await runtime.start_background()
    assert EventType.STARTUP in seen
    await runtime.shutdown()


def test_every_method_runtime_calls_on_its_children_exists(runtime: Runtime) -> None:
    """The bug in one assertion: a class may only call what exists.

    This is the static half of the guard. It reads `Runtime`'s own source, finds
    every `self.<child>.<method>(` call, and checks the attribute is really
    there — so deleting `EventEngine.start` can never again leave a call behind.
    """
    source = inspect.getsource(Runtime)
    children = {
        "event_engine": runtime.event_engine,
        "scheduler": runtime.scheduler,
        "health": runtime.health,
        "sender": runtime.sender,
        "store": runtime.store,
        "db": runtime.db,
        "locales": runtime.locales,
        "permissions": runtime.permissions,
        "handlers": runtime.handlers,
        "ai": runtime.ai,
    }
    missing: list[str] = []
    for child, instance in children.items():
        marker = f"self.{child}."
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or marker not in stripped:
                continue
            rest = stripped.split(marker, 1)[1]
            name = rest.split("(", 1)[0].split(".")[0].split(")", 1)[0].strip()
            if not name.isidentifier():
                continue
            if not hasattr(instance, name) and not hasattr(type(instance), name):
                missing.append(f"self.{child}.{name}")
    assert not missing, f"Runtime calls methods that do not exist: {sorted(set(missing))}"
