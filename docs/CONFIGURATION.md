# Configuration

Configuration comes from environment variables. A local `.env` file is read as a
fallback, and **real environment variables always win** — so you can keep
secrets in the environment and non-secrets in `.env`.

`havoc check` prints every issue it finds and never crashes on a bad value.

## Required

| Variable | Purpose |
| --- | --- |
| `API_ID` | Telegram API id (integer) |
| `API_HASH` | Telegram API hash (32 hex characters) |
| `PHONE` | International phone number, used only for the first login |

## Storage

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `data` | Base directory for database, session, exports |
| `DATABASE_PATH` | `data/havoc.db` | SQLite file |
| `SESSION_PATH` | `data/havoc` | Telethon session file (without extension) |
| `SESSION_STRING` | — | Headless alternative to the session file |
| `LOG_DIR` | `logs` | Log directory |
| `BACKUP_DIR` | `backups` | Backup directory |
| `BACKUP_RETENTION` | `10` | How many backups are kept |
| `PLUGIN_DIR` | `plugins` | Plugin directory |

## Behaviour

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMMAND_PREFIX` | `.` | Command prefix (changeable at runtime with `.prefix`) |
| `DEFAULT_LANGUAGE` | `en` | `en` or `fa` |
| `TIMEZONE` | `UTC` | IANA zone for scheduling and display |
| `OWNER_IDS` | `7542695649` | Comma-separated ids allowed to run commands. The default is the owner's numeric id, so ownership never depends on a username lookup |
| `AUTOMATION_ALLOWLIST` | — | Chats where automation may run |
| `AUTOMATION_DENYLIST` | — | Chats where automation never runs (wins over allowlist) |
| `OUTBOUND_RATE_LIMIT` | `20` | Messages per minute cap |
| `MISSED_JOB_POLICY` | `run_once` | `run_once`, `skip` or `ask` after downtime |
| `DRY_RUN` | `false` | Start with state-changing commands in preview mode |
| `DEBUG` | `false` | Verbose logging |

## Optional providers

Nothing here is required. Leave a provider empty and its feature stays disabled
with an explanatory message instead of an error.

| Variable | Values | Purpose |
| --- | --- | --- |
| `AI_PROVIDER` | `none`, `openai_compatible` | Yura's conversation, summaries and comebacks |
| `AI_BASE_URL` | e.g. `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint, local models included |
| `AI_MODEL` | `auto`, a slug, or a comma-separated chain | See below |
| `AI_API_KEY` | — | Provider key (never logged, never shown by `.config`) |
| `AI_TIMEOUT` | `30` | Seconds before a request is abandoned |

`AI_MODEL` accepts a **fallback chain** — `a:free,b:free` — and tries each in
order, because free tiers add and retire slugs without notice. `auto` is better
still: the bot asks the gateway's `/models` endpoint which free models exist
right now, ranks them by a small preference list (Llama 3.3 70B, Qwen3-Next 80B,
Gemma 4, …), and falls back through the top three. If every configured slug has
been retired, one discovery call happens automatically and the turn is retried
once.

The key can arrive three ways, in order of preference:

1. `.yura key <key>` in Telegram — stored in the database, message deleted, never
   echoed. Survives restarts and deploys (the database is snapshotted). It is
   then **probed once**: either the model's answer or the failure kind is
   reported back, and the same failure is repeated by `.yura key` and shown on
   `.yura status` (with the offline-fallback count) until a request succeeds.
2. the `OPENROUTER_API_KEY` repository secret, which the deploy workflow exports
   as `AI_API_KEY`;
3. `AI_API_KEY` in the environment, or `ai.api_key` in `.auth/config.json` for a
   fork without Actions secrets.

A committed key is refused by GitHub push protection, which is why (1) exists.

Verify with a real round-trip instead of trusting the config:

```bash
python -m havoc ai "say hi in your own voice"
```

It prints which model answered, or the failure kind (`unauthorized`, `network`,
`rate_limited`). The deploy workflow runs the same command and puts the reply in
the run summary; `.github/workflows/ai-probe.yml` runs it without Telegram and
commits the result to `.ai-probe.json`, which is the honest answer to "is the AI
working".

Two things that are easy to misread as a bad key:

- **`403` with an HTML body** is the CDN in front of the gateway refusing the
  `User-Agent`, not the provider refusing the key. Any `User-Agent` containing
  `HAVOC` is refused; the client calls itself `Yura/2.0`.
- **An empty reply at 200** is a model that spent the whole token budget on
  hidden reasoning, or an endpoint that is not a chat model (vision, embeddings,
  safety). The provider retries the same slug with a larger budget, then walks to
  the next candidate, and discovery skips slugs that cannot chat.

A free OpenRouter key allows **50 model requests a day**; past that every call
returns `429` with `free-models-per-day` until the UTC reset. The probe records
that as a quota fact and does not fail the build for it.
| `OWNER_USERNAMES` | `edited_by_nyx` | Owners named by handle. Resolved to ids once at login and stored in the database; the account itself is always an owner too |
| `TRANSLATE_PROVIDER` | `ai` (default), `mymemory`, `libretranslate`, `none` | `.translate` and auto-translation; `ai` asks Gemma 4 31B through the same keys she talks with |
| `TRANSLATE_URL` | — | Self-hosted LibreTranslate endpoint |
| `TRANSLATE_API_KEY` | — | Optional key for the endpoint above |

## Per-chat and per-user settings

Runtime settings live in the database and are changed with commands, not env
vars:

- `.lang chat fa` — language for this chat
- `.chatsettings set automation.denied true` — never automate in this chat
- `.quiet-hours on 23:00 07:00` — pause automation inside a window
- `.dryrun on` — preview instead of doing
- `.stop` / `.stop resume` — emergency stop for everything

## Telegram connection proxy

In networks where Telegram's MTProto protocol is blocked (Iran, for example),
the bot cannot reach Telegram even though DNS and HTTP work. Set the `PROXY_*`
variables to send the Telegram connection through a proxy:

```ini
PROXY_TYPE=socks5          # socks5 | socks4 | http | mtproxy
PROXY_HOST=127.0.0.1
PROXY_PORT=1080
PROXY_USERNAME=            # optional, SOCKS5/HTTP only
PROXY_PASSWORD=            # optional, SOCKS5/HTTP only
PROXY_SECRET=              # mtproxy only
```

| Type | Use when |
| --- | --- |
| `socks5` | you have a SOCKS5 proxy (the usual choice, supports a username/password) |
| `mtproxy` | you have a Telegram MTProto proxy; paste the secret it printed |
| `http` / `socks4` | you only have that |

Notes:

- Only the Telegram connection is proxied. `AI_BASE_URL` and LibreTranslate are
  called directly, so a proxy for those is a separate concern.
- The proxy is *not* relevant when the bot runs on a host with unrestricted
  access (a VPS in the EU, or a GitHub runner).
- `havoc check` validates the values: a bad host, port, missing mtproxy secret
  or unknown type is reported before the client is built.
- A proxy is not an app `api_id`: being able to reach Telegram does not let you
  create an application at my.telegram.org. That check is about the country of
  your IP versus your phone number — see docs/INSTALL.md.
