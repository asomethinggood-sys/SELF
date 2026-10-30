# 1.7 — what was broken, what was done about it

This is the checklist behind the version. Every line is something that was
*observed*, not something that seemed likely: a command answered with nothing, a
call that was written but never run, a lock that held after a failure.

The rule I hold myself to is the one you set: fixing one symptom must not create
another. So each fix below names the test or the probe that now fails if it comes
back.

## The big one: a request that did nothing

Her hands only ran when the model wrote a fenced block:

```
```tool
{"tool": "command", "args": {"command": "clock", "args": "off"}}
```
```

The model in production did not write that. It wrote this, as plain text, three
times in one message:

```
{"command": "autoreply", "args": "add private * he's not here lil bro"}
```

Three separate failures stacked up:

1. the parser only understood fences, so the object was not a call;
2. the scrubber only looked for `{"tool":`, so the JSON was not removed;
3. the intent floor had no rule for an auto reply, so nothing else picked it up.

`parse_calls` now reads **six** shapes, fenced or bare, anywhere in the reply:

| Written as | Read as |
| --- | --- |
| `{"tool": "remind", "args": {...}}` | the tool |
| `{"name": "recall", "arguments": "…"}` | the tool, OpenAI style |
| `{"command": "autoreply", "args": "…"}` | her console, line `autoreply …` |
| `{"tool": "fal", "args": "page 166"}` | the console — `fal` is a command, not a tool |
| `{"tool": "command", "args": "fal page 166"}` | the console, split into name + rest |
| two objects, one after the other | two calls |

The same object written three times is executed once. Anything call-shaped that
survives parsing is scrubbed, so no shape can reach him with a copy button.
`src`: `tests/test_tool_shapes.py`, `tests/test_agent_acts.py`, the `wire` section
of the live probe.

## The console gave up instead of trying again

`{"command": "autoreply", "args": "add private * he's not here lil bro"}` is a
correct console line *except* that `.autoreply add` wants a `=>`. The agent loop
fed the error back — once, with no hint — and the model had to guess.

* the observation now carries the command's usage line, so the next round is a
  repair rather than a guess;
* the loop allows three rounds instead of two (one repair);
* one call may carry several lines (`\n`, `;`, `&&`), so a compound request is one
  action, not four;
* `--type any` needs no trigger word: the rule matches everything, and the new
  `scope` column decides *where* it may speak.

## "Reply to anyone who DMs me" answered in groups

The auto-reply table had one chat or every chat — nothing in between. A rule
written for his inbox therefore answered in every group he is in. Migration 8 adds
`autoreply.scope` (`any` / `private` / `group`), `.autoreply add --private`, and a
matcher that checks the room before it checks the words.

Proved by `tests/test_autoreply_scope.py`: a private rule answers a DM, says
nothing in a group, and a group rule says nothing in a DM.

## The sentence floor

1.6 answered "turn off my live clock" with a paragraph; the clock stayed on. 1.7
reads the sentence itself and runs the command:

| He says | She runs |
| --- | --- |
| `turn off my live clock` | `clock off` |
| `turn off fal and poetry` | `disable fal` · `feature poetry off` |
| `remind me to drink water in 35 minutes` | `remind 35m drink water` |
| `set an auto reply to every private chat. if anyone messages me say he's not here` | `autoreply add --type any --private => he's not here` · `autoreply on` |
| `change my profile name to him` | `profile name him` |
| `set my bio to wandering soul` | `bio set wandering soul` |
| `i'm away until 9` | `away on I am away until 9` |
| `whatever you did wrong, fix it` → `disable fal, poetry off` | one card, both actions |

Everything that is *not* an instruction stays conversation: "i was thinking about
you" no longer opens the about card, "the clock on my profile is pretty" is a
compliment, and "i set an autoreply yesterday" is a story. `tests/test_intent.py`
holds both halves.

## Silent commands, stale locks, dead code

| Bug | What it was | Fix |
| --- | --- | --- |
| `.calc` (and twenty others) answered nothing without arguments | a dead end after a keypress | the dispatcher answers with the command's usage when a handler says nothing |
| A command that failed on a typo locked itself out for its whole cooldown | cooldown started before the run | checked before, started only after success |
| `.dryrun on` made every command reply disappear | the command's own answer was treated as a side effect | a command's answer always goes out; the preview replaces only the *effect* |
| `FLOOD_WAIT` never reached the owner | the bridge had a `flood_wait` method nothing called | the sender announces floods through the bridge |
| A missed job under `ask` vanished silently | `summary["jobs_missed"] = 0` after dropping them | the count is reported, the owner is told |
| `.detect status` raised `KeyError` | it read a snapshot field the engine no longer had | the row is gone, the locale key with it |
| `1.7` was in one file and `1.6` in another | two version strings | `havoc/version.py` is the only one; the test asserts `label()` |

## The audit is the point

`scripts/audit_live.py` walks the project the way a user would, in CI:

* **imports** — 86 modules;
* **locales** — every key used exists in both languages, no orphans;
* **features** — every switch on and off, through the real feature manager;
* **actions** — all seven tools, schema and policy;
* **commands** — 276 dispatch cases: bare, `--help`, and a plausible argument for
  each; a case that answers nothing or answers with a locale key fails the build;
* **sql** — 171 statements prepared against a migrated database, so a renamed
  column cannot hide;
* **schedule** — a due reminder, a due job, and the three missed-work policies,
  each read back from the database;
* **dead code** — an AST pass over every definition; a function that nothing names
  fails the build.

Current run: `overall ok · 276 command cases · 0 broken · 0 silent · 171 statements
prepared · orphans 0 · todos 0 · bare excepts 0`.

## What is deliberately still open

The observation you want next — knowing every DM by name and id, reading your
Telegram, sending to groups in bulk — is the *next* step, not this one. 1.7 stops
at "every command works when I ask for it, and nothing I write leaks into the
chat."
