"""Her own health: diagnose, repair, explain, and say what's new.

A bot that cannot say why it is quiet is indistinguishable from a bot that is
dead — that is the lesson that cost the most in 1.7, when a green CI run held a
process that died nine seconds after login. So the four questions he actually asks
have real answers here, built from the running process and the database:

* **are you okay** → ``.diagnose``: connection, scheduler, model, database, the
  last error, the queue depth. Anything red says what it is.
* **fix yourself** → ``.repair``: the safe repairs only — jobs stuck in
  ``running`` from a killed process, interactive flows abandoned mid-sentence,
  expired dedupe rows, a checkpointed WAL. Each one reports what it did *before*
  and after, never "done".
* **why didn't you answer** → ``.why``: the last skip reason for this chat, the
  counters behind it, and the newest error, because "she ignored me" and "a gate
  said no" look identical from the outside.
* **what's new** → ``.news``: the version and the last runs recorded in
  ``bot-status.json``, which is written by the workflow, not by hope.

Nothing here sends anything anywhere, and nothing here needs the network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..command_registry import Command, CommandContext
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate
from ..utils.time import iso, parse_iso, utc_now
from ..version import VERSION

__all__ = ["register"]

#: A job left in ``running`` for longer than this was killed, not working. The
#: scheduler's own retry is 15s + attempts; ten minutes is far past that.
STUCK_AFTER = 600
#: A flow with no interaction for this long is abandoned. It matches the store's
#: own expiry for interactive sessions.
FLOW_AFTER = 900


def _human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def _uptime(health: Any) -> float:
    """``uptime_seconds`` is a property on Health, and a method in some fakes."""
    value = getattr(health, "uptime_seconds", 0.0)
    return float(value() if callable(value) else value)


def _size(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


async def _job_counts(ctx: CommandContext) -> dict[str, int]:
    rows = await ctx.db.fetch_all(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    )
    return {str(row["status"]): int(row["n"]) for row in (rows or [])}


async def _queue_health(ctx: CommandContext) -> str:
    counts = await _job_counts(ctx)
    if not counts:
        return "no queued work"
    return " · ".join(f"{status} {count}" for status, count in sorted(counts.items()))


# ---------------------------------------------------------------------------
# .diagnose
# ---------------------------------------------------------------------------
async def cmd_diagnose(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    health = runtime.health
    checks: list[str] = []

    connected = bool(getattr(health, "connected", False))
    checks.append(
        ctx.t(
            "doctor.connection",
            state=ctx.t("common.on" if connected else "common.off"),
            reconnects=getattr(health, "reconnects", 0),
        )
    )
    try:
        db = await health.database_health(force=True)
        checks.append(
            ctx.t(
                "doctor.database",
                state=ctx.t("common.ok" if db.get("ok") else "doctor.bad"),
                ms=db.get("latency_ms", 0),
                size=_size(await runtime.db.db_size_bytes()),
            )
        )
    except Exception as exc:  # noqa: BLE001 - a broken db must still answer
        checks.append(ctx.t("doctor.database", state=ctx.t("doctor.bad"), ms="—", size="—"))
        runtime.health.record_error("diagnose", exc)

    scheduler = runtime.scheduler
    stats = getattr(scheduler, "stats", None)
    checks.append(
        ctx.t(
            "doctor.scheduler",
            ticks=getattr(stats, "ticks", 0),
            failures=getattr(stats, "failures", 0),
            last=escape(
                truncate(getattr(stats, "last_error", "") or ctx.t("doctor.none"), 80)
            ),
        )
    )
    checks.append(ctx.t("doctor.queue", queues=escape(await _queue_health(ctx))))

    provider = getattr(runtime, "ai", None)
    checks.append(
        ctx.t(
            "doctor.model",
            state=ctx.t("common.ok" if getattr(provider, "available", False) else "doctor.bad"),
            model=escape(getattr(provider, "active_model", "") or getattr(provider, "model", "—")),
        )
    )
    flood = getattr(runtime.sender, "flood", None)
    waits = getattr(health, "flood_waits", 0)
    checks.append(
        ctx.t(
            "doctor.flood",
            waits=waits,
            remaining=escape(getattr(flood, "remaining_human", lambda: "0s")()),
        )
    )
    errors = list(getattr(health, "errors", []) or [])
    if errors:
        last = errors[-1]
        checks.append(
            ctx.t(
                "doctor.last_error",
                source=escape(str(last.get("source", "?"))),
                message=escape(truncate(str(last.get("message", "")), 120)),
                when=escape(str(last.get("at", ""))),
            )
        )
    else:
        checks.append(ctx.t("doctor.no_errors"))

    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("doctor.header", version=VERSION)),
                ctx.t("doctor.uptime", uptime=_human(_uptime(health))),
                *checks,
            ]
        )
    )


# ---------------------------------------------------------------------------
# .repair
# ---------------------------------------------------------------------------
async def cmd_repair(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    done: list[str] = []
    cutoff = iso(utc_now() - timedelta(seconds=STUCK_AFTER))

    stuck = await ctx.db.execute(
        """
        UPDATE jobs SET status = 'pending', updated_at = ?
        WHERE status = 'running' AND updated_at < ?
        """,
        (iso(utc_now()), cutoff),
    )
    if stuck:
        done.append(ctx.t("repair.jobs", count=stuck))

    flow_cutoff = iso(utc_now() - timedelta(seconds=FLOW_AFTER))
    flows = await ctx.db.execute(
        "DELETE FROM interactive_sessions WHERE updated_at < ?", (flow_cutoff,)
    )
    if flows:
        done.append(ctx.t("repair.flows", count=flows))

    pruned = await ctx.store.prune_dedupe(86400)
    if pruned:
        done.append(ctx.t("repair.dedupe", count=pruned))

    # A wedged cooldown is invisible except as "it is refusing me now". Clearing
    # the in-memory tracker is safe: the cost is one extra command, not a loop.
    tracker = getattr(runtime.limiter, "_cooldowns", None)
    cleared = 0
    if tracker is not None and hasattr(tracker, "_last"):
        cleared = len(tracker._last)  # noqa: SLF001 - the repair is the point
        tracker._last.clear()  # noqa: SLF001
    if cleared:
        done.append(ctx.t("repair.cooldowns", count=cleared))

    try:
        await ctx.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        done.append(ctx.t("repair.wal"))
    except Exception as exc:  # noqa: BLE001 - a busy WAL is not a failure
        runtime.logger.info("repair: checkpoint skipped (%s)", exc)

    health = runtime.health
    checks = await health.self_check(runtime) if hasattr(health, "self_check") else []
    # A warning is not a failure: "the session file is not created yet" is
    # normal on a first login, and colouring it red taught him to ignore red.
    red = [
        check
        for check in checks
        if not getattr(check, "ok", True) and getattr(check, "severity", "error") == "error"
    ]
    warned = [
        check
        for check in checks
        if not getattr(check, "ok", True) and getattr(check, "severity", "error") != "error"
    ]
    lines = [bold(ctx.t("repair.header"))]
    if done:
        lines.extend(f"✓ {escape(line)}" for line in done)
    else:
        lines.append(ctx.t("repair.nothing"))
    if red:
        lines.append(ctx.t("repair.still_red", count=len(red)))
        lines.extend(f"✗ {escape(getattr(check, 'name', '?'))}" for check in red[:6])
    else:
        lines.append(ctx.t("repair.green"))
    if warned:
        lines.append(
            ctx.t(
                "repair.warnings",
                items=escape(", ".join(getattr(check, "name", "?") for check in warned[:5])),
            )
        )
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# .why
# ---------------------------------------------------------------------------
async def cmd_why(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    replies = getattr(runtime, "persona_replies", None)
    skips = dict(getattr(replies, "skips", {}) or {})
    last = getattr(replies, "last_skip", "") or ""
    if not skips and not last:
        await ctx.reply(ctx.t("why.quiet"))
        return
    reason_key = f"why.reason.{last}" if last else "why.reason.unknown"
    reason = ctx.t(reason_key, default=ctx.t("why.reason.unknown"))
    lines = [
        bold(ctx.t("why.header")),
        ctx.t("why.last", reason=escape(reason), code=code(last or "—")),
    ]
    if skips:
        lines.append(ctx.t("why.counters"))
        ordered = sorted(skips.items(), key=lambda pair: pair[1], reverse=True)[:6]
        lines.extend(f"• {escape(name)} — {code(str(count))}" for name, count in ordered)
    errors = list(getattr(runtime.health, "errors", []) or [])
    if errors:
        last_error = errors[-1]
        lines.append(
            ctx.t(
                "why.last_error",
                source=escape(str(last_error.get("source", "?"))),
                message=escape(truncate(str(last_error.get("message", "")), 140)),
            )
        )
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# .news / .version
# ---------------------------------------------------------------------------
def _status_file() -> Path:
    return Path("bot-status.json")


def _read_runs(limit: int = 3) -> list[dict[str, Any]]:
    path = _status_file()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    history = payload.get("history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        return []
    return [row for row in history if isinstance(row, dict)][:limit]


def _ago(written: str) -> str:
    moment = parse_iso(written)
    if moment is None:
        return written or "—"
    return _human((utc_now() - moment).total_seconds()) + " ago"


async def cmd_news(ctx: CommandContext) -> None:
    runs = _read_runs(3)
    lines = [bold(ctx.t("news.header", version=VERSION))]
    if not runs:
        lines.append(ctx.t("news.no_runs"))
    for row in runs:
        commit = str(row.get("commit", ""))[:7]
        verdict = ctx.t("common.ok") if row.get("healthy") else ctx.t("doctor.bad")
        lines.append(
            f"• {code(commit or '—')} · {verdict} · "
            + ctx.t(
                "news.run_line",
                uptime=_human(float(row.get("uptime_seconds") or 0)),
                when=escape(_ago(str(row.get("written_at", "")))),
            )
        )
        reason = str(row.get("reason", "") or "")
        if reason:
            lines.append(f"  {escape(truncate(reason, 110))}")
    lines.append(ctx.t("news.hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


async def cmd_version(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    import platform

    lines = [
        bold(ctx.t("version.header", version=VERSION)),
        ctx.t(
            "version.line",
            python=platform.python_version(),
            uptime=_human(_uptime(runtime.health)),
        ),
        ctx.t("version.schema", version=await runtime.db.schema_version()),
        ctx.t("version.commands", count=len(runtime.registry.all())),
    ]
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "diagnose",
                cmd_diagnose,
                category="core",
                aliases=("doctor", "health"),
                usage="",
                description="Connection, scheduler, model, database, queue, last error.",
                examples=(".diagnose",),
            ),
            Command(
                "repair",
                cmd_repair,
                category="core",
                aliases=("fix", "heal"),
                usage="",
                description="Clear stuck jobs, abandoned flows, expired dedupe and wedged cooldowns.",
                changes_state=True,
            ),
            Command(
                "why",
                cmd_why,
                category="core",
                usage="",
                description="Why she did not answer: the last gate, the counters, the last error.",
            ),
            Command(
                "news",
                cmd_news,
                category="core",
                aliases=("changelog", "whatsnew"),
                usage="",
                description="The version, and what the last runs actually did.",
            ),
            Command(
                "version",
                cmd_version,
                category="core",
                aliases=("ver",),
                usage="",
                description="Version, Python, schema and command count.",
            ),
        ]
    )
