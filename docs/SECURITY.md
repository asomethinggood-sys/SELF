# Security

## Threat model

A userbot runs **as you**. Anyone who controls the session file or the API hash
controls your Telegram account. Everything below follows from that.

## Rules HAVOC follows

1. **Secrets never leave the process.** `redact_text()` masks API hashes, bot
   tokens, session strings, phone numbers and `key=value` secrets on every log
   line, every error report and every diagnostics listing. `.config` shows
   secrets as *set (hidden)*, never their values.
2. **Nothing is committed.** `.env`, `*.session`, `data/`, `*.db`, `backups/` and
   `logs/` are git-ignored. `git status` after a run shows no credentials.
3. **Owner-only by default.** Every command is `owner_only` unless it is
   deliberately public. Commands from other accounts are refused, and the
   refusal is all they see — no command output leaks.
4. **Automation is opt-in.** All 13 automation features default to off, so a
   fresh install never sends anything without you enabling it explicitly.
5. **Automation can always be stopped.** `.stop` freezes commands, automation and
   the scheduler; `.dryrun on` previews without acting; per-chat denylists and
   quiet hours narrow the blast radius further.
6. **No spam, no mass messaging, no surveillance.** HAVOC does not implement
   broadcasting to lists, scraping member lists, or forward-everything rules.
   Fight Mode only ever answers the message you reply to, refuses threats, and
   never repeats an insult.
7. **User input is never executed.** Math goes through an AST evaluator with a
   fixed function table; SQL uses parameters only; templates are validated at
   creation time and rendered with a fixed placeholder set; regexes with nested
   quantifiers (a DoS shape) are rejected.
8. **File paths are contained.** Downloads, backups and imports resolve through
   `ensure_within()`, so a crafted filename cannot escape `data/`.
9. **Imports are validated.** A backup must be a HAVOC file, under 5 MB, and may
   only contain known tables and known columns; unknown keys are dropped rather
   than executed. Plugins are size-limited, scanned for a forbidden import list,
   and loaded from the plugin directory only.
10. **Rate limits exist.** Outbound sends pass through a token-bucket limiter with
    FloodWait awareness, so a runaway loop trips a limit instead of flooding a
    chat.

## Telegram-side hardening

- Use a session file that lives outside any synced folder.
- Enable two-step verification on the account.
- Set `OWNER_IDS` if you want a second account to be able to inspect things, and
  leave it unset otherwise.
- Review `.diagnostics` and `logs/` after enabling automation; every failure is
  recorded there.

## If something leaks

1. Revoke the session: Telegram → *Settings → Devices → terminate session*.
2. Delete `data/havoc.session`.
3. Rotate the API hash at my.telegram.org if it was exposed.
4. Run `havoc check` and inspect `.config` for unexpected overrides.
