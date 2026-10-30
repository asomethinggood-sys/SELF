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

## Her hands (the action layer)

Yura can act on her own now: she may decide a message needs a lookup, a
translation, a stored fact or a reminder. Everything she can do is a tool with a
declared scope, and every attempt goes through one executor
(`havoc/services/actions.py`) that checks the policy *before* the tool runs and
writes the attempt to `yura_actions` before it either.

| Scope | Policy |
| --- | --- |
| `read` | free — looking at something the account can already see |
| `write`, `send` | free by default — the owner asked for small things to just happen; set `actions.auto=0` to bring back confirm-once-per-tool |
| `delete` | confirmed **every time**, never remembered |
| `money`, `identity` | **refused** — no confirm, no setting, no exception |

Budgets are enforced from the audit table, so a restart is not a way around them:
four tool calls per turn, twenty per hour, and the same tool with the same
arguments twice in a turn stops the loop.

The confirmation is typed (`.yura yes` / `.yura no`, two-minute window), not a
button: a Telegram *user* account cannot attach an inline keyboard — `reply_markup`
is a bot-only feature.

There is no `send_message` tool. She already speaks, and a tool that can address
an arbitrary chat is a spam cannon with a personality.

## Who is allowed to touch her

Ownership is a short list and it is the whole gate:

* `OWNER_USERNAMES` (default `edited_by_nyx`) — handles, resolved to ids once at
  login via `get_entity`, then stored in `owner.resolved` so no message ever waits
  on a network call to decide who is speaking;
* `OWNER_IDS` — ids, for anyone who would rather not depend on a lookup;
* the logged-in account — always, so a failed lookup cannot lock the owner out.

Only an owner can run a command, change a setting, change *who* the owner is
(`.owner add|remove`), or make her use a hand. A guest may talk to her — that is
the product — and gets her voice, the room's manners and a tone of their own. A
guest's turn never even carries the tool catalogue, so there is nothing to talk
her into.

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
