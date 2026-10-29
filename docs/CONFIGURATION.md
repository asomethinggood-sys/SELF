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
| `OWNER_IDS` | your own id | Comma-separated ids allowed to run commands |
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
| `AI_PROVIDER` | `none`, `openai_compatible` | AI-enhanced summaries and comebacks |
| `AI_BASE_URL` | e.g. `https://api.openai.com/v1` | Any OpenAI-compatible endpoint, local models included |
| `AI_MODEL` | e.g. `gpt-4o-mini` | Model name |
| `AI_API_KEY` | — | Provider key (never logged, never shown by `.config`) |
| `TRANSLATE_PROVIDER` | `none`, `mymemory`, `libretranslate` | `.translate` and auto-translation |
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
