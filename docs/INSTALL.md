# Installation

> The bot introduces itself as **Yura / یورا**. The package, the CLI
> (`python -m havoc`) and every environment variable keep the original `havoc`
> name, so existing deployments and scripts keep working unchanged.

## Requirements

- Python **3.11 or newer**
- A Telegram account whose API credentials you control
  ([my.telegram.org](https://my.telegram.org) → *API development tools*)
- No paid services. Everything core runs offline; AI and machine translation
  are optional add-ons.

## Steps

```bash
git clone https://github.com/asomethinggood-sys/SELF.git
cd SELF

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .                   # add ".[dev]" to run the test suite
cp .env.example .env               # then edit .env
```

Fill in `.env`:

```ini
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
PHONE=+491701234567
TIMEZONE=Europe/Berlin
DEFAULT_LANGUAGE=en
```

`TIMEZONE` matters: reminders, quiet hours and the profile clock all use it.

## API credentials vs. the account that runs the bot

`API_ID` and `API_HASH` identify the **application** you register at
https://my.telegram.org — not the person using it. `PHONE` identifies the
**account** HAVOC signs in as. They are independent:

- Any Telegram account you control can register the application.
- The code Telegram sends always goes to the account named in `PHONE`.
- The account that registered the app is never logged in by HAVOC and gains no
  access to the bot's chats.

So `PHONE` must be the number you want the userbot to *be*, with the leading
`+` and country code, even when somebody else's account created the app.

Two practical notes: an app registered by a brand-new account is scrutinised
harder by Telegram, and if that account is banned the `api_id` goes with it.

`my.telegram.org` also fails *silently* on invalid form values, and each silent
failure counts against the per-number daily attempt limit. Use a short name of
5+ lowercase characters, a non-empty URL and a plain ASCII title, and submit in
one go — the form session expires after a couple of minutes. If the limit is
already hit, every retry restarts the 24-hour window; wait a full day and try
once. Messaging [@SpamBot](https://t.me/SpamBot) in Telegram tells you whether
the account itself is limited, which no form can work around.

### When my.telegram.org refuses to create the app

The form answers a refused submission with a bare `ERROR` page and no reason.
There are three causes, and they need opposite responses:

| Symptom | Cause | What to do |
| --- | --- | --- |
| `ERROR` on submit | the country of your IP does not match the country of the phone number, or a VPN/datacenter exit is in use | use the mobile network of the number's own country, VPN off, submit exactly once |
| `ERROR` but the app exists after a reload | the submission actually succeeded | open https://my.telegram.org/apps and read the values |
| "Too many attempts" | per-number daily limit; every retry restarts the 24-hour window | stop, wait a full day, try once |

`scripts/create_api_app.py` performs the same request the website performs and
tells you which of the three you hit instead of leaving you with `ERROR`:

```bash
# 1. log in at https://my.telegram.org and open /apps
# 2. copy the 'stel_token' cookie value from your browser's developer tools
export TELEGRAM_STEL_TOKEN='…'
python scripts/create_api_app.py --short-name havocbot7k
```

It exits immediately on a rate-limit instead of retrying, prints an existing
application rather than creating a second one, and validates the short name
(5+ lowercase alphanumerics — a shorter or taken name is one of the silent
failures). The cookie is short-lived and scoped to that website; the script
never logs it or writes it to disk.

Matching the *number's* country matters more than anything else here, and a
second account on the same network will fail identically — the account is not
the variable, the IP is.

## First login

```bash
havoc login
```

HAVOC asks for the Telegram login code (and your 2FA password if you have one)
and writes the session to `SESSION_PATH` (default `data/havoc.session`). That
file is a credential — it is git-ignored, and it must never be shared.

For a headless server you can instead paste an existing Telethon
`StringSession` into `SESSION_STRING`.

### Login without a terminal (two steps)

If you cannot type into the machine that will run the bot — a CI runner, a
container, a friend's VPS — the login splits in two:

```bash
havoc login --request                 # sends the code, saves the challenge
havoc login --code 12345              # finishes; add --password for 2FA
havoc login --code 12345 --export-session session.txt   # also write SESSION_STRING
```

`--request` stores the pending challenge in `data/login-state.json` (mode 600,
deleted after a successful login). `SESSION_STRING` then goes into your .env or
your platform's secret store. Treat it exactly like a password: it is the
account.

## Running it on GitHub Actions

Useful when you have no terminal on the machine that should host the bot, or no
machine at all. Both workflows are manual: **Actions** tab → pick the workflow →
*Run workflow*.

| Workflow | What it does |
| --- | --- |
| **Telegram login** | `phase=request` asks Telegram for a code; `phase=verify` with the code signs in and uploads the session as the `telegram-session` artifact |
| **Telegram bot** | runs `havoc run` for up to 5h20m, restoring and saving `data/` through the actions cache |

Every value can be typed into the **Run workflow** form instead of being stored
as a secret; an input always overrides the matching secret. Trying a candidate
`api_id` therefore costs one dispatch, not a commit:

| Input | Falls back to |
| --- | --- |
| `api_id` | `API_ID` secret |
| `api_hash` | `API_HASH` secret |
| `phone` | `PHONE` secret |
| `code`, `password` | `TELEGRAM_2FA_PASSWORD` secret |

Repository secrets (Settings → Secrets and variables → Actions), optional when
you fill the form:

- `API_ID`, `API_HASH`, `PHONE` — the first two from https://my.telegram.org
- `TELEGRAM_2FA_PASSWORD` — only if the account has two-factor enabled
- `SESSION_STRING` — copied from the login artifact
- `OWNER_IDS` — **optional**: without it, the logged-in account is the owner

The login code goes to your Telegram app, not to the runner, which is why the
login needs two dispatches: the code is bound to the auth key that requested it,
so phase two restores the session file along with the pending challenge.

**A login code is short-lived and single-use; a session string is not.** Anyone
who reads the `SESSION_STRING` — or a public run log that contained a code — can
act as you until you revoke that session in Telegram → Settings → Devices →
*Terminate all other sessions*. Do the login in a private repository, delete the
artifacts and runs afterwards, and revoke the session when you are done testing.

Caveats of hosting a userbot on CI: jobs are killed at six hours, the runner is
discarded (the database lives in the actions cache, so work due in the final
minutes of a run can be lost), and Telegram sometimes challenges logins from
datacenter IPs. A €4 VPS or a Raspberry Pi has none of these problems and is the
recommended host.

## Iterating on features while the bot runs

The session is kept in the actions cache, so **a code change never requires
logging in again**:

```bash
# edit something under havoc/
python -m pytest -q            # 450+ tests, no network
git push                       # this is the deploy
```

What happens: the push queues a new run, which restores the cached session,
skips both login steps and starts the new code. The run that is currently live
notices the newer commit within a minute, stops itself, and lets the queued run
take over — so the two never fight over the same session, and the handover needs
no manual step or cancellation permission.

Only `havoc/**`, `pyproject.toml`, `.auth/config.json` and the workflow itself
trigger a deploy. Pushing docs or tests does not restart a running bot.

Every deploy announces its short commit SHA in its own summary, so you can check
which code is live:

| Where | What |
| --- | --- |
| Actions → *Auth and run* → latest run → Summary | the commit being deployed |
| the run's "Run the userbot" step | `Handing over to <sha>` when it steps aside |

If you ever want to *keep* a session somewhere permanent (a VPS), the string in
the cache is all you need; docs/CONFIGURATION.md explains `SESSION_STRING`.

## Staying alive

A job on a runner can only last six hours, so "24/7" here means *a chain of runs
with no gap in the middle*, not one immortal process. Three things make that
chain hold:

1. **Each run queues its own successor in its first step**
   (`scripts/handover.py` → `workflow_dispatch`). Whoever ends the run — a
   normal finish, the six-hour ceiling, a crash, a cancellation by hand — the
   next run is already waiting and starts within seconds. The dispatch is the
   documented exception to "the token's events do not trigger workflows", and
   this repository has measured it: the selftest workflow dispatches a run and
   finds it, and the result is committed to `.handover-selftest.json`.
2. **The queue is re-asserted every minute.** If the queued run is cancelled, the
   running bot notices the empty queue and queues another. A push *queues*
   behind the running bot (it does not cancel it any more), and the bot stands
   down only once it has confirmed a replacement exists.
3. **A wedged run is reaped.** The concurrency group admits one running and one
   pending run, and GitHub cancels the *older pending* one whenever a third
   arrives — so a run stuck past the six-hour ceiling in front can keep a fresh
   push from ever starting. `scripts/handover.py --reap` cancels anything still
   "in progress" after 320 minutes before it refills the queue, and the
   **Restart** workflow stops everything and starts clean in one click.
4. **The supervisor is the last resort.** If nothing is running *and* nothing is
   queued — a cancellation during the first few seconds of a run, for example —
   `.github/workflows/supervisor.yml` dispatches one. GitHub only fires
   `schedule` for workflows on the **default branch**, so this one is only a
   working safety net once it lives on `main`; from any other branch it runs
   when you press *Run workflow*, not on a timer.

Two honest limits. There is a boot gap of about a minute while the next runner
installs the package — that gap is the runner, not the bot's design, and a VPS
does not have it. And a bot that is not running cannot hear you: nothing can
start it from inside Telegram, which is exactly why the queue is kept full
*before* anything goes wrong. "Saying her name and she comes back" holds because
the replacement is already queued when you say it, not because a message can
wake a dead process.

One trap worth knowing, because it cost a six-hour outage: **`GITHUB_TOKEN` is a
secret, not one of the automatic `GITHUB_*` variables.** A step that talks to the
API without it in its own `env` sends an empty `Authorization` header, gets 401,
and the stand-down guard then refuses to stand down — on purpose, since an
unconfirmed answer must never leave the bot down. The test suite now fails if a
step that calls the API forgets the token.

## What survives a restart, and how it was checked

Two mechanisms keep your data, and they fail independently:

| Store | Where | Best at |
| --- | --- | --- |
| SQLite database | actions cache `havoc-data-*` | fast restarts within the same branch |
| JSON snapshot | `.data/snapshot.json` in the repository | anything the cache misses |

The cache is a cache: a miss looks exactly like a first boot, and a run that is
replaced before its save step never writes one at all. So the bot also commits a
snapshot of every table it stores — reminders, notes, bookmarks, templates,
schedules, watches, triggers, features, settings — through the GitHub contents
API every five minutes, and once more on shutdown. Committed data is versioned,
reviewable in the repository history, and cannot be evicted.

On boot, if a table that should have data is empty, the snapshot is imported
before any handler runs. The import inserts or updates by primary key and never
deletes, so it fills gaps but cannot wipe anything.

Evidence is left where you can look at it:

| File | Means |
| --- | --- |
| `data-status.json` | what the database contained at the start of each run |
| `.data/snapshot.json` | everything the bot had stored, with a timestamp |
| `.data/restore-report.json` | a restore actually happened: run, source, row counts |

`python -m havoc data` prints the same information locally: path, schema
version, populated tables, `integrity_check`, and `--boot-probe` counts boots.

**Privacy note.** The snapshot is committed to the same branch, so on a public
repository those rows are public. It deliberately holds no session string, phone
number or API credentials, because those are not in the tables it exports. To
turn snapshots off, remove `HAVOC_SNAPSHOT_TOKEN` from the *Run the userbot*
step; the actions cache keeps working as a best-effort store either way.

## Optional: rendering the fal card

`.fal` sends a picture. Drawing one needs three libraries:

```bash
pip install -e ".[media]"      # pillow, arabic-reshaper, python-bidi
```

Without them the bot still runs — `.fal` notices at runtime, says so in one
quiet line, and sends the reading as text instead of failing. The fonts and the
divination table ship inside the package, so there is nothing else to install.

## Her voice

`.yura speak <text>` needs one of two things, and the first needs nothing:

* **No key.** `pip install -e ".[voice]"` installs `edge-tts`, which speaks
  through Microsoft's read-aloud engine. The runtime workflow already installs
  this extra. `.yura voice` says `edge · ready` when it is there.
* **A free Groq key.** <https://console.groq.com/keys> — free tier, no card,
  model `playai-tts`, OpenAI-compatible endpoint. Send `.yura voice key <key>`
  (the message is deleted straight away, the key is stored base64), then
  `.yura voice groq`. `.yura voice off` goes back to the keyless voice.

If neither is available, `.yura say` stays a conversation instead of failing:
a word silently changing meaning is worse than a feature being absent.
