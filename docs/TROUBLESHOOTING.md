# Troubleshooting

Start with `havoc check` — it validates config, database, locales, providers and
the installed Telethon version, and every failure it prints names the field to
fix.

## The bot starts but every command is ignored

- The prefix may not be what you think. `.prefix` (with the *current* prefix)
  shows it; `havoc check` prints the configured one.
- Another device may be running an old session. Only the account itself can see
  its outgoing messages, but a second instance can cause duplicate replies —
  check `.diagnostics`.
- If you are writing from a second account, it is not the owner. Add its id to
  `OWNER_IDS`.

## "This command is restricted to the account owner"

That is working as designed. Owner commands only run for the logged-in account
or ids listed in `OWNER_IDS`.

## Reminders never fire

- `TIMEZONE` is wrong → the reminder is scheduled for a different moment. Check
  with `.time`.
- The scheduler is not running → `.tasks` shows pending work; `.diagnostics`
  shows tick count and last error.
- The emergency stop is on → `.stop status`.
- The process was stopped during the reminder. `MISSED_JOB_POLICY` decides what
  happens: `run_once` fires late, `skip` drops it, `ask` notifies you.

## Automation does nothing

1. `.features` — is the feature on? They all default to **off**.
2. `.rules` — are the rules enabled and do they match?
3. `.chatsettings show` — is `automation.denied` set for this chat?
4. `.quiet-hours status` — are you inside the quiet window?
5. `.dryrun` — is preview mode on? Previews go to your Saved Messages.
6. Emergency stop: `.stop status`.

## "Translation is not configured"

Only happens when she has no AI key at all: translation runs on the same model
she talks with (Gemma 4 31B), so a working bot can already translate. Add
`AI_API_KEY` or put keys in `.auth/ai-keys.json`, or set
`TRANSLATE_PROVIDER=mymemory` (no key needed), then restart. When translation is
unavailable, `.translate` and `auto_translate` print this hint instead of failing
silently.

## Telegram asks for a code on every start

The session file is not being written where you expect. Check `SESSION_PATH`,
directory permissions, and whether `SESSION_STRING` is set (it wins and is
memory-only).

## FloodWait errors

Telegram asked HAVOC to slow down. The limiter honours the wait and retries; if
you see repeats, lower `OUTBOUND_RATE_LIMIT` and reduce automation.

## Locale or formatting problems

```bash
python scripts/check_locales.py      # missing keys, en/fa parity, bad placeholders
python scripts/generate_docs.py      # regenerate docs/COMMANDS.md
```

## Resetting

- Keep data, reset settings: delete rows from the `settings` table, or
  `rm data/havoc.db` to start completely fresh (this deletes reminders, notes
  and bookmarks too).
- Re-authenticate: delete `data/havoc.session` and run `havoc login` again.
