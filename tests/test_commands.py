"""End-to-end command behaviour through the real dispatcher.

Every test drives the same path the router uses: parse -> dispatch -> Sender,
with a fake Telethon client recording what would have been sent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeMessage, invoke, last_text, sent_text


# ---------------------------------------------------------------------------
# Core commands
# ---------------------------------------------------------------------------
async def test_help_lists_commands(runtime) -> None:
    await invoke(runtime, ".help")
    text = sent_text(runtime)
    assert ".remind" in text and ".fight" in text


async def test_help_for_single_command(runtime) -> None:
    await invoke(runtime, ".help remind")
    text = last_text(runtime)
    assert ".remind" in text
    assert "30m" in text or "usage" in text.lower()


async def test_help_unknown_command(runtime) -> None:
    await invoke(runtime, ".help nope-not-real")
    assert "nope-not-real" in last_text(runtime)


async def test_commands_is_paginated(runtime) -> None:
    await invoke(runtime, ".commands")
    first = last_text(runtime)
    assert "page 1/" in first
    await invoke(runtime, ".commands 99")
    assert "page" in last_text(runtime)


async def test_status_reports_runtime_facts(runtime) -> None:
    await invoke(runtime, ".status")
    text = last_text(runtime)
    assert "2.0.0" in text
    assert runtime.prefix in text


async def test_ping_reports_latency(runtime) -> None:
    await invoke(runtime, ".ping")
    assert "ms" in last_text(runtime)


async def test_id_shows_chat_and_sender(runtime) -> None:
    await invoke(runtime, ".id")
    text = last_text(runtime)
    assert "100" in text and "42" in text


async def test_features_are_listed_and_toggled(runtime) -> None:
    await invoke(runtime, ".features")
    assert "autoreply" in last_text(runtime)
    await invoke(runtime, ".feature autoreply on")
    assert await runtime.store.feature_enabled("autoreply") is True
    await invoke(runtime, ".feature autoreply off")
    assert await runtime.store.feature_enabled("autoreply") is False


async def test_feature_unknown_name(runtime) -> None:
    await invoke(runtime, ".feature nonsense on")
    text = last_text(runtime)
    assert "nonsense" in text


async def test_feature_dependency_is_enforced(runtime) -> None:
    # "auto_translate" needs a translation provider, which is not configured.
    await invoke(runtime, ".feature auto_translate on")
    text = last_text(runtime)
    assert "translation" in text.lower()
    assert await runtime.store.feature_enabled("auto_translate") is False


async def test_language_switch_changes_output(runtime) -> None:
    await invoke(runtime, ".lang fa")
    await invoke(runtime, ".status")
    text = last_text(runtime)
    assert "وضعیت" in text
    assert await runtime.store.get_language("global", "") == "fa"


async def test_language_rejects_unknown_code(runtime) -> None:
    await invoke(runtime, ".lang klingon")
    assert "klingon" in last_text(runtime)


async def test_per_chat_language_override(runtime) -> None:
    await invoke(runtime, ".lang chat fa")
    await invoke(runtime, ".status", chat_id=200)
    # chat 200 has no override, so English remains
    assert "Yura status" in last_text(runtime)
    await invoke(runtime, ".status", chat_id=100)
    assert "وضعیت" in last_text(runtime)


async def test_config_redacts_secrets(runtime) -> None:
    await invoke(runtime, ".config")
    text = last_text(runtime)
    assert runtime.config.api_hash not in text
    assert "hidden" in text.lower() or "masked" in text.lower()


async def test_selfcheck_passes(runtime) -> None:
    await invoke(runtime, ".selfcheck")
    text = last_text(runtime)
    assert "✓" in text


async def test_prefix_change_and_reset(runtime) -> None:
    await invoke(runtime, ".prefix !")
    assert "yes" in last_text(runtime).lower() or "confirm" in last_text(runtime).lower()
    await invoke(runtime, ".prefix ! --yes")
    assert runtime.prefix == "!"
    assert await runtime.store.get_setting("command_prefix") == "!"
    await invoke(runtime, "!help")
    assert "!remind" in sent_text(runtime)
    await invoke(runtime, "!prefix reset --yes")
    assert runtime.prefix == "."


async def test_prefix_rejects_long_values(runtime) -> None:
    await invoke(runtime, f".prefix {'x' * 30} --yes")
    assert runtime.prefix == "."


async def test_dryrun_blocks_side_effects_but_answers(runtime) -> None:
    await invoke(runtime, ".dryrun on")
    assert "on" in last_text(runtime).lower()
    sent_before = len(runtime.client.sent)
    await invoke(runtime, ".remind 5m test")
    assert len(runtime.client.sent) > sent_before
    assert await runtime.store.list_reminders() == []
    await invoke(runtime, ".dryrun off")
    await runtime.permissions.set_dry_run(False)
    runtime.sender.dry_run = False


async def test_emergency_stop_blocks_commands(runtime) -> None:
    await invoke(runtime, ".stop")
    assert await runtime.store.emergency_stop() is True
    await invoke(runtime, ".remind 5m while stopped")
    assert await runtime.store.list_reminders() == []
    await invoke(runtime, ".stop resume")
    assert await runtime.store.emergency_stop() is False


async def test_emergency_stop_status(runtime) -> None:
    await runtime.store.set_emergency_stop(True)
    await invoke(runtime, ".stop status")
    assert "active" in last_text(runtime).lower()


# ---------------------------------------------------------------------------
# Text and utility commands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line,expected",
    [
        (".calc 2+2*3", "8"),
        (".calc sqrt(81)", "9"),
        (".calc 10/4", "2.5"),
    ],
)
async def test_calc(runtime, line: str, expected: str) -> None:
    await invoke(runtime, line)
    assert expected in last_text(runtime)


async def test_calc_rejects_code(runtime) -> None:
    await invoke(runtime, ".calc __import__('os').system('ls')")
    text = last_text(runtime)
    assert "os" not in text or "error" in text.lower() or "invalid" in text.lower()


async def test_calc_division_by_zero(runtime) -> None:
    await invoke(runtime, ".calc 1/0")
    assert "zero" in last_text(runtime).lower() or "error" in last_text(runtime).lower()


async def test_digits_conversion(runtime) -> None:
    await invoke(runtime, ".digits fa 1234")
    assert "۱۲۳۴" in last_text(runtime)
    await invoke(runtime, ".digits en ۱۲۳۴")
    assert "1234" in last_text(runtime)


async def test_digits_from_reply(runtime) -> None:
    reply = FakeMessage(id=2, chat_id=100, text="total 42")
    await invoke(runtime, ".digits fa", reply=reply)
    assert "۴۲" in last_text(runtime)


async def test_normalize_persian(runtime) -> None:
    await invoke(runtime, ".normalize علي")
    assert "علی" in last_text(runtime)


async def test_rtl_analysis(runtime) -> None:
    await invoke(runtime, ".rtl سلام hello")
    text = last_text(runtime).lower()
    assert "direction" in text and "persian letters" in text


async def test_detect_language_en_and_fa(runtime) -> None:
    await invoke(runtime, ".detect hello world this is english text")
    assert "en" in last_text(runtime).lower()
    await invoke(runtime, ".detect سلام دنیا این یک متن فارسی است")
    assert "fa" in last_text(runtime).lower()


async def test_textstats(runtime) -> None:
    await invoke(runtime, ".textstats one two three")
    text = last_text(runtime)
    assert "3" in text


async def test_transform_upper(runtime) -> None:
    await invoke(runtime, ".transform upper hello")
    assert "HELLO" in last_text(runtime)


async def test_cleanup_collapses_whitespace(runtime) -> None:
    await invoke(runtime, ".cleanup a    b")
    assert "a b" in last_text(runtime)


async def test_coin_and_random(runtime) -> None:
    await invoke(runtime, ".coin")
    assert last_text(runtime) in {"🪙 Heads", "🪙 Tails", "Heads", "Tails"} or "Heads" in last_text(runtime) or "Tails" in last_text(runtime)
    await invoke(runtime, ".random 5 6")
    assert last_text(runtime).strip().endswith(("5", "6"))
    await invoke(runtime, ".random uuid")
    assert len(last_text(runtime).strip()) >= 32


async def test_time_commands(runtime) -> None:
    await invoke(runtime, ".time")
    assert "UTC" in last_text(runtime)
    await invoke(runtime, ".convert-time 10:30 UTC Asia/Tehran")
    text = last_text(runtime)
    assert "14:00" in text


async def test_joke_and_quote_are_non_empty(runtime) -> None:
    await invoke(runtime, ".joke")
    assert len(last_text(runtime)) > 15
    await invoke(runtime, ".quote")
    assert len(last_text(runtime)) > 10


async def test_joke_in_persian(runtime) -> None:
    await invoke(runtime, ".joke", language="fa")
    assert any("\u0600" <= ch <= "\u06FF" for ch in last_text(runtime))


async def test_translate_without_provider_explains(runtime) -> None:
    await invoke(runtime, ".translate fa hello")
    assert "TRANSLATE_PROVIDER" in last_text(runtime) or "not configured" in last_text(runtime).lower()


async def test_summarize_requires_text(runtime) -> None:
    await invoke(runtime, ".summarize")
    assert "reply" in last_text(runtime).lower() or "text" in last_text(runtime).lower()


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
async def test_remind_creates_pending_reminder(runtime) -> None:
    await invoke(runtime, ".remind 30m drink water")
    text = last_text(runtime)
    assert "drink water" in text
    rows = await runtime.store.list_reminders()
    assert len(rows) == 1 and rows[0]["text"] == "drink water"


async def test_remind_rejects_bad_duration(runtime) -> None:
    await invoke(runtime, ".remind abcdef water")
    text = last_text(runtime).lower()
    assert "time" in text or "duration" in text
    assert await runtime.store.list_reminders() == []


async def test_remind_in_persian(runtime) -> None:
    await invoke(runtime, ".remind ۱۰m آب بخور", language="fa")
    rows = await runtime.store.list_reminders()
    assert rows and rows[0]["text"] == "آب بخور"


async def test_reminders_list_and_cancel(runtime) -> None:
    await invoke(runtime, ".remind 10m first")
    await invoke(runtime, ".reminders")
    assert "first" in last_text(runtime)
    await invoke(runtime, ".reminders cancel 1")
    assert await runtime.store.list_reminders() == []


async def test_reminders_cancel_missing_id(runtime) -> None:
    await invoke(runtime, ".reminders cancel 999")
    assert "999" in last_text(runtime)


async def test_schedule_and_list(runtime) -> None:
    await invoke(runtime, ".schedule 2h hello later")
    rows = await runtime.store.list_schedules()
    assert rows[0]["text"] == "hello later"
    await invoke(runtime, ".scheduled")
    assert "hello later" in last_text(runtime)


async def test_schedule_cancel(runtime) -> None:
    await invoke(runtime, ".schedule 2h bye")
    await invoke(runtime, ".scheduled cancel 1")
    assert await runtime.store.list_schedules(enabled_only=True) == []


async def test_agenda_is_empty_initially(runtime) -> None:
    await invoke(runtime, ".agenda")
    assert "Nothing scheduled" in last_text(runtime) or "empty" in last_text(runtime).lower()


async def test_quiet_hours_roundtrip(runtime) -> None:
    await invoke(runtime, ".quiet-hours on 23:00 07:00")
    row = await runtime.store.get_setting("quiet_hours")
    assert row is not None
    await invoke(runtime, ".quiet-hours status")
    assert "23:00" in last_text(runtime)
    await invoke(runtime, ".quiet-hours off")
    assert await runtime.store.get_setting("quiet_hours") is None


async def test_quiet_hours_rejects_bad_time(runtime) -> None:
    await invoke(runtime, ".quiet-hours on 25:99 07:00")
    assert await runtime.store.get_setting("quiet_hours") is None


async def test_tasks_reflect_scheduler(runtime) -> None:
    await invoke(runtime, ".tasks")
    assert "task" in last_text(runtime).lower()


# ---------------------------------------------------------------------------
# Information: bookmarks, notes, search, media, export
# ---------------------------------------------------------------------------
async def test_bookmark_add_list_search_delete(runtime) -> None:
    await invoke(runtime, ".bookmark save Groceries milk and bread")
    assert await runtime.store.search_bookmarks("Groceries")
    await invoke(runtime, ".bookmark list")
    assert "Groceries" in last_text(runtime)
    await invoke(runtime, ".bookmark search bread")
    assert "Groceries" in last_text(runtime)
    await invoke(runtime, ".bookmark delete 1")
    assert await runtime.store.list_bookmarks() == []


async def test_bookmark_search_no_match(runtime) -> None:
    await invoke(runtime, ".bookmark search nothing-here")
    assert "No results" in last_text(runtime) or "nothing-here" in last_text(runtime)


async def test_note_lifecycle(runtime) -> None:
    await invoke(runtime, ".note add call the dentist tomorrow")
    assert await runtime.store.search_notes("dentist")
    await invoke(runtime, ".note list")
    assert "dentist" in last_text(runtime)
    await invoke(runtime, ".note delete 1")
    assert await runtime.store.search_notes("dentist") == []


async def test_note_from_reply(runtime) -> None:
    reply = FakeMessage(id=3, chat_id=100, text="remember this idea about caching")
    await invoke(runtime, ".note add", reply=reply)
    assert await runtime.store.search_notes("caching")


async def test_bookmark_from_reply(runtime) -> None:
    reply = FakeMessage(id=4, chat_id=100, text="the recipe uses saffron")
    await invoke(runtime, ".bookmark save Saffron recipe", reply=reply)
    rows = await runtime.store.search_bookmarks("saffron")
    assert rows


async def test_media_info_without_reply(runtime) -> None:
    await invoke(runtime, ".media info")
    assert "reply" in last_text(runtime).lower()


async def test_media_info_with_document(runtime) -> None:
    from conftest import FakeDocument

    reply = FakeMessage(id=5, chat_id=100, media=FakeDocument(size=2048, file_name="report.pdf"))
    await invoke(runtime, ".media info", reply=reply)
    text = last_text(runtime)
    assert "report.pdf" in text
    assert "2.0 KB" in text


async def test_labels_and_chatinfo(runtime) -> None:
    await invoke(runtime, ".labels add work")
    assert await runtime.store.chat_labels(100) == ["work"]
    await invoke(runtime, ".chatinfo")
    assert "100" in last_text(runtime)


async def test_export_writes_file(runtime) -> None:
    await invoke(runtime, ".bookmark save Exported item")
    await invoke(runtime, ".export bookmarks")
    files = list(Path(runtime.config.data_dir).rglob("havoc-bookmarks-*.json"))
    assert files, "export must write a JSON file"
    assert json.loads(files[0].read_text())


async def test_saved_overview(runtime) -> None:
    await invoke(runtime, ".saved")
    text = last_text(runtime)
    assert "Bookmark" in text or "bookmark" in text


# ---------------------------------------------------------------------------
# Profile commands
# ---------------------------------------------------------------------------
async def test_profile_reports_state(runtime) -> None:
    await invoke(runtime, ".profile")
    assert "profile" in last_text(runtime).lower()


async def test_counter_increment_and_list(runtime) -> None:
    await invoke(runtime, ".counter inc habits")
    await invoke(runtime, ".counter list")
    assert "habits" in last_text(runtime)
    assert await runtime.store.get_counter("habits") == 1


async def test_clock_toggle(runtime) -> None:
    await invoke(runtime, ".clock on")
    assert await runtime.store.feature_enabled("clock") is True
    await invoke(runtime, ".clock off")
    assert await runtime.store.feature_enabled("clock") is False


async def test_bio_set_and_show(runtime) -> None:
    await invoke(runtime, ".bio set Available on weekdays")
    text = last_text(runtime)
    assert "Available" in text
    await invoke(runtime, ".bio")
    assert "Available" in last_text(runtime)


async def test_preset_save_and_list(runtime) -> None:
    await invoke(runtime, ".preset create work Ali Working hours")
    await invoke(runtime, ".preset list")
    assert "work" in last_text(runtime)
    assert await runtime.store.get_profile_preset("work") is not None


async def test_profile_history_records_change(runtime) -> None:
    await invoke(runtime, ".bio set first")
    await invoke(runtime, ".bio set second")
    await invoke(runtime, ".profile history")
    assert "first" in last_text(runtime) or "second" in last_text(runtime)


# ---------------------------------------------------------------------------
# Messaging commands
# ---------------------------------------------------------------------------
async def test_autoreply_full_cycle(runtime) -> None:
    await invoke(runtime, ".autoreply add hi there => hello friend")
    await invoke(runtime, ".autoreply list")
    text = last_text(runtime)
    assert "hi there" in text and "hello friend" in text
    await invoke(runtime, ".autoreply on")
    assert await runtime.store.feature_enabled("autoreply") is True
    await invoke(runtime, ".autoreply clear")
    assert await runtime.store.list_autoreplies() == []


async def test_autoreply_requires_separator(runtime) -> None:
    await invoke(runtime, ".autoreply add no separator here")
    assert "=>" in last_text(runtime)


async def test_autoreply_rejects_bad_regex(runtime) -> None:
    await invoke(runtime, ".autoreply add [unclosed => nope --type regex")
    assert await runtime.store.list_autoreplies() == []


async def test_away_toggle(runtime) -> None:
    await invoke(runtime, ".away on Back in an hour")
    assert await runtime.store.feature_enabled("away") is True
    assert "Back in an hour" in await runtime.store.get_setting("away.message", "")
    await invoke(runtime, ".away off")
    assert await runtime.store.feature_enabled("away") is False


async def test_template_lifecycle(runtime) -> None:
    await invoke(runtime, ".template add greeting Hi {name}, how are you?")
    row = await runtime.store.get_template("greeting")
    assert row and "{name}" in row["body"]
    await invoke(runtime, ".template show greeting")
    assert "greeting" in last_text(runtime)
    await invoke(runtime, ".template list")
    assert "greeting" in last_text(runtime)


async def test_template_send_with_values(runtime) -> None:
    await invoke(runtime, ".template add greeting Hi {name}!")
    await invoke(runtime, ".template send greeting --name Ali")
    assert any("Hi Ali!" in message.text for message in runtime.client.sent)


async def test_template_rejects_unknown_placeholder(runtime) -> None:
    await invoke(runtime, ".template add bad Hi {unknown}")
    row = await runtime.store.get_template("bad")
    assert row is None


async def test_fight_requires_reply(runtime) -> None:
    await invoke(runtime, ".fight")
    assert "reply" in last_text(runtime).lower()


async def test_fight_drafts_and_sends(runtime) -> None:
    reply = FakeMessage(id=6, chat_id=100, text="your code is slow and boring")
    await invoke(runtime, ".fight clever", reply=reply)
    draft = await runtime.store.get_chat_setting(100, "fight.draft")
    assert draft
    await invoke(runtime, ".fight send")
    assert any(str(draft) in message.text or message.text == str(draft) for message in runtime.client.sent)


async def test_fight_refuses_threats(runtime) -> None:
    reply = FakeMessage(id=7, chat_id=100, text="I will kill you")
    await invoke(runtime, ".fight", reply=reply)
    text = last_text(runtime).lower()
    assert "refus" in text or "not" in text
    assert await runtime.store.get_chat_setting(100, "fight.draft") is None


async def test_fight_never_echoes_insults(runtime) -> None:
    reply = FakeMessage(id=8, chat_id=100, text="you are an idiot and a loser")
    await invoke(runtime, ".fight", reply=reply)
    draft = str(await runtime.store.get_chat_setting(100, "fight.draft"))
    assert "idiot" not in draft.lower()


async def test_fight_styles_lists_options(runtime) -> None:
    await invoke(runtime, ".fight styles")
    text = last_text(runtime)
    assert "sarcastic" in text


async def test_suggest_requires_reply(runtime) -> None:
    await invoke(runtime, ".suggest friendly")
    assert "reply" in last_text(runtime).lower()


async def test_suggest_offers_tone(runtime) -> None:
    reply = FakeMessage(id=9, chat_id=100, text="Can we meet tomorrow at 10?")
    await invoke(runtime, ".suggest friendly", reply=reply)
    assert len(last_text(runtime)) > 10


async def test_rules_overview(runtime) -> None:
    await invoke(runtime, ".rules")
    assert "Auto-repl" in last_text(runtime)


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------
async def test_chatsettings_roundtrip(runtime) -> None:
    await invoke(runtime, ".chatsettings set automation.denied true")
    assert await runtime.store.get_chat_setting(100, "automation.denied") is True
    await invoke(runtime, ".chatsettings show")
    assert "automation.denied" in last_text(runtime)


async def test_alias_runs_command(runtime) -> None:
    await invoke(runtime, ".alias add st status")
    await invoke(runtime, ".st")
    assert "Yura status" in last_text(runtime)


async def test_alias_list_and_remove(runtime) -> None:
    await invoke(runtime, ".alias add st status")
    await invoke(runtime, ".alias list")
    assert "st" in last_text(runtime)
    await invoke(runtime, ".alias remove st")
    assert await runtime.store.resolve_alias("st") is None


async def test_alias_cannot_shadow_command(runtime) -> None:
    await invoke(runtime, ".alias add help status")
    assert await runtime.store.resolve_alias("help") is None


async def test_quick_action(runtime) -> None:
    await invoke(runtime, ".quick add pingit ping")
    assert await runtime.store.get_setting("quick.pingit") == "ping"
    await invoke(runtime, ".quick pingit")
    assert "pong" in last_text(runtime)


async def test_quick_list(runtime) -> None:
    await invoke(runtime, ".quick add hello help")
    await invoke(runtime, ".quick list")
    assert "hello" in last_text(runtime)


async def test_backup_export_and_list(runtime) -> None:
    await invoke(runtime, ".backup export")
    text = last_text(runtime)
    assert ".json" in text
    files = list(Path(runtime.config.backup_dir).glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["app"] == "havoc" and payload["version"] == 1
    await invoke(runtime, ".backup list")
    assert files[0].name in last_text(runtime)


async def test_backup_import_restores_data(runtime) -> None:
    await runtime.store.add_template("keepme", "body text")
    await invoke(runtime, ".backup export")
    backup = next(Path(runtime.config.backup_dir).glob("*.json"))
    await runtime.store.delete_template("keepme")
    await invoke(runtime, f".backup import {backup.name} --yes")
    assert await runtime.store.get_template("keepme") is not None


async def test_backup_import_rejects_foreign_file(runtime) -> None:
    bogus = Path(runtime.config.backup_dir) / "bogus.json"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_text('{"app": "other", "version": 1, "tables": {}}')
    await invoke(runtime, f".backup import {bogus.name} --yes")
    assert "invalid" in last_text(runtime).lower() or "not" in last_text(runtime).lower()


async def test_plugins_list(runtime) -> None:
    await invoke(runtime, ".plugins list")
    text = last_text(runtime)
    assert "plugin" in text.lower()


# ---------------------------------------------------------------------------
# Detection / automation declarations
# ---------------------------------------------------------------------------
async def test_watch_add_and_list(runtime) -> None:
    await invoke(runtime, ".watch add urgent payment")
    await invoke(runtime, ".watch list")
    assert "urgent" in last_text(runtime) or "payment" in last_text(runtime)


async def test_trigger_add_and_list(runtime) -> None:
    await invoke(runtime, ".trigger add hello-notify hello")
    await invoke(runtime, ".trigger list")
    assert "hello-notify" in last_text(runtime)


async def test_react_add_and_list(runtime) -> None:
    await invoke(runtime, ".react add nice 👍")
    await invoke(runtime, ".react list")
    assert "👍" in last_text(runtime)


async def test_detect_status(runtime) -> None:
    await invoke(runtime, ".detect status")
    assert "Detection engine" in last_text(runtime)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
async def test_commands_ignored_from_other_users(runtime) -> None:
    from conftest import FakeEvent, FakeMessage

    message = FakeMessage(id=600, chat_id=100, sender_id=999, text=".status", out=False)
    sent_before = len(runtime.client.sent)
    await runtime.handlers.on_outgoing(FakeEvent(message, chat_id=100, out=False))
    new = runtime.client.sent[sent_before:]
    # A refusal is allowed; executing the command is not.
    assert all("owner" in message.text.lower() for message in new)
    assert not any("Yura status" in message.text for message in new)


async def test_non_command_text_is_not_dispatched(runtime) -> None:
    from conftest import FakeEvent, FakeMessage

    message = FakeMessage(id=601, chat_id=100, sender_id=42, text="just chatting", out=True)
    sent_before = len(runtime.client.sent)
    await runtime.handlers.on_outgoing(FakeEvent(message, chat_id=100, out=True))
    assert len(runtime.client.sent) == sent_before


async def test_backup_rejects_unknown_action(runtime) -> None:
    """A typo must not silently export a backup instead of the intended action."""
    from conftest import invoke, sent_text

    directory = runtime.config.backup_dir
    before = sorted(directory.glob("havoc-backup-*.json")) if directory.exists() else []

    await invoke(runtime, ".backup restore havoc-backup-x.json")
    rendered = sent_text(runtime)
    assert "export, list, import, auto" in rendered, rendered
    after = sorted(directory.glob("havoc-backup-*.json")) if directory.exists() else []
    assert after == before, f"a backup was written for a rejected action: {after}"

    # The default action still works with no arguments at all.
    await invoke(runtime, ".backup")
    assert len(sorted(directory.glob("havoc-backup-*.json"))) == len(before) + 1


async def test_template_duplicate_is_explained(runtime) -> None:
    from conftest import invoke, sent_text

    await invoke(runtime, ".template add greet Hello {name}")
    await invoke(runtime, ".template add greet Hi {name}")
    rendered = sent_text(runtime)
    assert "already exists" in rendered, rendered
    assert "--replace" in rendered

    # --replace overwrites instead of erroring.
    await invoke(runtime, ".template add greet Hi {name} --replace")
    assert "updated" in sent_text(runtime)
    templates = await runtime.store.list_templates()
    greet = next(row for row in templates if row["name"] == "greet")
    assert greet["body"] == "Hi {name}"
