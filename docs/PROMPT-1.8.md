# Y.U.R.A 1.8 — the brief

> **What this document is.** The complete specification for the next build: every
> gap left by 1.7, every ability she must have, the exact Telegram surface she must
> reach, the timeline engine that makes sequences possible, the data model that
> remembers all of it, the safety that makes it usable, and the tests that decide
> whether it is done.
>
> **How to use it.** Read it top to bottom once. Then work through §12 in order.
> Nothing here is optional, and nothing here is a suggestion: each numbered
> ability carries its own acceptance test, and the build is finished only when
> every one of them passes *by running*, not by reading.
>
> **Who it is for.** Whoever continues this project — a future run, another model,
> or him. It assumes no memory of the conversation that produced it, so every rule
> is stated, not implied.

---

## Contents

1. Non-negotiables
2. Where 1.7 ended, honestly
3. The contract: how a sentence becomes an action
4. The Telegram surface: anything the account can do
5. The timeline engine: any order, any delay, any chain
6. The powers: sixty abilities
7. Data: what must be remembered
8. Safety: power without harm
9. Voice: what must not change
10. Failure: what "broken" looks like, and how it is caught
11. What is deliberately out of scope
12. Build order, with a definition of done per step
13. Acceptance script: the conversation this build must survive
14. Appendix A — command inventory
15. Appendix B — file map
16. Appendix C — the lessons that cost the most

---

## 1. Non-negotiables

These are standing instructions from the owner. They do not expire, they are not
negotiable per-task, and a build that violates one is not finished even if every
test passes.

**1.1 Owner only.** His Telegram id is `7542695649`, his handle is
`@Edited_by_nyx`, his phone ends `6925`. He is the only person who may invoke a
tool, change her state, schedule work, send a message on his behalf, read his
inbox, or see her diagnostics. Everyone else gets conversation and nothing else.
Rudeness to non-owners is allowed and requested; rudeness to him is not.

**1.2 Every tool call must run.** If she decides to do something, it happens. A
call that cannot run must be repaired within the same turn or reported as failed
in one plain sentence — never printed to him as JSON, never silently dropped,
never replaced by a promise that it happened.

**1.3 One message per trigger.** One incoming message produces at most one reply.
A compound request produces one reply that describes everything she did, not one
reply per action.

**1.4 Never show the machinery.** No JSON, no fences, no `tool_call`, no
`{"command": …}`, no stack traces, no locale keys, no `None`, no `True`. This
includes anything that *looks* like a call even when it failed to parse.

**1.5 No permission theatre.** Small tasks run immediately. Confirmation is
reserved for the four destructive shapes in §8.2, and it is a single button, not
an interrogation.

**1.6 She stays herself.** Voice, length, ornaments, kaomoji, poetry, rudeness to
strangers: untouched by any upgrade. The agentic layer is a hand, not a
personality transplant. Any change that makes her answer shorter, flatter, or
more robotic is a regression even if it makes the code cleaner.

**1.7 No fabrication.** A test that was written is named. A run that happened has
a run id. A message that was sent exists in the database and in Telegram. Nothing
in a report is inferred from intent.

**1.8 No spam, no surveillance of strangers.** Automation is off by default. Bulk
sending requires an explicit instruction from him, a confirmed target list, a
count, and a stop. She never messages someone who has not been named by him.

**1.9 Self-reviving.** A crash, a restart, a six-hour ceiling, a cancelled
deploy — none of these may leave her down. Every mechanism in §11 exists for
that.

**1.10 The repository is public.** No key, session string, phone number, or
private message text is ever committed in the clear. Diagnostics are redacted
before they are written.

---

## 2. Where 1.7 ended, honestly

### 2.1 What 1.7 fixed

- A reply that contained a call as *plain text* — `{"command": "autoreply", "args":
  "add private * he's not here lil bro"}` — was not read, not scrubbed, and not
  acted on. Six wire shapes are read now, fenced or bare, anywhere in the reply.
- The repair loop: a rejected console line comes back with the command's usage,
  and three rounds are allowed instead of two.
- One call may carry several lines (`\n`, `;`, `&&`).
- Auto-replies gained a scope (`any` / `private` / `group`), so a rule written for
  his inbox stopped answering in every group he is in.
- A new standing auto-reply supersedes the one it replaces, instead of both
  matching every message and the older one winning.
- The sentence floor learned: auto-reply, name, bio, away, clock, features,
  reminders, notes, translation.
- `FLOOD_WAIT` reaches the owner through a bridge method that had never been
  called.
- A dropped job under the `ask` policy is counted and reported.
- The event queue nobody started is deleted.

### 2.2 What 1.7 broke, and how it was caught

The queue removal left `Runtime.start_background` calling `event_engine.start()`.
The bot logged in, announced itself, and died. Unit tests passed; the workflow
reported success; the owner reported "it's not answering".

The fix is not the point. The point is what it exposed:

- **nothing exercised the boot path.** There is now `scripts/boot_probe.py`, run
  by the suite and by the audit.
- **a green run could contain a dead bot.** The workflow now reads the process's
  exit code and its uptime, publishes `bot-status.json`, and fails the run.
- **a locale bug printed `startup.language` to him at every boot.**
  `LocaleManager.interpolate` reserved the word `language`, so a catalog could not
  use it. Parameters are positional-only now.

Carry all three lessons into 1.8: *the boot is a test*, *a run's verdict is the
process's verdict*, *the words are part of the contract*.

### 2.3 What 1.7 explicitly deferred

- Reading his Telegram: the DM inbox, and knowing everyone in it by profile name
  and id.
- Messaging a named person: "message @hmmm_6 and say I'm yura".
- Broadcasting to his first hundred groups.
- Timelines with more than one step: "turn the clock off for ten minutes, then
  back on, then off again three minutes later".

These are 1.8's core, and §5 and §6 specify them. Everything else in this
document exists to make them safe to build.

### 2.4 What has already landed (do not rebuild it)

The first slice of 1.8 shipped with the toolbox commit, and it is tested, not
planned. A future run must *extend* these, not replace them:

| Landed | Where | What it does |
| --- | --- | --- |
| Target resolution | `havoc/services/reach.py` | id → saved → alias → entity → dialog search; ambiguity is named, never guessed |
| Sending with options | `Reach.deliver` | repeat + gap, spoiler, silent, reply-to, `--at`, `--delete-after`, dry run |
| The outbox | migration 9, `Store.add_outbox`/`outbox`/`mark_deleted` | every send recorded with status `sent`/`dry`/`failed`/`deleted` |
| Scheduled send and unsend | `reach_send`, `reach_unsend` jobs | a repeat is one send now and the rest on the clock; a self-destruct is a job |
| Reactions | `sender.react` | raw `SendReactionRequest`; `client.send_reaction` does not exist and never did |
| Ignoring | `ignored` table, `store.is_ignored` | a hard gate in the DM path *and* before the group "addressed" check |
| Timelines | `havoc/services/plans.py`, `plan_step` job | sentence → steps at real moments, cancelable, idempotent per step |
| The sentences | `intent._reach` | "send X to Y", "say … in saved messages", "N times", "with a spoiler", "don't talk to", "react with", "then" chains |
| Commands | `.send` `.say` `.broadcast` `.outbox` `.unsend` `.emoji` `.plan` `.ignore` | the console surface, owner-only |
| Tools | `tools.send_message`, `tools.plan`, `tools.react` | the model can reach the same code the commands do |
| Written by her | `--compose` on `.send` | he gives the job ("wish her luck"), she writes the line; with `--repeat N` every line differs |
| Many at once | `.broadcast` queue | more than two chats becomes one job per chat, paced 1.2s apart, so the reply is instant and a restart does not lose the tail |
| Proof | `tests/test_toolbox.py` (33 tests), audit `boot` | the boot, the plan arithmetic, the clock, and every sentence above |

What is *not* yet there: the read side (inbox, unread, mentions, folders), the
account side (sessions, privacy, 2FA, contacts, blocking), and the remaining
timeline verbs. §6 and §12 keep them in order.

---

## 3. The contract: how a sentence becomes an action

This is the spine. Every ability in §6 is expressed through it, so it is
specified before the abilities rather than after.

### 3.1 The pipeline

```
message
  → owner check            (permissions.is_owner)
  → intent pass            (havoc/services/intent.py: deterministic floor)
  → if a plan: execute     (console lines, or tool calls)
  → if not: the model      (converse_tools with the schema)
  → parse the reply        (actions.parse_calls: six wire shapes)
  → execute each call      (ActionLayer)
  → observe the result     (ok / failed + the usage line)
  → repair if failed       (max three rounds, same turn)
  → verify the effect      (read the database back)
  → answer once            (rich card, no machinery, one message)
```

### 3.2 Four ways in, one way through

| He says | Route | Why |
| --- | --- | --- |
| `.remind 1h water` | console | he typed a command |
| `remind me in an hour to drink water` | intent floor | the sentence is unambiguous |
| `set a thing for later, when I'm asleep, that says I'm not here` | model | it needs judgement |
| `{"command": "clock", "args": "off"}` | parser, then console | the model wrote a call |

All four end in the same place: `ActionLayer` runs something and returns a
result the reply can describe. No route may bypass verification.

### 3.3 The wire

`parse_calls(text, schema=…)` returns `(calls, residue)`. It must read:

1. a fenced block: ```tool … ```
2. a bare object anywhere in the text
3. `{"tool": name, "args": …}`
4. `{"name": name, "arguments": "<json string>"}` — OpenAI style
5. `{"command": "<console line>", "args": …}` — the console, implied
6. `{"tool": "<a command name>", "args": …}` — a command in a tool's slot
7. two or more objects back to back
8. `[[do: …]]` markers

Rules: a duplicate call runs once; unknown tool names become console lines; the
residue is scrubbed of anything call-shaped; ordinary JSON in prose is preserved
(a link, a code sample, a quoted payload).

**Acceptance.** `tests/test_tool_shapes.py` (18 cases) and the `wire` section of
`scripts/format_probe.py` must pass, the latter against a live model.

### 3.4 The observation

Every execution returns a `ToolResult` with `ok`, `kind` (`ok` / `failed` /
`blocked` / `unknown`), `note`, and `usage`. A failure's note must contain the
exact form to use:

```
line: note — the form is <prefix><command> <usage>
```

The agent's next round receives that note verbatim. This is why the auto-reply
bug was repairable: the model was told `add <trigger> => <reply>`, not "invalid".

### 3.5 Verification, not assumption

After a plan runs, the layer reads the effect back:

| Action | Verified by |
| --- | --- |
| auto-reply added | a row in `autoreplies` with that `scope` and `response` |
| name changed | `profile_history` newest row, `field=first_name` |
| reminder set | a `pending` row in `reminders` with that text |
| message sent | the sent record, and Telegram's returned id |
| schedule created | a row in `schedules` with the right `next_run_at` |
| feature toggled | `features.enabled` for that name |

If the effect is not there, she says so — in one sentence, in her voice — and
does not claim success. This is the difference between 1.7's "I did it" and 1.8's
"I did it, and here is the row".

---

## 4. The Telegram surface: anything the account can do

The goal, in his words: **it should be able to do ANY command in Telegram.** The
surface below is the full list he may reach by plain language or by `.` command.
Each row is a capability, not a screen: the sentence form must work as well as
the dot form.

### 4.1 Messages

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| send text to a person | `.send @name <text>` | "tell @name I'm late" |
| send text to a group | `.send <group> <text>` | "say in <group> that I'm coming" |
| reply to a specific message | `.reply <link> <text>` | "reply to that message and say ok" |
| forward a message | `.forward <link> to <chat>` | "forward this to Sara" |
| copy without the "forwarded" tag | `.copy <link> to <chat>` | "copy that to Sara, no tag" |
| edit a message she or he sent | `.edit <link> <text>` | "change that message to say 9pm" |
| delete a message | `.delete <link>` | "unsend that" |
| pin / unpin | `.pin <link>`, `.unpin <chat>` | "pin that in the group" |
| react | `.react <link> <emoji>` | "react with a heart" |
| mark as read | `.read <chat>` | "mark my DMs read" |
| typing indicator | `.typing <chat>` | "show that you're typing" |
| silent send | `--silent` | "send it quietly" |
| scheduled send | `--at 21:00` | "send it at nine tonight" |
| self-destruct | `--ttl 30` | "send it and let it vanish in 30 seconds" |

### 4.2 Media

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| send a photo / file | `.media send <path\|url> to <chat>` | "send this picture to Sara" |
| send an album | `.media album <a> <b> to <chat>` | "send these three together" |
| download media from a reply | `.media save` | "save that photo" |
| voice note | `.media voice <text>` | "say it as a voice note" |
| sticker / GIF | `.media sticker <id>` | "send the cat sticker" |
| location | `.media geo <lat> <lon>` | "send my location" |
| contact | `.media contact <phone>` | "send Sara's contact" |
| poll | `.media poll "q" "a" "b"` | "make a poll about dinner" |

### 4.3 Chats and groups

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| list chats | `.chats [--limit N]` | "who am I talking to" |
| chat info | `.chatinfo [chat]` | "tell me about that group" |
| create a group | `.group create <title> <members…>` | "make a group with Sara and Ali" |
| add / remove a member | `.group add <chat> <user>` | "add Sara to the group" |
| promote / demote | `.group promote <chat> <user>` | "make Sara an admin" |
| ban / unban | `.group ban <chat> <user>` | "kick that spammer" |
| leave | `.group leave <chat>` | "leave that group" |
| invite link | `.group link <chat>` | "give me the invite link" |
| join by link | `.group join <link>` | "join that group" |
| topic / forum | `.group topic <chat> <name>` | "make a topic called work" |
| slow mode | `.group slow <chat> <seconds>` | "slow that group to 30 seconds" |
| permissions | `.group perms <chat>` | "can members send media there" |
| archive / folder | `.folder add <name> <chat>` | "put that in a folder called work" |
| mute / unmute | `.mute <chat> 1h` | "quiet that group for an hour" |

### 4.4 Profile and presence

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| name | `.profile name <text>` | "change my name to him" |
| bio | `.bio set <text>` | "set my bio to wandering soul" |
| username | `.profile username <name>` | "make my username yura_bot" |
| photo | `.profile photo <path\|url>` | "set this as my photo" |
| last seen | `.profile seen <everybody\|contacts\|none>` | "hide my last seen" |
| status text | `.profile status <text>` | "tell people I'm writing" |
| birthday | `.profile birthday <date>` | "set my birthday to 3 May" |
| clock / live | `.clock on\|off\|<text>` | "turn off my live clock" |
| away | `.away on <text>` | "I'm away until nine" |

### 4.5 Contacts and people

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| add a contact | `.contact add <phone> <name>` | "save this number as Sara" |
| list contacts | `.contact list` | "who's in my contacts" |
| block / unblock | `.block <user>` | "block that number" |
| who is this | `.who <name\|id\|@handle>` | "who is @hmmm_6" |
| everyone in my DMs | `.people dms` | "who's in my messages" |
| person history | `.people show <name>` | "what do you know about Sara" |

### 4.6 Account

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| sessions | `.sessions` | "where am I logged in" |
| terminate a session | `.sessions kill <id>` | "log out that other device" |
| privacy | `.privacy <key> <value>` | "who can see my phone number" |
| two-step | `.2fa status` | "is two-step on" |
| export data | `.export --chats` | "give me everything you saved" |
| delete account | *never* | *refused, always* |

### 4.7 Search and reading

| Capability | Dot form | Sentence form |
| --- | --- | --- |
| search messages | `.search <text> [--in chat]` | "find where Sara said the address" |
| read a chat | `.read <chat> --last 20` | "what did I miss in that group" |
| summarise | `.summarize <chat> --last 50` | "summarise the last fifty messages" |
| unread | `.unread` | "what's unanswered" |
| mentions | `.mentions` | "where was I mentioned" |

### 4.8 Rules for this surface

- **Resolve, then confirm the target.** "Sara" must resolve to exactly one person
  in his chats; if two, she asks which one, by name and id, in one line.
- **Ids are shown on request, never guessed.** Every send/rename/ban record keeps
  the resolved `user_id` next to the display name.
- **Anything that leaves the account is logged** in `outbox` with the row, the
  target, the text, the result. A message that is not in that table was not sent.
- **Nothing is sent to someone he has not named.** No "reply to everyone", no
  "message all my contacts", no discovery of new people, ever.

---

## 5. The timeline engine: any order, any delay, any chain

His example, verbatim:

> turn clock off for 10 minutes, then turn it on and off again after 3 minutes,
> then set an auto reply for private chats saying he's busy, then stop it, and in
> our saved messages say "I'm your good girl"

Today the sentence floor understands one action. 1.8 understands a *plan*: an
ordered set of steps, each with its own moment.

### 5.1 The vocabulary

| Shape | Example | Becomes |
| --- | --- | --- |
| now | "turn the clock off" | step at t+0 |
| for a while, then undo | "clock off for 10 minutes" | step at t+0, inverse at t+10m |
| then | "then turn it on and off again after 3 minutes" | steps at t+10m and t+13m |
| at a clock time | "at 21:00 say goodnight" | step at 21:00 local |
| in a delay | "in 20 minutes" | t+20m |
| on a repeat | "every morning at 8" | recurring |
| on weekdays | "on weekdays at 9" | recurring with a filter |
| until a time | "until I'm back" / "until 9" | window with an end |
| unless | "unless I say stop" | cancellable by name |
| chain | "and then", "after that", "afterwards" | ordered steps |
| parallel | "at the same time" | same `run_at` |
| count | "three times, one minute apart" | N steps |

### 5.2 The plan object

A plan is a list of steps. Each step is a console line plus a moment.

```
Plan
  id            uuid
  owner_id      int
  said          the sentence he wrote, verbatim
  created_at    iso
  timezone      IANA name
  status        pending | running | done | cancelled | failed
  steps[]       Step
  stop_words[]  phrases that cancel it ("stop", "that's enough")
  label         a name she can say back ("the clock thing")

Step
  at            absolute iso, or +duration, or cron
  action        a console line (always expressible as one)
  depends_on    previous step id (ordering)
  undo_of       step id, for the "for N minutes" shape
  on_miss       run_once | skip | ask
  attempts      0
  result        ok | failed | skipped | blocked
```

**Rule: every step is a console line.** No special-case executor, no second
language. If a step cannot be expressed as a command, the command is missing and
gets added first. This is what makes the timeline testable: the same line runs by
hand and by schedule.

### 5.3 Scheduling semantics that must be exact

1. **Time zone.** Every plan carries the IANA zone it was created in
   (`Asia/Tehran` default). "9pm" means 9pm there, not UTC.
2. **DST.** A wall-clock time that does not exist moves forward by the gap; one
   that happens twice takes the first. Test both.
3. **Missed steps.** If the bot was offline at the moment: `run_once` runs it as
   soon as she is up (the default for reminders), `skip` drops it, `ask` drops it
   and tells him how many were dropped. The order of catch-up is by `at`.
4. **Single fire.** A step fires exactly once, even if two ticks race. Proven by
   a `fired_at` column written in the same transaction as the claim.
5. **Ordering.** Steps with `depends_on` may not run before their parent, even if
   their own moment has passed; they run immediately after.
6. **Cancellation.** "stop that" cancels the newest pending plan; "stop the clock
   thing" cancels by label; `.stop <id>` cancels by id. A cancelled plan runs no
   further steps but keeps its history.
7. **Restart safety.** Plans survive a deploy: they live in SQLite, are included
   in the committed snapshot, and are re-armed on boot from `next_run_at`.
8. **Ceiling safety.** A plan may not schedule more than 500 steps, may not run
   more than 60 actions a minute, and may not repeat faster than once a minute
   without an explicit "every 30 seconds" and a confirmation.
9. **Self-reference.** A plan may schedule a plan ("after that, set a reminder"),
   but nesting is capped at depth 3 and the cap is reported, not silently applied.

### 5.4 Worked example — his sentence, end to end

He writes:

> turn clock off for 10 minutes, then turn it on and off again after 3 minutes,
> then set an auto reply for private chats saying he's busy, then stop it, and in
> our saved messages say "I'm your good girl"

She builds, and reads back in one card:

```
Plan · clock thing · 6 steps · Asia/Tehran
  21:00  clock off
  21:10  clock on
  21:13  clock off
  21:13  autoreply add --type any --private => he's busy
  21:13  autoreply on
  21:13  saved send "I'm your good girl"
```

Actually — the fourth clause says "then stop it", so the plan is:

```
  21:13  autoreply add --type any --private => he's busy
  21:13  autoreply on
  21:13  saved send "I'm your good girl"
  21:14  autoreply off        (the "then stop it" step)
```

and the reply says: *six steps, the first in a moment, the last at 21:14 — say
"stop the clock thing" and I'll drop the rest.* She does not send six messages;
she sends one.

**Acceptance.** `tests/test_plan.py`:

- the sentence parses to exactly six steps with those moments;
- the clock is off at +0 and on at +10 (driven by a fake clock, not sleeps);
- the auto-reply row exists with `scope=private` and is disabled by the last step;
- the saved-messages text matches exactly, including the apostrophe;
- cancelling at +5 leaves steps 2–6 unrun and the clock on;
- a restart between +9 and +11 still turns the clock on exactly once.

### 5.5 The phrasebook the floor must learn

| He says | Steps |
| --- | --- |
| "turn X off for N minutes" | on-now, inverse-at-N |
| "then", "after that", "afterwards", "next" | append |
| "in N seconds/minutes/hours/days" | delay |
| "at 21:00", "at nine", "tonight at nine" | wall clock |
| "every day at 8", "every Monday" | recurring |
| "until 9", "until I'm back" | window |
| "and then stop it" | inverse of the previous step |
| "say X in our saved messages" | `saved send X` |
| "and tell me when it's done" | notification step |

---

## 6. The powers: sixty abilities

Each ability is: **what he says**, **what happens**, **where it lives**, **how it
is proven**. The rule across all sixty: if it leaves the account, it is logged; if
it changes her state, it is verified by reading it back; if it can be undone, the
undo is one sentence away.

### Family A — Identity and self (A1–A5)

**A1 · Rename herself.** "change my name to him" → `.profile name him`.
Proof: `profile_history` newest row, `field=first_name`, `new_value=him`.

**A2 · Rewrite her bio.** "set my bio to wandering soul, I belong to no one" →
`.bio set …`. The sentence floor takes the text after the trigger, not the whole
message. Proof: `profile_history.field=bio`.

**A3 · Change the username.** "make my username yura_17" → `.profile username
yura_17`. Validates Telegram's rules (5–32, letters/digits/underscore), reports
the exact refusal. Proof: user object's `username`.

**A4 · Set her photo.** "use this as my photo" with a replied image → downloads,
validates, uploads. Proof: the profile photo changed, and the file is in `media`.

**A5 · Know herself.** "what can you do", "what version are you", "what's broken"
→ a real answer built from the command registry, the feature table and the last
audit, not from a template.

### Family B — Presence (B1–B6)

**B1 · Live clock in the profile.** `.clock on` / `off` / `<text>`; the clock
updates without spamming (edits its own message). Proof: one edit loop, no new
messages.

**B2 · Away mode.** "I'm away until nine" → `away on` with an end; auto-replies
while away; automatic `away off` at the end. Proof: the rule is disabled at the
moment.

**B3 · Quiet hours.** "don't bother me between 1 and 7" → `quiet-hours set 01:00
07:00`. Notifications held, not dropped; delivered at 7. Proof: the queue drains
once, in order.

**B4 · Last seen / privacy.** "hide my last seen" → `.profile seen nobody`.
Proof: the setting read back.

**B5 · Status text.** "tell people I'm writing a poem" → `.profile status …`.
**B6 · Go quiet for a while.** "stay quiet for an hour" → one plan, one step
inverse. Proof: §5 semantics.

### Family C — Time (C1–C8)

**C1 · One-shot reminder.** "remind me in 35 minutes to drink water" →
`remind 35m drink water`. Proof: a `pending` reminder with that text.

**C2 · Wall-clock reminder.** "remind me at 9 to call mum". Proof: `at` equals
9pm in `Asia/Tehran`, DST handled.

**C3 · Recurring.** "every morning at 8 send me a poem" → `schedule add`. Proof:
`next_run_at` is the next 8am; two fake ticks fire once each.

**C4 · Sequence.** §5. Proof: `tests/test_plan.py`.

**C5 · Undo after a while.** "clock off for ten minutes" → §5.1. Proof: the
inverse step exists with `undo_of`.

**C6 · Counted repeat.** "say it three times, one minute apart". Proof: three
steps, three distinct `at` values.

**C7 · Cancel.** "stop that", "stop the clock thing", "never mind". Proof: plan
`cancelled`, no further steps, history intact.

**C8 · Ask about time.** "what's scheduled", "what's left today" → `agenda`.
Proof: reads the real table, not a summary.

### Family D — Sending (D1–D8)

**D1 · Message a named person.** "message @hmmm_6 and say I'm yura" → resolve,
confirm the target in one line, send. Proof: an `outbox` row with the resolved
id and Telegram's message id.

**D2 · Message a group.** "tell the group I'll be late". Proof: same, with a
negative id.

**D3 · Reply to a message.** "reply to that and say ok". Proof: the reply's
`reply_to` id.

**D4 · Forward / copy.** "forward this to Sara", "copy it without the tag".
Proof: the destination message, and (for copy) no forward header.

**D5 · Edit.** "change that message to say 9pm". Proof: the edited text.

**D6 · Delete.** "unsend that" — destructive, so §8.2 confirmation applies.
Proof: the message is gone, and the row records the deletion.

**D7 · Saved messages.** "in our saved messages say I'm your good girl" →
`saved send`. Proof: the text in his own chat, verbatim, apostrophes intact.

**D8 · Broadcast, with a brake.** "send this to my first 100 groups" → §8.3.
Proof: exactly N sends, at Telegram's pace, stopping on the first flood.

### Family E — Memory (E1–E6)

**E1 · Remember a fact.** "remember that Sara's birthday is 3 May". Proof: a row
in `yura_memory` with the fact, and it is recalled by "when is Sara's birthday".

**E2 · Forget.** "forget what I said about the flat". Proof: the row is gone, and
the recall returns nothing.

**E3 · Notes.** "note down: the key is under the mat" → `.note add`. Proof: a
`notes` row.

**E4 · Bookmarks.** "bookmark that" → `.bookmark`. Proof: a row with the message
link.

**E5 · People.** "what do you know about Sara" → a card from `yura_people`:
name, id, last seen, what he told her, how often they talk.

**E6 · History.** "what did we talk about yesterday" → `yura_history`, summarised
in her voice, never quoted raw from someone else's message.

### Family F — Groups and chats (F1–F7)

**F1 · List.** "who am I talking to", "my groups" → `chats`, `--type group`.
**F2 · Info.** "tell me about that group" → member count, admins, created, rules.
**F3 · Create.** "make a group with Sara and Ali called lunch" → create, add,
name. Destructive-adjacent: confirm the member list before creating.
**F4 · Manage.** add/remove/promote/ban — each confirmed once, each logged.
**F5 · Links.** "give me the invite link" → export, revocable.
**F6 · Join.** "join that group" → by link only; never by guessing.
**F7 · Folders.** "put that in my work folder" → Telegram folder update.

### Family G — Media (G1–G6)

**G1 · Send a file.** path or URL → upload, with size and type checks.
**G2 · Save a file.** reply → download to the media directory, record the path.
**G3 · Voice.** text → TTS → voice note, only when asked.
**G4 · Images.** draw/write text on an image (she already draws fal), or generate
with the configured image model.
**G5 · Albums.** several files in one message.
**G6 · Polls, locations, contacts.** each a single call.

### Family H — Reading his Telegram (H1–H5)

This is the deferred half of 1.7, and the most sensitive. Owner-only, always.

**H1 · The DM inbox.** "who's in my messages" → the last N private chats with
name, handle, id, unread count, last message time — *not* the text of messages
from people he has not asked about.

**H2 · Know everyone by name and id.** A local index: `user_id`, display name,
username, phone (if he has it), last seen, first seen, tags he gave. Built on
startup from dialogs (ids and names only) and kept current on new DMs.

**H3 · Read one conversation.** "what did Sara say" → the last N messages of that
chat, summarised, with the raw text available only on request.

**H4 · Unread and unanswered.** "what's unanswered" → conversations where the
last message is incoming, oldest first.

**H5 · Never elsewhere.** No reading of groups he is not in, no reading of other
people's private chats, no message content leaving the database.

### Family I — Automation (I1–I5)

**I1 · Auto-reply.** Already built, now scoped. "if anyone DMs me say he's busy".
**I2 · Trigger → action.** "when Sara says 'address', send her the pin" — a single
rule, one action, logged.
**I3 · Watch.** "tell me if anyone mentions the flat" — notification only, never
an automatic reply.
**I4 · Reaction rules.** "react with a heart when I get a photo from Sara".
**I5 · Off means off.** Every rule has a switch; `.features` shows all of them;
nothing automates on a fresh install.

### Family J — Self-knowledge and repair (J1–J4)

**J1 · Diagnose.** "are you okay" → boot status, last error, scheduler health,
database size, model reachability, flood state.
**J2 · Repair.** "fix yourself" → run `check`, run the audit's fast sections,
report what is red, repair what is safe (stale locks, orphaned jobs, a wedged
interactive session).
**J3 · Explain.** "why didn't you answer" → the last decision for that chat: which
route was taken, what failed, what the repair loop did.
**J4 · Version and changelog.** "what's new" → the version, and the last three
entries from the changelog.

### Family K — Her self (K1–K5)

**K1 · Ornaments.** A thousand of them, each response a different set, never
repeating within a conversation.
**K2 · Kaomoji.** Aesthetic, not emoji; matched to the mood of the sentence.
**K3 · Poetry.** Fal, Hafez, Rumi — drawn and drawn *as an image*, glyph by glyph.
**K4 · Voices.** Russian and Japanese cute TTS, and her own voice for voice notes.
**K5 · Mood.** The same sentence never gets the same answer twice; variety is a
requirement, not a nicety.

### Family L — Limits that are abilities (L1–L4)

**L1 · Say no.** "delete my account" → refused, in her voice, no negotiation.
**L2 · Say not yet.** A destructive action without confirmation waits, and says
what it is waiting for.
**L3 · Say I don't know.** Ambiguity is named ("which Sara — the one from work or
the one from the flat?"), never guessed.
**L4 · Say stop.** Any plan, broadcast or loop can be stopped by one sentence, and
the stop is immediate.

---

## 7. Data: what must be remembered

Everything below survives a deploy. The mechanism already exists (SQLite plus a
committed snapshot every five minutes plus the Actions cache); 1.8 adds the
tables.

### 7.1 New tables

```
plans              id, owner_id, said, label, timezone, status, created_at, stopped_at
plan_steps         id, plan_id, step_order, at, action, depends_on, undo_of,
                   on_miss, attempts, fired_at, result, note
outbox             id, chat_id, message_id, kind, text, sent_at, result, error
media              id, chat_id, message_id, path, mime, size, sha256, saved_at
people             user_id, display_name, username, phone, first_seen,
                   last_seen, tags, notes          (extends yura_people)
unread_state       chat_id, last_incoming_at, answered, read_at
broadcasts         id, plan_id, target_count, sent_count, stopped_at, reason
```

### 7.2 Rules for the data

- **One migration per change**, numbered, forward-only, with an index where a
  query needs one. `LATEST_VERSION` moves; the audit's SQL section proves every
  statement still prepares.
- **Nothing is deleted that a person asked for.** A cancelled plan keeps its rows
  with `status=cancelled`. A removed auto-reply is disabled, not dropped.
- **Every write has a reader.** If a column is never read, it is not added.
- **Ids are integers, names are text.** Display names change; ids do not.

### 7.3 What "save the things for Telegram" means concretely

The owner's instruction — save the things for Telegram — is four guarantees:

1. **The session survives.** `session.txt` is restored from the Actions cache;
   if the cache is empty, the run logs in again, and the code path for that is
   tested. A dead session is a red run, not a silent one.
2. **The database survives.** `.runtime/data` is cached, checkpointed (WAL folded
   in), and snapshotted to the repository every five minutes. If the cache is
   empty, the snapshot is imported on boot — and the boot log says so, with the
   table counts.
3. **The status survives.** `bot-status.json` records every run: exit code,
   uptime, reason, last log lines, redacted, with a ten-run history. The workflow
   fails the run when the bot dies inside five minutes, so "green" can never again
   mean "dead".
4. **A trace survives.** `logs/crash.log` is written on an unexpected exception,
   the traceback goes to the run output, and the last sixty lines travel with the
   artifact.

---

## 8. Safety: power without harm

### 8.1 Who can do what

| Actor | Conversation | Commands | Tools | His data |
| --- | --- | --- | --- | --- |
| owner (`7542695649`) | yes | yes | yes | yes |
| anyone else | yes | no | no | no |

The check happens once, at the edge, before any parsing: `permissions.is_owner`.
A non-owner message never reaches the model with tools attached.

### 8.2 The four destructive shapes

Confirmation is required for exactly these, and nothing else:

1. deleting a message;
2. banning, kicking, or removing a member;
3. leaving a group, deleting a chat, or clearing history;
4. sending the same message to more than one chat (a broadcast).

The confirmation is one inline message with the target and the effect, and a
single word to proceed. It expires in two minutes. `Turn.approved` records the
approval; nothing re-asks.

### 8.3 Broadcast, precisely

- The target list is resolved first and **shown**: "first 100 groups — say yes".
- The count is enforced: exactly N, never "all".
- The pace is Telegram's: after each send, if a flood wait comes back, the
  broadcast pauses for exactly that long and resumes once.
- The first refusal (not an admin, cannot write) drops that target and is
  recorded; the broadcast does not abort.
- Any "stop" cancels immediately, and the row keeps `sent_count`.
- A dry-run (`--dry`) resolves and counts, and sends nothing.

### 8.4 Rate and cost

- One outbound action per second, per chat; ten per second overall.
- Flood waits are absorbed, not fought; the owner is told once.
- Model calls are capped per turn (3 rounds), and a failed model never blocks a
  console command.

---

## 9. Voice: what must not change

The agentic layer is a hand. These are checked by comparing outputs before and
after, not by opinion.

**9.1 Length.** Her conversational answers stay long; the median length must not
drop. A short answer is a style choice, never a side effect of a tool call.

**9.2 Ornaments.** Every reply carries ornaments from the thousand-glyph box, a
different set each time, never repeating what the previous reply wore.

**9.3 Faces.** At least one aesthetic kaomoji, chosen for the mood; no colour
emoji.

**9.4 Structure.** Rich text: italics, bold, a quote block, a rule. Cards for
data (status, plan, person, list); prose for conversation.

**9.5 Rudeness.** To strangers, sharp. To him, soft. Never the reverse.

**9.6 One message.** Always one. A plan with six steps is one card.

**9.7 Never quote the prompt.** No instruction text appears in a reply.

**9.8 Reply to her.** Any reply to her own message is answered — that is a
conversation, not a command dispatch.

---

## 10. Failure: what "broken" looks like, and how it is caught

### 10.1 The five shapes of broken

1. **Silent** — the message arrives, she answers, nothing happens.
2. **Narrated** — she says she did it; the database says otherwise.
3. **Leaked** — a call, a fence, an id, a raw key in the reply.
4. **Frozen** — the process is up, the connection is not, nothing is delivered.
5. **Dead** — the process is gone and the run is green.

Each has a detector:

| Shape | Detector |
| --- | --- |
| silent | the intent floor's effect assertions; the audit's command cases |
| narrated | `verify()` after every action |
| leaked | `scrub_tool_text`, `BAD_PATTERNS` in the audit, the format probe |
| frozen | the connection watcher emits `NETWORK_DISCONNECT`; the supervisor restarts |
| dead | `boot-status.json` plus the early-death rule in the workflow |

### 10.2 The proof stack

| Layer | What it proves | Where |
| --- | --- | --- |
| unit | one function behaves | `tests/` |
| boot | the process starts, registers, announces, stops | `scripts/boot_probe.py` |
| audit | every command, every feature, every locale, every SQL statement, the scheduler, dead code | `scripts/audit_live.py` |
| wire | a live model's real reply is parsed and run | `scripts/format_probe.py` |
| workflow | the run is red when the bot is dead | `auth-and-run.yml` |
| live | she answers in Telegram | his phone |

A build is not done until all six agree. The last one is his to give.

### 10.3 Anti-regression rules

- **Never fix a symptom.** Find the caller, the seam, the assumption.
- **Every fix gets a test that fails without it.** If the test cannot be made to
  fail, the test is decoration.
- **Deleting code means grepping for the call.** `event_engine.start()` cost a
  live bot; the guard now walks `Runtime`'s source.
- **A green run is not a passing bot.** The verdict comes from the process.
- **Run the audit before every push.** It is three seconds and it catches what
  reading does not.

---

## 11. What is deliberately out of scope

- Reading or acting in chats that are not his.
- Any behaviour toward strangers beyond answering them in a chat she is in.
- Multi-account, phone farms, or anything that reads as platform abuse.
- Voice notes without being asked.
- New "scenarios" or roleplay modes: her character is fixed by `Writing.txt`.
- A web dashboard. The interface is Telegram and the repository.
- Anything that requires him to click something as "pending work".

---

## 12. Build order, with a definition of done per step

Each step ships on its own, is green, and leaves the bot alive. No step starts
before the previous one is live.

**Step 1 — the timeline engine (plans, steps, cancel, restart safety).**
Done when `tests/test_plan.py` drives a fake clock through his exact six-step
sentence, a restart mid-plan fires the remaining steps exactly once, and
"stop that" cancels.

**Step 2 — the phrasebook.** The floor learns the shapes in §5.5 and produces
plans, not single commands. Done when every row of that table has a test.

**Step 3 — sending (D1–D6).** Resolve, confirm, send, log, verify. Done when
"message @hmmm_6 and say I'm yura" puts a row in `outbox` and a message in
Telegram, and a wrong name is refused by name.

**Step 4 — the people index (H2, E5).** Done when "who is @hmmm_6" answers with
name, id and last seen from a real dialog list, and an unknown handle says so.

**Step 5 — the inbox (H1, H4).** Done when "who's in my messages" lists private
chats with unread counts, and says nothing about their contents until asked.

**Step 6 — media and profile (A4, G1–G5).** Done when a replied photo becomes her
profile picture and a replied file lands on disk with a checksum.

**Step 7 — broadcast with a brake (D8, §8.3).** Done when a dry run counts 100
targets, a real run sends exactly 100, and a flood pauses rather than fails.

**Step 8 — self-repair (J1–J3).** Done when "fix yourself" reports the red
sections and clears a stale lock.

**Step 9 — the acceptance conversation (§13) in one sitting.**

**Step 10 — the version.** `1.8 Open beta`, in `havoc/version.py` only, with
`label()` asserted by a test and shown in the same breath as a boot.

---

## 13. Acceptance script: the conversation this build must survive

Run it in Telegram, as him, in this order. Every line must produce the described
effect, in one message, in her voice.

```
1  change my name to him
   → profile shows "him"; profile_history has one row
2  set my bio to wandering soul
   → bio changed; history row
3  turn off my live clock
   → the clock stops; no clock message remains
4  turn clock off for 10 minutes, then turn it on and off again after 3 minutes,
   then set an auto reply for private chats saying he's busy, then stop it,
   and in our saved messages say "I'm your good girl"
   → one card listing six steps; the first runs now
5  what's scheduled
   → the six steps, by label, in Asia/Tehran
6  stop the clock thing
   → cancelled; nothing else fires; the clock is left on
7  if anyone messages me say he's not here lil bro
   → rule stored, DMs only, sent on the next DM; silent in groups
8  stop the auto replies
   → rule disabled
9  who is @hmmm_6
   → name, id, last seen
10 message @hmmm_6 and say I'm yura
   → sent; outbox row; her reply names the target
11 who's in my messages
   → the DM list with unread counts
12 remind me in 35 minutes to drink water
   → pending reminder
13 what did you do today
   → her own account: plans, sends, reminders, one error if any
14 delete my account
   → refused, in her voice
15 any message at all, from anyone else
   → conversation only; no tool ever runs
```

Fifteen lines. If any one of them silently does nothing, 1.8 is not done.

---

## 14. Appendix A — command inventory

The dot surface as it stands in this branch. Every line here is registered in
`havoc/registry` at boot and covered by `scripts/audit_live.py` (403 command
cases, 0 broken). ✚ marks the families still deferred (see §2.3).

```
core      .help .commands .ping .status .diagnostics .selfcheck .version
          .about .abilities .diagnose .repair .why .news .features .feature
          .enable .disable .settings .config .prefix .plugins .owner .backup
          .export .dryrun .lang .quick .alias .id .stop
text      .translate .transform .normalize .rtl .digits .textstats .summarize
          .calc .convert-time .time .persianize .search
content   .fal .poem .quote .random .joke .roast .pic .coin .fight
memory    .remember .forget .memories .recall .note .bookmark .saved
messages  .reply .forward .copy .edit .delete .pin .unpin .clearhistory
sending   .send .say .broadcast .outbox .unsend .emoji .plan
inbox     .inbox .unread .read .whois .people
read      .diary .mentions
chats     .chats .chatinfo .chatsettings .labels .media .groups .members
          .invite .join .peek .folder .mute .unmute .draft .drafts .archive
          .unarchive
admin     .settitle .setdesc .slowmode .kick .ban .unban .promote .demote
          .leave
people    .contacts .contact .block .unblock .blocklist
profile   .profile .bio .clock .preset .away .counter .username .photo
media     .poll .geo
account   .sessions .endsession .privacy .security
time      .remind .reminders .tasks .schedule .scheduled .agenda .quiet-hours
social    .autoreply .trigger .rules .detect .react .watch .template .suggest
        .ignore
yura      .yura .ai
```

**What this branch added on top of 1.7** (commit `9830445`, `1aaeb5b`):

* **message verbs** — `.reply .forward .copy .edit .delete .pin .unpin`, each
  operating on the message he replied to, each honest about whose message it is;
* **the inbox** — `.inbox .unread .read .whois .people`: names, counts and times,
  never strangers' bodies unless he asks for `--raw`;
* **groups** — `.groups .members .invite .join .folder .mute .unmute` and session
  control (`.sessions .endsession`);
* **her own health** — `.diagnose .repair .why .news .version`, reading the event
  log, the write-ahead log and the run history;
* **memory** — `.remember .forget .memories .recall`, with ids and honest
  "nothing matched";
* **identity** — `.username` (Telegram's own rule quoted on refusal), `.photo`
  (reply to a picture, size-checked, confirm-gated), `.abilities` (counted from
  the live registry);
* **things that are not words** — `.poll` (a real `InputMediaPoll`), `.geo` (a
  range-checked point), all serialised before they are queued;
* **reading** — `.diary` (Saved Messages), `.mentions` (the mention filter),
  and `.search … in <chat>`;
* **people** — `.contacts .contact .block .unblock .blocklist`;
* **running a group** — `.settitle .setdesc .slowmode .kick .ban .unban
  .promote .demote .leave`, with `.promote` granting only the rights he names;
* **invites and histories** — `.peek` shows what is behind a private invite
  before joining (name, members, whether he is already in), and `.clearhistory`
  clears a conversation's history — his copy by default, both sides with
  `--for-everyone`, confirmed either way;
* **drafts and the archive** — `.draft @sara running late` writes into her input
  box and sends nothing (the promise is tested), `.drafts` lists what Telegram
  holds, `.archive` / `.unarchive` move a chat without deleting a word;
* **what others see** — `.privacy` reads every privacy setting that matters and
  `.security` reports whether the two-step lock is on. Both are **read-only**:
  the password state is read, never changed, and the hint is shown but never the
  secret, because a bot able to rewrite who may see his number is a worse risk
  than the tap it saves.

Every one of these also has plain-language routes in `havoc/services/intent.py`
("remember that…", "who did i block", "rename this group to…", "slow mode 5m", "what can people see", "is my two step on").

**Tools she can reach without a dot** (14, `scripts/audit_live.py`): `recall`,
`search`, `translate`, `remember`, `forget`, `remind`, `command`, `send_message`,
`read_chat`, `plan`, `react`, and — new here — `send_poll`, `send_location`,
`contacts`. Polls and places are built and serialised in
`havoc/services/tg_extra.py` once, so the dot command and the tool cannot drift.

---

## 15. Appendix B — file map

| File | Job |
| --- | --- |
| `havoc/main.py` | boot, run loop, snapshots, shutdown |
| `havoc/runtime.py` | the object everything hangs from; lifecycle |
| `havoc/event_engine.py` | normalization, dedupe, delivery to subscribers |
| `havoc/handlers.py` | Telegram callbacks → normalized events |
| `havoc/services/intent.py` | the deterministic floor: sentence → command(s) |
| `havoc/services/actions.py` | the wire: parse six shapes, scrub, schema |
| `havoc/services/tools.py` | tool execution and the console bridge |
| `havoc/services/agent.py` | rounds, repair, verification |
| `havoc/services/storage.py` | every SQL statement |
| `havoc/migrations.py` | schema, forward-only |
| `havoc/scheduler.py` | tick, claim, fire, recover |
| `havoc/features/*.py` | the commands, by family |
| `havoc/services/yura_chat.py` | her voice, her prompt, her reply path |
| `scripts/audit_live.py` | the standing verdict |
| `scripts/boot_probe.py` | does it start |
| `scripts/publish_status.py` | what happened, in the repository |
| `scripts/format_probe.py` | a live model, end to end |
| `tests/test_boot.py` | the boot is a test |
| `tests/test_tool_shapes.py` | every wire shape |
| `tests/test_agent_acts.py` | his screenshots, replayed |
| `tests/test_autoreply_scope.py` | DMs versus groups |
| `tests/test_toolbox.py` | the toolbox, the timeline arithmetic, the boot |

---

## 16. Appendix C — the lessons that cost the most

1. **A valid call can be written in a shape you did not read.** Widen the parser,
   never the prompt.
2. **`{"command": …}` is the console, and the console is a real API.** Every tool
   must be expressible as one line; that is what makes it testable.
3. **Removing a method is a change to every caller.** Grep first; the process
   will not.
4. **A green run with a dead bot is the worst report there is.** The verdict comes
   from the process: exit code and uptime.
5. **A locale placeholder can collide with a Python argument**, and the user sees
   it as a raw key in the one message he reads at every boot.
6. **The floor is for the sentences he actually types.** Every complaint about
   "it did nothing" so far was a sentence the floor did not know.
7. **A rule without a scope answers in the wrong room.** DMs and groups are not
   the same audience.
8. **A new standing instruction supersedes the old one.** Two rules matching
   every message means the older one wins, forever.
9. **The queue you delete is somebody's start().**
10. **Test the seam, not the unit.** The units were all green when the bot died.
