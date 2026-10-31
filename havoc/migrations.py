"""Schema migration system.

Format
------
Each migration is a numbered, idempotent-by-construction step applied inside a
transaction. ``schema_migrations`` records what ran, so a restart never
re-applies a migration. New columns/tables must be added as a *new* migration;
existing migrations are never edited after release (that is how data loss
happens).

Feature area: 85 (database migration system).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .database import Database, DatabaseError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATION_1_FOUNDATION = Migration(
    version=1,
    name="foundation",
    statements=(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id    INTEGER NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS language_prefs (
            scope      TEXT NOT NULL,             -- global | chat | feature
            scope_id   TEXT NOT NULL DEFAULT '',
            language   TEXT NOT NULL,             -- en | fa | auto | mixed
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (scope, scope_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS features (
            name       TEXT PRIMARY KEY,
            enabled    INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profile_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            field      TEXT NOT NULL UNIQUE,
            value      TEXT,
            captured_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profile_presets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            first_name TEXT,
            last_name  TEXT,
            bio        TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS profile_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            field      TEXT NOT NULL,
            old_value  TEXT,
            new_value  TEXT,
            reason     TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS counters (
            name       TEXT PRIMARY KEY,
            value      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS templates (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            body       TEXT NOT NULL,
            language   TEXT NOT NULL DEFAULT 'auto',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS autoreplies (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern    TEXT NOT NULL,
            match_type TEXT NOT NULL DEFAULT 'contains',
            response   TEXT NOT NULL,
            language   TEXT NOT NULL DEFAULT 'auto',
            scope_chat_id INTEGER,
            cooldown   INTEGER NOT NULL DEFAULT 300,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL DEFAULT 'message',
            chat_id     INTEGER NOT NULL,
            text        TEXT NOT NULL,
            run_at      TEXT NOT NULL,
            recurrence  TEXT,
            timezone    TEXT NOT NULL DEFAULT 'UTC',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_by  INTEGER,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            last_run_at TEXT,
            last_error  TEXT,
            meta        TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            text        TEXT NOT NULL,
            remind_at   TEXT NOT NULL,
            recurrence  TEXT,
            timezone    TEXT NOT NULL DEFAULT 'UTC',
            status      TEXT NOT NULL DEFAULT 'pending',
            created_by  INTEGER,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            last_fired_at TEXT,
            meta        TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS watches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword         TEXT NOT NULL,
            scope_chat_id   INTEGER,
            notify_chat_id  INTEGER,
            language        TEXT NOT NULL DEFAULT 'auto',
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS triggers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL UNIQUE,
            event_type   TEXT NOT NULL DEFAULT 'message',
            pattern      TEXT NOT NULL,
            match_type   TEXT NOT NULL DEFAULT 'contains',
            action_type  TEXT NOT NULL DEFAULT 'notify',
            payload      TEXT,
            scope_chat_id INTEGER,
            language     TEXT NOT NULL DEFAULT 'auto',
            enabled      INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reaction_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern     TEXT NOT NULL,
            match_type  TEXT NOT NULL DEFAULT 'contains',
            emoji       TEXT NOT NULL,
            scope_chat_id INTEGER,
            cooldown    INTEGER NOT NULL DEFAULT 60,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bookmarks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            message_id INTEGER,
            kind       TEXT NOT NULL DEFAULT 'message',
            title      TEXT,
            content    TEXT,
            sender_id  INTEGER,
            labels     TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            title      TEXT,
            body       TEXT NOT NULL,
            labels     TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS aliases (
            alias      TEXT PRIMARY KEY,
            target     TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            payload     TEXT,
            run_at      TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            attempts    INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            last_error  TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS usage_stats (
            feature    TEXT NOT NULL,
            counter    TEXT NOT NULL,
            value      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (feature, counter)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS diagnostics (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plugins (
            name       TEXT PRIMARY KEY,
            version    TEXT,
            enabled    INTEGER NOT NULL DEFAULT 1,
            error      TEXT,
            loaded_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS chat_labels (
            chat_id    INTEGER NOT NULL,
            label      TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chat_id, label)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS dedupe (
            key     TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS interactive_sessions (
            user_id    INTEGER PRIMARY KEY,
            flow       TEXT NOT NULL,
            step       TEXT NOT NULL,
            data       TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_schedules_run_at ON schedules(run_at)",
        "CREATE INDEX IF NOT EXISTS idx_reminders_remind_at ON reminders(remind_at)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_run_at ON jobs(status, run_at)",
        "CREATE INDEX IF NOT EXISTS idx_bookmarks_chat ON bookmarks(chat_id)",
        "CREATE INDEX IF NOT EXISTS idx_notes_chat ON notes(chat_id)",
        "CREATE INDEX IF NOT EXISTS idx_dedupe_seen ON dedupe(seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_profile_history_created ON profile_history(created_at)",
    ),
)

MIGRATION_2_TIMESTAMPS = Migration(
    version=2,
    name="scheduler_durability",
    statements=(
        # A schedule can only fire once per planned slot; the column records the
        # slot so reconnects/restarts cannot double-send (feature area 51/90).
        "ALTER TABLE schedules ADD COLUMN last_slot TEXT",
        "ALTER TABLE reminders ADD COLUMN last_slot TEXT",
        "CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled, run_at)",
        "CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status, remind_at)",
    ),
)

MIGRATION_3_YURA_MEMORY = Migration(
    version=3,
    name="yura_memory",
    statements=(
        # What Yura remembers about the owner. ``kind`` separates durable facts
        # ("works night shifts") from preferences and one-off notes, so recall
        # can weigh them differently.
        """
        CREATE TABLE IF NOT EXISTS yura_memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            kind       TEXT NOT NULL DEFAULT 'fact',
            content    TEXT NOT NULL,
            weight     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """,
        # Rolling conversation window: the last N turns of a real chat, so a
        # reply has context instead of amnesia.
        """
        CREATE TABLE IF NOT EXISTS yura_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_yura_memory_chat ON yura_memory(chat_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_yura_history_chat ON yura_history(chat_id, id DESC)",
    ),
)

MIGRATION_4_PEOPLE = Migration(
    version=4,
    name="yura_people",
    statements=(
        # One row per person Yura has actually talked to — not everyone she has
        # ever seen. The row is created when she answers them, and it holds what
        # she was told about them, how she is supposed to treat them, and how
        # they are related to the owner ("his girlfriend").
        #
        # ``facts`` is a JSON array: a person is a handful of sentences, not a
        # relational model. ``tone`` is the standing instruction — ``rude`` is the
        # default for everyone who is not the owner, and the owner can change it.
        """
        CREATE TABLE IF NOT EXISTS yura_people (
            user_id    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '',
            relation   TEXT NOT NULL DEFAULT '',
            tone       TEXT NOT NULL DEFAULT 'rude',
            facts      TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_yura_people_updated ON yura_people(updated_at DESC)",
    ),
)

MIGRATION_5_PEOPLE_DM = Migration(
    version=5,
    name="yura_people_dm",
    statements=(
        # Private replies are opt-in per person. She answers a private chat only
        # when the owner has introduced that person to her, so a stranger cannot
        # start a conversation with his account and get an answer.
        "ALTER TABLE yura_people ADD COLUMN dm INTEGER NOT NULL DEFAULT 0",
    ),
)

MIGRATION_6_LESSONS = Migration(
    version=6,
    name="yura_lessons",
    statements=(
        # How she is corrected and how that sticks. There is no filter in front of
        # her mouth: the owner tells her a thing is not acceptable, the sentence
        # is kept here, and every later prompt carries it as something she agreed
        # to. ``times_broken`` is not a punishment — it is how she learns that a
        # rule is hard for her, and the next turn mentions it.
        """
        CREATE TABLE IF NOT EXISTS yura_lessons (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            scope          TEXT NOT NULL DEFAULT 'global',
            subject_id     INTEGER NOT NULL DEFAULT 0,
            rule           TEXT NOT NULL,
            tokens         TEXT NOT NULL DEFAULT '[]',
            source         TEXT NOT NULL DEFAULT '',
            times_broken   INTEGER NOT NULL DEFAULT 0,
            last_broken_at TEXT,
            created_at     TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_yura_lessons_scope ON yura_lessons(scope, subject_id)",
    ),
)

MIGRATION_7_ACTIONS = Migration(
    version=7,
    name="yura_actions",
    statements=(
        # Every hand she uses leaves a mark. One row per attempt, written *before*
        # the tool runs so a crash mid-action is still visible, and closed after it
        # with what came back. ``args_hash`` is what the budget and the loop-breaker
        # count; ``args_preview`` is for the owner's eyes, redacted by the caller.
        """
        CREATE TABLE IF NOT EXISTS yura_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL DEFAULT 0,
            actor_id     INTEGER NOT NULL DEFAULT 0,
            tool         TEXT NOT NULL,
            scope        TEXT NOT NULL DEFAULT 'read',
            risk         TEXT NOT NULL DEFAULT 'low',
            args_hash    TEXT NOT NULL DEFAULT '',
            args_preview TEXT NOT NULL DEFAULT '',
            decision     TEXT NOT NULL DEFAULT 'allowed',
            status       TEXT NOT NULL DEFAULT 'running',
            result       TEXT NOT NULL DEFAULT '',
            error        TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            finished_at  TEXT
        )
        """,
        # The hour budget reads this; the card reads the newest rows.
        "CREATE INDEX IF NOT EXISTS idx_yura_actions_created ON yura_actions(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_yura_actions_chat ON yura_actions(chat_id, id)",
    ),
)

MIGRATION_8_AUTOREPLY_SCOPE = Migration(
    version=8,
    name="autoreply_scope",
    statements=(
        # "Reply to anyone who DMs me" is the request; before this column the only
        # scopes were one chat or every chat, so a rule written for his inbox also
        # answered in every group he is in. ``scope`` is who the rule may speak in:
        # any | private | group.
        "ALTER TABLE autoreplies ADD COLUMN scope TEXT NOT NULL DEFAULT 'any'",
        "CREATE INDEX IF NOT EXISTS idx_autoreplies_scope ON autoreplies(scope, enabled)",
    ),
)

MIGRATION_9_TOOLBOX = Migration(
    version=9,
    name="toolbox",
    statements=(
        # Every message this account sends on his behalf, in one place. Without
        # this table "did it actually send?" is an opinion: the send either has a
        # row or it did not happen, and `.outbox` reads the same rows.
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER,
            kind        TEXT NOT NULL DEFAULT 'text',
            text        TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'sent',
            note        TEXT,
            plan_id     INTEGER,
            sent_at     TEXT NOT NULL DEFAULT (datetime('now')),
            deleted_at  TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_outbox_chat ON outbox(chat_id, sent_at)",
        # A plan is a sentence he said, broken into timed steps. The steps are
        # console lines, so a plan can do anything a command can do and nothing
        # needs a second implementation.
        """
        CREATE TABLE IF NOT EXISTS plans (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER,
            label       TEXT NOT NULL DEFAULT '',
            said        TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            stopped_at  TEXT,
            note        TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plan_steps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id     INTEGER NOT NULL,
            idx         INTEGER NOT NULL,
            at          TEXT NOT NULL,
            action      TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            fired_at    TEXT,
            note        TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id, idx)",
        # People he told her not to talk to. A row here beats every other rule:
        # "don't answer him" is not a preference, it is an instruction.
        """
        CREATE TABLE IF NOT EXISTS ignored (
            user_id     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL DEFAULT '',
            reason      TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
    ),
)

MIGRATION_10_REPLY_RULES = Migration(
    version=10,
    name="reply_rules",
    statements=(
        # Who she answers, and how. Before this table the only instruction was
        # "never answer him" — a binary, global, and invisible. A rule now has a
        # *mode* (answer | ignore | react), an emoji for the reacting one, and a
        # scope: chat 0 is everywhere, a real chat id is that room only, so "in
        # this group, don't answer him" does not silence him everywhere.
        """
        CREATE TABLE IF NOT EXISTS reply_rules (
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL DEFAULT 0,
            mode        TEXT NOT NULL DEFAULT 'ignore',
            emoji       TEXT NOT NULL DEFAULT '',
            name        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, chat_id)
        )
        """,
        # Every row he already had stays exactly as strong as it was.
        """
        INSERT OR IGNORE INTO reply_rules(user_id, chat_id, mode, name, created_at)
        SELECT user_id, 0, 'ignore', name, created_at FROM ignored
        """,
        "CREATE INDEX IF NOT EXISTS idx_reply_rules_chat ON reply_rules(chat_id, mode)",
    ),
)

MIGRATION_11_RULE_WINDOWS = Migration(
    version=11,
    name="rule_windows",
    statements=(
        # A rule that lasts: "don't answer him for two hours" is a different
        # instruction from "don't answer him", and it has to end by itself —
        # otherwise the only way back is remembering that it exists.
        "ALTER TABLE reply_rules ADD COLUMN expires_at TEXT",
    ),
)

MIGRATION_12_PLAN_ROOM = Migration(
    version=12,
    name="plan_room",
    statements=(
        # A plan remembers the room it was asked in. Without it, "این گروه ساعت
        # نه بگو سلام" scheduled a step that ran in Saved Messages, because the
        # scheduler has no chat of its own and the plan never said which one.
        "ALTER TABLE plans ADD COLUMN chat_id INTEGER",
    ),
)

MIGRATIONS: tuple[Migration, ...] = (
    MIGRATION_1_FOUNDATION,
    MIGRATION_2_TIMESTAMPS,
    MIGRATION_3_YURA_MEMORY,
    MIGRATION_4_PEOPLE,
    MIGRATION_5_PEOPLE_DM,
    MIGRATION_6_LESSONS,
    MIGRATION_7_ACTIONS,
    MIGRATION_8_AUTOREPLY_SCOPE,
    MIGRATION_9_TOOLBOX,
    MIGRATION_10_REPLY_RULES,
    MIGRATION_11_RULE_WINDOWS,
    MIGRATION_12_PLAN_ROOM,
)

LATEST_VERSION = max(m.version for m in MIGRATIONS)


class MigrationError(DatabaseError):
    pass


async def current_version(db: Database) -> int:
    try:
        return int(await db.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0)
    except DatabaseError:
        return 0


async def apply_migrations(db: Database, migrations: Sequence[Migration] = MIGRATIONS) -> list[str]:
    """Apply all pending migrations in ascending order.

    Returns the names of migrations that were applied. Raises
    :class:`MigrationError` (and leaves the database untouched for the failing
    step) if a migration fails, so partial upgrades are visible instead of
    silently ignored.
    """
    applied: list[str] = []
    ordered = sorted(migrations, key=lambda m: m.version)

    if not db.is_open:
        await db.connect()

    try:
        # Bootstrap the bookkeeping table so `current_version` works.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
    except DatabaseError as exc:
        raise MigrationError(f"cannot prepare migration table: {exc}") from exc

    for migration in ordered:
        version = await current_version(db)
        if migration.version <= version:
            continue
        try:
            await db.execute_script(
                [
                    *migration.statements,
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    f"VALUES({migration.version}, '{migration.name}', datetime('now'))",
                ]
            )
            applied.append(migration.name)
        except Exception as exc:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) failed: {exc}"
            ) from exc
    return applied
