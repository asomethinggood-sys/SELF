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
| A switched-off command looked identical to a working one | `.help` showed it as if it worked; it now carries `×` and a one-line legend |
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
* 627 locale keys used, 0 missing in English, 0 missing in Persian.
* 177 SQL statements **prepared against the real schema**: no handler selects a
  column a migration has moved or dropped. That check is in the audit now — it is
  the quietest kind of rot, because nothing fails until someone runs that command.
* State survives a restart: disabled commands, `actions.auto`, clock state, reminders.
* Reminders fire on tick, do not fire while the emergency stop is on, and fire after
  it is resumed.
* 276 command cases, 0 broken, 0 silent.
* The scheduler's `recover()` no longer carries a no-op summary line, and a missed
  job under the "ask" policy is reported instead of silently dropped.

## Version

`Y.U.R.A 1.7 Open beta` — from `havoc/version.py`, which is the only place it lives.

### Dead code, removed rather than tolerated

Twenty-three functions and properties were defined and never once called — not by
the package, not by a test, not by the scripts, not by a document. They were the
residue of two refactors: a storage layer that grew better callers, and a
command helper layer that stopped using its own helpers.

Gone: `RedactionFilter.add_secret`, `Command.all_names`, `CommandContext.reply_error`,
`CommandContext.require_args`, `RateLimiter.check_cooldown`, `RateLimiter.check_outbound`
(a non-blocking twin of `acquire_outbound`, which is the one the sender calls),
`Store.delete_profile_snapshot`, `Store.get_diagnostic`, `Store.set_diagnostic`,
`Store.next_schedules`, `Store.next_reminders`, `feature_on`, `ActionCard.has_card`,
`AIResponse.is_text`, `Person.is_rude`, `PersonBook.set_relation`, `LanguageGuess.is_confident`,
`IncomingEvent.is_media`, `Scheduler.next_due`, `LocaleManager.report_missing`,
`validation.validate_int`.

Two were kept and put to work instead, because deleting them would have hidden a
real gap:

* `LocaleManager.find_placeholders` now checks that a translation carries exactly
  the placeholders English does. A dropped `{name}` is not a formatting error — it
  renders, silently missing the name. An extra one raises at render time, inside a
  handler, which is the "something went wrong" with no explanation.
* `HavocClient.is_authorized` is what `authorize()` asks now, so a failed session
  check is reported the same way in every path.

`scripts/audit_live.py` fails on an orphan from this commit on: `dead code ok ·
orphans 0`, gated in CI. A helper nobody calls is a promise nobody keeps, and the
sweep is only permanent if the day a new one appears is a red build.

### The everyday asks that were still only conversation

The floor covered switching things on and off, reminders, notes, translation and
search. Everything else — the small things a person actually types — reached her
as a sentence, and a free model would answer the sentence. "What time is it" got a
paragraph about time; the clock in the card did not appear.

Thirty-three phrasings now become the command they mean, written as whole
sentences (anchored at both ends, like the lookup rules) so a sentence that merely
*mentions* the words stays conversation:

| He says | She runs |
| --- | --- |
| `what time is it`, `what's the time in Tehran?`, `ساعت چنده` | `time`, `time tehran` |
| `what's today's date`, `what day is it` | `time` |
| `flip a coin`, `heads or tails`, `شیر یا خط` | `coin` |
| `give me a random number between 1 and 10` | `random 1 10` |
| `what's 12 times 8`, `what is 3 plus 4`, `calculate 45 / 9` | `calc 12 * 8`, `calc 3 + 4`, `calc 45 / 9` |
| `make a backup`, `back up the database` | `backup export` |
| `run a selfcheck`, `check yourself`, `are you ok` | `selfcheck` |
| `ping`, `how fast are you` | `ping` |
| `what language is this`, `این چه زبانیه` | `detect` |
| `tell me a poem`, `read me a hafez poem` | `poem` |
| `what's on my agenda`, `what do i have scheduled` | `agenda today` |
| `what's on my agenda this week` | `agenda week` |
| `show my bookmarks` | `bookmark list` |
| `summarize this`, `summarize the last 20 messages`, `tldr` | `summarize`, `summarize 20` |
| `what version are you`, `ورژن چیه` | `about` |

Spoken arithmetic is normalized first, so `times`, `plus`, `divided by` and `x`
become the operators before the shape is read. A place name is only accepted if it
is short and carries no content words: **"the time in that poem about"** is a
sentence about a poem, and it is answered as one. `are you there` and `are you
awake` were deliberately left out of `ping` — that is talking to her, not measuring
a round trip, and `.yura are you awake` still reaches her own voice.
