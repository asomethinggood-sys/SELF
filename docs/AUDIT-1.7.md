# Y.U.R.A 1.7 — what was broken, and what the walk found

This is not a changelog of features. It is the list of things that did not work,
found by running the project instead of reading it.

The instrument is `scripts/audit_live.py`. It builds the real runtime, enables every
feature, imports every module, then dispatches **every registered command** with no
arguments, with `--help`, with a realistic argument, and with up to three of its own
subcommands — 276 cases — and judges the answer: a missing locale key, a raw
`{placeholder}`, an escaped-entity double escape, a Python traceback, or silence.

```bash
python scripts/audit_live.py          # summary
python scripts/audit_live.py --json   # .audit.json, every case
```

It runs in CI on every push. The release is the point at which it prints `overall ok`
with zero broken cases and zero silent ones.

---

## The bug he actually felt: she answered instead of acting

`havoc/services/actions.py` executes tool calls, and a tool call only exists if the
model writes one. On the live account a free model often wrote a paragraph instead,
so "turn off my live clock" was answered and nothing happened — 1.6 had no floor
under that.

**Fixed in `havoc/services/intent.py`.** The sentence is read as an instruction and
turned into a line for the console she already has (`clock off`, `feature poetry off`,
`disable fal`, `remind 35m drink water`, `note add …`, `translate de …`,
`search …`). It runs through the same dispatcher as a typed command, so there is one
implementation, one reply, one audit trail.

Rules of restraint, because a wrong action is worse than a missing one:

* a switch verb **and** a nameable thing are both required;
* "do you like the clock on my profile?" is a question — no verb, no action;
* "why is the clock always in my profile?" is talk — it does not even ask for status;
* two names in one breath are two actions ("turn off fal and poetry");
* the model's own tool block wins when it exists; the sentence reader only fills in;
* only the owner. A stranger's sentence changes nothing.

## Stale locks

| Lock | What happened | Fix |
| --- | --- | --- |
| Dry run swallowed **every** reply | `.dryrun on` made the account mute; the words were simulated, not sent | `Sender.respond()` always delivers; dry run holds back side effects only |
| Dry run could not be turned off | `.dryrun off` is a state change, so it was previewed instead of run — a lock with the key inside it | `.dryrun` and `.stop` are exempt from their own gate |
| Cooldown started **before** the command ran | one failure on a typo locked the command for its whole cooldown | checked before, started only after the handler succeeds |
| A switched-off command had no switch | nothing could stop `fal` (a command with no feature behind it) | `.disable <command>` / `.enable <command>`, persisted, and the way back can never be switched off |
| Commands that answered nothing | `.calc`, `.search`, `.convert-time`, `.digits`, `.transform`, `.suggest`, `.summarize`, `.textstats`, `.normalize`, `.rtl`, `.detect` replied with silence when arguments were missing | the dispatcher notices a command that sent nothing and prints what it needs |

## Rot

| Rot | Detail |
| --- | --- |
| Two version strings | `havoc/__init__.py` said 1.6 while `havoc/version.py` said something else; the CLI printed the copy. One source now, and the test asserts against `label()` instead of a hard-coded number |
| 67 dead module-level functions | removed: unused formatters, unused validators, a second retry helper, an unused placeholder, an unused date formatter. The suite is the proof that nothing moved |
| 285 unreferenced locale strings | reported by the audit, not deleted: several are used through computed keys. Counted so the number cannot grow silently |
| Docs | `docs/COMMANDS.md` regenerated; `.enable` / `.disable` documented in both languages |

## What was checked and found healthy

* 86 modules import cleanly; `ruff` clean; `compileall` clean.
* 17 features switch on and off; the one that cannot (`auto_translate`) fails only
  because the deployment has no translator configured — that is a fact, not a bug.
* 7 tools: schema valid, and a guest's attempt is refused before anything runs.
* 626 locale keys used, 0 missing in English, 0 missing in Persian.
* State survives a restart: disabled commands, `actions.auto`, clock state, reminders.
* Reminders fire on tick, do not fire while the emergency stop is on, and fire after
  it is resumed.
* 276 command cases, 0 broken, 0 silent.

## Version

`Y.U.R.A 1.7 Open beta` — from `havoc/version.py`, which is the only place it lives.
