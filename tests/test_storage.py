"""Repository layer behaviour."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from havoc.database import Database
from havoc.migrations import apply_migrations
from havoc.services.storage import DEFAULT_FEATURES, Store
from havoc.utils.time import iso, utc_now


@pytest.fixture
async def store(tmp_path: Path) -> Store:
    db = Database(tmp_path / "store.db")
    await db.connect()
    await apply_migrations(db)
    store = Store(db)
    await store.ensure_features()
    yield store
    await db.close()


async def test_settings_roundtrip(store: Store) -> None:
    await store.set_setting("a", "text")
    await store.set_setting("b", 42)
    await store.set_setting("c", {"x": [1, 2]})
    await store.set_setting("d", True)
    assert await store.get_setting("a") == "text"
    assert await store.get_setting("b") == 42
    assert await store.get_setting("c") == {"x": [1, 2]}
    assert await store.get_bool("d") is True
    assert await store.get_setting("missing", "fallback") == "fallback"
    await store.delete_setting("a")
    assert await store.get_setting("a") is None


async def test_chat_settings_and_language(store: Store) -> None:
    await store.set_chat_setting(5, "automation.denied", True)
    assert await store.get_chat_setting(5, "automation.denied") is True
    await store.set_language("chat", "5", "fa")
    assert await store.get_language("chat", "5") == "fa"
    assert await store.get_language("chat", "6") is None
    prefs = await store.language_prefs()
    assert len(prefs) == 1


async def test_feature_defaults_are_off(store: Store) -> None:
    features = await store.features()
    for name, enabled in DEFAULT_FEATURES.items():
        assert features[name] is enabled is False
    await store.set_feature("autoreply", True)
    assert await store.feature_enabled("autoreply") is True


async def test_emergency_stop(store: Store) -> None:
    assert await store.emergency_stop() is False
    await store.set_emergency_stop(True)
    assert await store.emergency_stop() is True
    await store.set_emergency_stop(False)
    assert await store.emergency_stop() is False


async def test_reminders_lifecycle(store: Store) -> None:
    when = utc_now() + timedelta(minutes=5)
    reminder_id = await store.add_reminder(10, "drink water", when, timezone_name="UTC")
    due = await store.due_reminders(utc_now() + timedelta(minutes=10))
    assert [row["id"] for row in due] == [reminder_id]
    assert await store.complete_reminder(reminder_id, slot=iso(when)) is True
    # second attempt for the same slot must fail (duplicate prevention)
    assert await store.complete_reminder(reminder_id, next_at=when, slot=iso(when)) is False
    assert await store.list_reminders(status="pending") == []


async def test_reminder_recurrence_and_cancel(store: Store) -> None:
    when = utc_now() + timedelta(minutes=1)
    first = await store.add_reminder(1, "daily", when, recurrence="daily")
    assert await store.complete_reminder(first, next_at=when + timedelta(days=1), slot=iso(when)) is True
    rows = await store.list_reminders(status="pending")
    assert rows and rows[0]["recurrence"] == "daily"
    assert await store.cancel_reminder(first) is True
    assert await store.cancel_reminder(first) is False


async def test_schedules_and_slot_guard(store: Store) -> None:
    when = utc_now() + timedelta(minutes=2)
    schedule_id = await store.add_schedule(99, "hello", when)
    assert [row["id"] for row in await store.due_schedules(utc_now() + timedelta(minutes=3))] == [schedule_id]
    await store.mark_schedule_run(schedule_id, slot=iso(when), next_run=None)
    row = await store.get_schedule(schedule_id)
    assert row["enabled"] == 0 and row["last_slot"] == iso(when)
    await store.reschedule(schedule_id, when + timedelta(hours=1))
    assert await store.cancel_schedule(schedule_id) is False  # already disabled


async def test_autoreplies_and_templates(store: Store) -> None:
    rule = await store.add_autoreply("hi", "hello!", match_type="exact")
    assert len(await store.list_autoreplies()) == 1
    assert await store.set_autoreply_enabled(rule, False) == 1
    assert await store.list_autoreplies(enabled_only=True) == []
    await store.add_template("greet", "Hi {name}")
    assert (await store.get_template("greet"))["body"] == "Hi {name}"
    await store.update_template("greet", "Hey {name}")
    assert (await store.get_template("greet"))["body"] == "Hey {name}"
    assert await store.delete_template("greet") == 1


async def test_watch_trigger_reaction(store: Store) -> None:
    watch = await store.add_watch("urgent")
    assert len(await store.list_watches()) == 1
    assert await store.delete_watch(watch) == 1
    await store.add_trigger("t1", "hello", action_type="notify")
    assert (await store.list_triggers())[0]["name"] == "t1"
    await store.add_reaction_rule("nice", "👍")
    assert (await store.list_reaction_rules())[0]["emoji"] == "👍"


async def test_aliases_and_loop_detection(store: Store) -> None:
    await store.add_alias("hi", "help")
    assert await store.resolve_alias("hi") == "help"
    await store.add_alias("help", "hi")
    assert await store.resolve_alias("hi") is None  # loop rejected
    assert await store.delete_alias("hi") == 1


async def test_bookmarks_notes_search(store: Store) -> None:
    bookmark_id = await store.add_bookmark(5, message_id=7, title="Recipe", content="pasta with garlic")
    assert (await store.get_bookmark(bookmark_id))["title"] == "Recipe"
    assert [row["id"] for row in await store.search_bookmarks("garlic")] == [bookmark_id]
    assert await store.search_bookmarks("missing") == []
    note_id = await store.add_note(5, "call dentist", title="health")
    assert [row["id"] for row in await store.search_notes("dentist")] == [note_id]
    assert await store.delete_note(note_id) == 1


async def test_counters_and_usage(store: Store) -> None:
    assert await store.increment_counter("messages") == 1
    assert await store.increment_counter("messages", 5) == 6
    await store.reset_counter("messages")
    assert await store.get_counter("messages") == 0
    await store.bump_usage("autoreply", "sent")
    await store.bump_usage("autoreply", "sent")
    rows = await store.usage_stats()
    assert rows[0]["value"] == 2


async def test_profile_snapshots_and_history(store: Store) -> None:
    await store.save_profile_snapshot("first_name", "Original")
    await store.save_profile_snapshot("first_name", "New")  # upsert
    assert await store.get_profile_snapshot("first_name") == "New"
    await store.add_profile_history("first_name", "Original", "New", "test")
    history = await store.profile_history()
    assert history[0]["reason"] == "test"
    await store.add_profile_preset("work", first_name="Ali", bio="working")
    assert (await store.get_profile_preset("work"))["first_name"] == "Ali"
    assert [row["name"] for row in await store.list_profile_presets()] == ["work"]


async def test_dedupe_window(store: Store) -> None:
    assert await store.seen_recently("k", 60) is False
    await store.mark_seen("k")
    assert await store.seen_recently("k", 60) is True
    assert await store.seen_recently("k", 0) is False


async def test_export_import_roundtrip(store: Store, tmp_path: Path) -> None:
    await store.set_setting("theme", "dark")
    await store.add_template("t", "body")
    payload = await store.export_tables()
    assert set(payload) <= set(Store.BACKUP_TABLES)
    assert "settings" in payload

    other = Store(Database(tmp_path / "other.db"))
    await other.db.connect()
    await apply_migrations(other.db)
    counts = await other.import_tables(payload)
    assert counts["templates"] == 1
    assert await other.get_setting("theme") == "dark"
    await other.db.close()


async def test_import_rejects_unknown_tables_and_columns(store: Store) -> None:
    malicious = {
        "settings": [{"key": "x", "value": '"kept"', "evil": "DROP TABLE settings"}],
        "not_a_table": [{"x": 1}],
    }
    counts = await store.import_tables(malicious)
    assert "not_a_table" not in counts
    # unknown columns are dropped, known ones survive, no SQL is executed
    assert await store.get_setting("x") == "kept"
    columns = {row["name"] for row in await store.db.fetch_all("PRAGMA table_info(settings)")}
    assert "evil" not in columns
    assert "settings" in await store.db.table_names()


async def test_legacy_plain_values_still_read(store: Store) -> None:
    """Rows written before JSON encoding existed must keep working."""
    await store.db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("command_prefix", "."))
    assert await store.get_setting("command_prefix") == "."
    await store.db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("legacy_bool", "1"))
    assert await store.get_bool("legacy_bool") is True


async def test_interactive_sessions_expire(store: Store) -> None:
    await store.set_interactive(42, "confirm", "backup", {"args": ["export"]})
    assert await store.get_interactive(42) is not None
    await store.clear_interactive(42)
    assert await store.get_interactive(42) is None
    # an old session is discarded
    await store.set_interactive(43, "confirm", "x")
    await store.db.execute(
        "UPDATE interactive_sessions SET updated_at = datetime('now', '-2 hours') WHERE user_id = 43"
    )
    assert await store.get_interactive(43) is None


async def test_chat_labels_and_plugins(store: Store) -> None:
    await store.add_chat_label(1, "work")
    await store.add_chat_label(1, "work")
    assert await store.chat_labels(1) == ["work"]
    await store.record_plugin("demo", "1.0", True, None)
    await store.record_plugin("demo", "1.1", False, "boom")
    plugins = await store.list_plugins()
    assert plugins[0]["version"] == "1.1" and plugins[0]["error"] == "boom"
