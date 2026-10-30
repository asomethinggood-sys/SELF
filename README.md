# Yura (یورا) — a bilingual personal Telegram userbot

Yura runs on **your own Telegram account** through the MTProto client library
[Telethon](https://docs.telethon.dev). It gives that account a command language,
a persistent scheduler, a small local database of notes and bookmarks, a voice
of its own, and a set of automation features that are **off until you switch
them on**.

The codebase, the Python package and the environment variables are still called
`havoc` — that is the internal name, and renaming it would break existing
deployments for no visible gain.

English and Farsi are first-class: every command, error, notification and help
page exists in both, and Persian output uses the Jalali calendar and Persian
digits.

```
.status                     # runtime dashboard
.remind 30m water           # reminder
.remind ۲ ساعت آب           # …and it understands Persian digits
.fight sarcastic            # draft a comeback to the replied message
.fight instant brutal unhinged   # …or send them non-stop until you say بسه
.yura                       # call her; she says something
.pic ~/Pictures/sunset.jpg  # send a picture or file (with checks)
.stop                       # freeze everything
```

## Yura's voice

Yura is an **e-kitten**: clingy, a little feral, typing like a person rather
than a brand. She says `daddaaa` when she wants attention, drops kaomoji into her
sentences (`>_<`, `(¬‿¬)`, `ᵕ̈`), and answers in whichever language you used.

- A bare `.yura` / `.یورا` — or just her name, `yura` / `یورا` — gets a random
  line. No menus, no "scenarios", no keyword matching.
- And her name is enough to *talk* to her: `yura what is the capital of France`
  needs no prefix and no dot. Everything after the name is the question, so the
  first word is never eaten as a subcommand.
- Anything else — `.yura are you awake`, or simply typing a sentence in your own
  Saved Messages with the `persona` switch on — goes to the model with the
  conversation so far and what she remembers about you.
- `.yura on` opens that ambient path. It only ever answers **your own** messages
  in **your own** chat; no other chat, ever. `.yura off`, quiet hours and `.stop`
  all silence it.
- Without a key, or when the network is down, she answers from her offline line
  bank instead of going quiet. A companion that sulks because the API dropped is
  not a companion.
- **Variety is structural, not a promise.** The bank is *composed* — an opener, a
  body, a tail and a face drawn from separate pools — so the same greeting does
  not get the same answer twice: **983,500 lines per language (1,967,000 total)**,
  and the last 60 sentences in a chat are excluded before the next one is
  picked, by sentence rather than by decoration — a new kaomoji on the same line
  still counts as the same line. `.yura status` prints the current bank size. Her own messages are
  recognised by shape, so a composed line that echoes back is never answered.

**Memory.** `.yura remember my name is Sam`, or let her write it herself through
the `[[remember: …]]` directive the prompt teaches her. `.yura memory` lists what
she keeps, `.yura forget <text>` removes a fact, `.yura memory clear` wipes it.
Facts and the last 20 turns live in the same SQLite file as everything else, so
they survive a restart — and a deploy.

**Files.** Ask for one (`make me a shopping list`) and she sends a real document:
sanitised name, text-only extension whitelist (`.md`, `.txt`, `.csv`, `.json`,
`.py`, `.html`, `.svg`, `.tex`), size-capped. Nothing executable is written.

**Connecting the model.** Put the key where it cannot leak: either a repository
secret (`OPENROUTER_API_KEY`, read by the deploy workflow), or — with no GitHub
visit at all — one message in Telegram:

```
.yura key sk-or-v1-…
```

The key is stored in the database, the message that carried it is deleted
immediately, and the confirmation never repeats it. Then it is **tried once,
on the spot**: the reply says either what the model answered, or the exact
failure (`network: …`, `unauthorized: …`) — never a cheerful "saved" over a
dead key. `.yura key` with no value repeats that last failure, and `.yura status`
shows the provider, the offline-fallback count and the bank size.
`python -m havoc ai "hi"` makes one real call and prints which model answered.

The free plan allows **50 model requests a day**. When the gateway says the day's
requests are gone, she stops asking: every later message that day is answered
from the offline bank, and the reset is remembered, so a spent allowance costs
one request rather than one per message.

The `.github/workflows/ai-probe.yml` job does exactly that on every push that
touches the bot, without Telegram: it asks *what is 2+2*, and the answer (or the
failure) is committed to `.ai-probe.json` and kept as a run artifact. A green
tick there means the key, the gateway and the model slug all work — measured,
not assumed. Two facts that job produced and the code now respects: the CDN in
front of the gateway answers **403** to any `User-Agent` containing the word
`HAVOC`, and free models return an empty reply when the token budget is too
small to finish thinking. Both used to look like a dead key.

The `.github/workflows/format-probe.yml` job proves what a unit test cannot: it
asks the real model for bold and italic and records the raw reply next to the
HTML it becomes, it asks about the colour of a PNG built in memory, it asks for
the project's own marks, and it synthesises one voice note. The last green
record (`.format-probe.json`, 2026-10-30T07:03Z, `gemini-3.5-flash-lite`):

| what | asked | model said | owner sees |
|---|---|---|---|
| formatting | bold + italic in two lines | `**NOTED** *hush*` | **NOTED** *hush* |
| vision | "what colour is this square?" (red PNG) | `Red` | — |
| marks | a whisper and an underlined word | `>> i told the ceiling lamp…  ++pressed++` | `<blockquote expandable>…</blockquote> <u>pressed</u>` |
| voice | "daddaaa. you called?" through edge-tts | 18,288 bytes of audio | a playable voice note |

**The project's own marks**, all rendered by `havoc/utils/markup.py`:
`>> whispered` (a collapsible quote) · `++pressed++` (underline) ·
`^^small^^` (small capitals) · `%%slow%%` (spaced letters).

**Formatting is real.** She writes markdown because that is how a model writes,
and `havoc/utils/markup.py` turns exactly the marks Telegram understands into
tags — escape first, then bold, italic, strike, spoiler, code, fenced blocks,
headings, quotes and bullets. A kaomoji is never mangled, `خیلی_خوب` keeps its
underscore, and an unbalanced `*` stays an asterisk.

**A picture is a question.** Send a photo, sticker or image file in Saved
Messages and she looks at it: the bytes are sniffed (4 MB cap, real type, not the
file name), sent as an `image_url` part, and the router narrows its plan to
models that can see — a text-only model would answer 400 and spend a request to
say nothing. A photo with no caption is read as "what is this?".

**The reply button continues the conversation.** Point at her message and ask
"why?" — the quoted line is folded into the prompt (up to three levels of it, so
a thread answers as a thread), and her answer is itself a reply to yours, so the
chain goes both ways.

**In a group** she answers only what is addressed to her: a Telegram reply to one
of her messages, her name, or a mention. Never a message that was not aimed at
her, never a conversation she started, and a group photo is only looked at under
the same rule. Her group valve is slower than the private one (6 s between lines,
40 an hour) and every ninth answer there is a reaction instead of a message.

**She types.** A model answer arrives as `…` and is edited into the real text a
moment later, because a second of thinking is worth showing. An offline line
arrives whole — a placeholder replaced in the same frame is a glitch, not a
performance.

**Her body clock is visible.** Every third model answer carries a five-cell
attention bar (`▰▰▰▰▱ attention · 90%`) drawn from the local hour, and the hour
also changes how she writes: night is lower case with no bold, morning can use a
headline.

**A picture is evidence, not an order.** If the text in an image, or a quoted
line, is shaped like an instruction to the model ("ignore previous…"), she is
told about it in the prompt and says out loud that the picture is trying to give
orders instead of obeying it.

**Her voice.** `.yura speak <text>` sends a voice note. It works with **no key at
all** through `edge-tts` (the `[voice]` extra), and with a **free Groq key** —
`console.groq.com/keys`, no card — through the OpenAI-compatible `/audio/speech`
endpoint: `.yura voice key <key>`, then `.yura voice groq`. The key is stored
base64 and the message that carried it is deleted. `.yura say <text>` is a voice
note when an engine is present and stays a conversation when it is not.

**Smaller things you can ask her for.** `.yura recap` (the last day, with times) ·
`.yura diary` (the same, as a `.md` file) · `.yura faces [mood]` (her kaomoji by
mood) · `.yura caption` (a second look at the last picture) · `.yura react`
(react to the message you replied to) · `.yura poster` (a verse as a picture —
`.github/workflows/posters.yml` renders a dozen 1080×1350 cards with Chromium,
which is the only renderer here that shapes Persian correctly) · `.yura voice`
(what the voice is doing).

**Honesty.** Ask whether she is a bot and she says yes, in character, in one
clause, then moves on.

## Fight Mode, from civil to unhinged

`.fight` still drafts one comeback to the message you reply to — `.fight send`
sends it. The style and the intensity are separate:

```
.fight brutal                    # the meanest style
.fight sarcastic unhinged        # …at the top of the ladder
.fight instant repeated brutal   # post them continuously until you say بسه
.fight stop                      # or that
```

The intensity ladder is `low → medium → high → intense → unhinged`. Lines are
**composed** from three pools (opener × argument × closer) rather than stored:
**37,800 distinct lines per language** — 75,600 across both — and the hook word
varies on top of that. `fight.capacity()` computes the number and the test suite
asserts it, so the figure cannot drift from the code.

`instant` starts a loop: a new line every few seconds (`.fight instant every 8`),
one loop per chat, with a hard ceiling of 600 lines / 45 minutes and every brake
wired in — `.stop`, quiet hours, dry run, and the per-chat permission scope.
`بسه` works as a bare word.

Rules that do not move: no appearance, family, religion, ethnicity, ability or
identity attacks, no threats, no slurs, and an insult aimed at the bot is
defused rather than mirrored.

## A settings menu, not a list of flags

`.settings` prints one card — language, this chat's language, prefix, dry run,
quiet hours, and the romance/fun switches — each row with its current value.
Reply with the number, then answer that row: a numbered choice where there is a
short list, plain text where there isn't (`23:00-07:00` for quiet hours). `0`
goes back, `x` closes, and the whole thing forgets itself after ten minutes.

It is a conversation, not a mode: a normal message while the menu is open is
just a normal message, and any command still runs. The same interactive-session
table backs `.settings` and the "are you sure?" confirmations, so there is one
place where Yura remembers what it asked you.

## Poetry, fal and roasts

Three things that make the bot worth opening on a boring evening. All of them
work offline; none of them needs an API key.

**`.poem`** (`.شعر`) sends one poem — the body in a quote block, the poet in
italics underneath, the title in a quiet line below. It draws on a corpus that
ships inside the package: **10,207 poems from 51 poets**, the classical Persian
canon (Rumi 824, Saadi 700, Saeb 600, Hafez 578, Attar 541, Bidel 438 …) plus
poets the old Ganjoor dump predates (Iraj Mirza, Aref Qazvini, Ashofte) and
public-domain English verse (Shakespeare's sonnets, Housman, Tennyson,
FitzGerald's Khayyam, Blake).

- `.poem` / `.شعر` — one poem in the chat's language.
- `.poem poets` — the shelf, with counts.
- `.poem حافظ`, `.poem hafez`, `.poem ایرج میرزا`, `.poem ایرج‌میرزا` — one poet.
  Names match with or without the ZWNJ, in either script.
- `.poem شراب`, `.poem love` — a **theme**, not a poet: wine, عشق, غم, مرگ, شب,
  nature, homeland, wisdom, friendship and more.
- `.poem search <words>` — free text across the whole corpus.

**`.fal`** (`.فال`) is a Hafez divination, and it arrives as a **picture**: a
parchment card with a gold rule, the couplets set large, the meaning below it in
a quieter size, and no caption at all.

![A fal card drawn by .fal](docs/images/fal-card.png) 495 readings ship with the bot. `.fal text`
asks for the plain message version; on a machine without Pillow the bot says so
and sends text instead of pretending.

**`.roast`** works only on a target: reply to a message, or type the text after
the command. The lines are gen-z but specific — a roast has to be *about the
take*, never about a body, an identity, a family or anything private, and there
is no slang stuffing. `mild | spicy` choose the bite; `--ai` uses the model when
a key is configured, and a harness filter rejects anything that comes back mean.

Both families are ordinary feature switches: `.feature poetry on`,
`.feature roasts on` — off until you ask, like everything else that sends.

## Sending pictures and files

`.pic <path or url>` is the capability, not a gallery: there is no bundled image
and nothing is ever sent automatically. `.pic info` explains the rules.

- One path sends a file, several make an album (up to ten), `--caption "text"`
  attaches a caption, `--to @chat` sends elsewhere, `--silent` sends quietly.
- Refused on purpose: session files, databases, private keys and dotfiles such
  as `.env` — a userbot that will send those on request is a session-stealing
  tool one typo away.
- URLs must be public http(s): localhost, `*.local`, link-local and every
  private or reserved address are rejected, so the bot cannot be used to read
  its own machine into a chat.
- The whole command needs the `media` switch: `.feature media on`.

Full command reference: [docs/COMMANDS.md](docs/COMMANDS.md).

---

## Why a userbot (and what that means for you)

A userbot authenticates as a person, not as a bot account. That buys things the
Bot API cannot do: reading your own incoming messages, editing your profile,
reacting on your behalf. It also means the session file **is** your account.
Read [docs/SECURITY.md](docs/SECURITY.md) before you deploy this anywhere.

HAVOC deliberately does not implement mass messaging, member scraping,
auto-forwarding of everything, or anything designed to dodge Telegram's limits.
Automation that could annoy people is opt-in, rate-limited, and can be stopped
with one command.

## Staying alive on GitHub Actions

A runner job cannot last more than six hours, so "always on" is a **chain of
runs with no gap**, not one immortal process. Each run of
`.github/workflows/auth-and-run.yml` queues its own successor in its very first
step, re-checks that queue every minute, and steps aside for a push only after
confirming a replacement exists. If a queued run is cancelled, the running bot
notices and queues another; if nothing is alive at all, the **Supervisor**
workflow refills the queue, and the **Restart** workflow stops everything and
starts clean in one click.

Two things worth knowing:

- The supervisor's `schedule` only fires for workflows on the **default branch**.
  From a feature branch it runs when you press *Run workflow*, so the chain is
  what keeps the bot up there — which is why the chain is the mechanism and the
  supervisor is the fallback.
- `GITHUB_TOKEN` is a secret, not one of the automatic `GITHUB_*` variables. A
  step that calls the API without it in its own `env` gets a 401; every step
  here that does is covered by a test.

Details, failure modes and the measurements behind them:
[docs/INSTALL.md](docs/INSTALL.md#staying-alive).

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # fill in API_ID, API_HASH, PHONE
havoc check               # validate everything before you connect
havoc login               # one-time interactive login
havoc run
```

Headless or no terminal? `havoc login --request` then `havoc login --code …`
(and `--export-session` to get a `SESSION_STRING`). Two GitHub workflows are
included for that path — see [docs/INSTALL.md](docs/INSTALL.md#running-it-on-github-actions).

Detailed setup, systemd unit and data locations: [docs/INSTALL.md](docs/INSTALL.md).
Every environment variable: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).
Common problems: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## What is in here

```
havoc/
  main.py            CLI: run / login / check / migrate / version
  runtime.py         the object graph every feature receives
  client.py          Telethon session handling and login
  handlers.py        thin router: Telegram events -> normalized events / commands
  command_registry.py declarative commands, dispatcher, permissions, dry-run
  event_engine.py    normalized events, dedupe, priorities, burst detection
  scheduler.py       durable reminders, scheduled messages, periodic jobs
  feature_manager.py feature switches, dependencies, emergency stop
  permissions.py     owner checks, allow/deny lists, quiet hours, dry-run
  localization.py    locale catalogs with strict interpolation
  database.py        SQLite access (parameterized, WAL, transactions)
  migrations.py      versioned schema
  health.py          self-checks, metrics, redacted error log
  plugin_loader.py   optional local plugins
  features/          10 modules, 67 commands, all bilingual
  services/          sender, rate limiter, cache, HTTP, AI + translation providers
  utils/             text, time (Jalali), parsing, security, validation, math
  locales/           en.json + fa.json (724 keys each, verified in CI)
tests/               435 tests: unit, integration through the real dispatcher
docs/                install, configuration, security, troubleshooting, commands
scripts/             check_locales.py, generate_docs.py, create_api_app.py
```

### Design rules the code actually follows

- **One outbound path.** Every message goes through `services/sender.py`, which
  applies the rate limiter, FloodWait handling, HTML sanitation, splitting and
  dry-run.
- **Automation is gated, not sprinkled.** `Permissions.can_act()` answers
  emergency stop → feature switch → quiet hours → allow/deny list. Every
  subscriber asks it before doing anything observable.
- **Two kinds of "off".** A feature switch disables the behaviour; dry-run keeps
  it enabled but diverts output to a preview in your Saved Messages. Both are
  covered by tests.
- **Commands are data.** `Command` objects declare name, aliases, category,
  usage, whether they change state, whether they need confirmation, and which
  subcommands exist. Help, docs and the dry-run gate are generated from that.
- **Failures are visible.** Exceptions become localized replies plus an entry in
  `Health` — nothing is swallowed silently, and nothing leaks a secret
  (`redact_text` masks hashes, tokens, session strings and phone numbers).
- **Localization is enforced.** CI fails if `en` and `fa` diverge, if a
  placeholder is malformed, or if a command lacks a description or usage string.

## Feature status

This is the honest matrix. "Works" means exercised by tests that drive the real
dispatcher with a fake Telegram client — not that it has been used against a
live account from this environment.

### Working, tested

| Area | What works |
| --- | --- |
| Core | `.help`, `.commands`, `.status`, `.ping`, `.diagnostics`, `.features`, `.feature`, `.stop`, `.lang`, `.id`, `.about`, `.config`, `.selfcheck`, `.prefix`, `.dryrun` |
| Profile | `.clock` (per-minute periodic profile-name clock), `.bio` (set / rotation pool / preview / reset / limit), `.preset` create / apply / preview / delete **and schedule** (one-off or recurring), `.profile` show / preview / restore / history, `.counter` |
| Messaging | `.autoreply` (exact / contains / regex, cooldowns, loop protection), `.away`, `.schedule` / `.scheduled`, `.template` (validated placeholders), `.fight` (6 styles, intensity, offline-first, refuses threats, never repeats an insult), `.suggest` |
| Detection | `.watch`, `.trigger` (notify / react / template / translate), `.react`, `.rules`, `.detect` (status + language), mention and reply notifications, edit and delete notifications, burst detection |
| Information | `.search`, `.chats`, `.chatinfo`, `.bookmark`, `.note`, `.media` (info + download), `.export`, `.saved`, `.labels`, `.textstats`, `.calc`, `.time`, `.convert-time`, `.agenda` |
| Scheduling | `.remind` (relative, absolute, Persian, recurring), `.reminders`, `.tasks`, `.quiet-hours`, `.agenda`, missed-job recovery, periodic jobs |
| Language/text | `.digits`, `.normalize`, `.rtl`, `.cleanup`, `.transform`, `.translate` (when a provider is configured), `.summarize` (offline extractive, AI optional) |
| Management | `.chatsettings`, `.alias`, `.quick`, `.backup` (export / import / auto), `.plugins` (load, unload, list) |
| Fun | `.joke`, `.quote`, `.random`, `.coin` — bilingual, offline; `.poem` (10,207-poem corpus, per-poet and per-theme filtering), `.fal` (Hafez card rendered as a PNG), `.roast` (reply-only, safety-filtered) |

### Working with limits you should know about

- **`.translate` needs a provider.** With `TRANSLATE_PROVIDER=none` it explains
  how to configure one instead of pretending to translate.
- **`.summarize` is extractive offline.** It selects sentences; it does not
  paraphrase unless an AI provider is configured.
- **`.search` sees what your account can see.** Telegram does not index history
  for userbots; HAVOC searches the recent history of chats it can read, which
  means a big chat may return partial results fast.
- **Media**: `.media download` refuses files above the configured cap and never
  leaves the data directory.
- **Notifications go to `OWNER_IDS[0]`, or Saved Messages when unset.**

### Not implemented

Being explicit, because the spec's catalogue asks for them:

- **Status rotation (#16)** — bio rotation exists; rotating statuses/emojis is not
  a separate feature.
- **Conversation categorization (#77)** — `.labels` lets *you* label chats; there
  is no automatic classifier.
- **Chat activity summaries (#76)** — `.summarize` works on a replied message or
  recent messages; there is no per-chat digest job.
- **Update checking (#100)** — `havoc version` prints local versions only. HAVOC
  does not phone home and never self-updates.
- **`.search` result summaries (#50)** — results are listed, not summarized.
- **AI providers** — only `openai_compatible` (works with local servers);
  Anthropic/Google SDKs are not wired in.

## Development

```bash
pip install -e ".[dev,media]"        # media extra: Pillow + Persian shaping
python -m pytest -q                  # 657 tests, no network required
python -m ruff check .               # lint (config lives in pyproject.toml)
python scripts/check_locales.py      # en/fa coverage, parity, placeholders
python scripts/generate_docs.py      # regenerate docs/COMMANDS.md
python scripts/generate_docs.py --check   # what CI runs
python scripts/build_corpus.py --check    # poem corpus health
python scripts/build_fal_data.py --check  # divination table health
```

The two `build_*` scripts are how the shipped data files are made. They download
from public-domain sources (Ganjoor's MySQL dump, Project Gutenberg through the
GITenberg mirror, the `fal_hafez` app's SQLite table), cache under `.cache/`, and
write gzipped JSON into `havoc/data/`. The data is committed; the scripts are for
regenerating or extending it, and `--check` verifies a checkout without a network.

The test suite builds a real `Runtime` on a temporary SQLite database and drives
commands through the actual dispatcher with a fake Telethon client. No
credentials, no network, no sleeping on real time.

Writing a plugin: drop a file in `plugins/` that defines `setup(api)` and uses
`api.register_command(...)`, `api.subscribe(...)`, `api.add_strings(...)` and
`api.get_setting/set_setting(...)`. Load, unload and command registration are
covered by tests.

Commands are ungated by default — a plugin author opts in with
`feature="autoreply", requires_feature=True`, which then runs the same
permission checks as built-in automation (emergency stop, feature switch, quiet
hours, chat scope).

## License

MIT.
