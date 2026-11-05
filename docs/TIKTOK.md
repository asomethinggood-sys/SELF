# Yura on TikTok

Yura runs on a TikTok account the same way she runs on Telegram: **a logged-in
session that does what a human can do**, not an API client. On Telegram that is
Telethon/MTProto. On TikTok it is a real Chromium wearing the owner's cookies.

What that buys, and what the TikTok API could not:

| | Session (this) | TikTok API |
|---|---|---|
| Post a video | yes, through the composer | drafts, or public only after an audit |
| Like / unlike | yes | no endpoint exists |
| Follow / unfollow | yes | no endpoint exists |
| Block | yes | no endpoint exists |
| Read and answer DMs | yes | Business Messaging API only: business account, review, no EEA/UK/CH |
| Cost | free | free, but weeks of approval |
| Against TikTok's ToS | **yes** | no |

That last row is the honest one. This is automation of a personal account, and
the account is the collateral. Everything in `havoc/tiktok/limits.py` exists
because of it.

## Setup, once, on your machine

```bash
pip install -e ".[browser]"
python -m playwright install --with-deps chromium

python scripts/tiktok_login.py          # a Chromium window opens
```

The window is TikTok's own login page. You sign in yourself — password, QR, a
passkey, whatever you normally use. **Nothing is typed by the tool**, because
TikTok's login guard is built to refuse a scripted one and a human handshake is
both the honest route and the one that keeps working.

When the `sessionid` cookie appears, the jar is written to
`.auth/tiktok/default.json` (mode 0600) and the script exits.

```bash
python scripts/tiktok_login.py --check   # what did we get?
havoc tt status                          # session, surfaces, today's allowance
```

Commit that jar so the workflow can restore it:

```bash
git add .auth/tiktok/default.json
git commit -m "tiktok: session"
```

The jar is a bearer credential — the same class of secret as
`.auth/session.txt`. Treat it that way: it lives in this repository on this
branch, it is revocable from TikTok's device list, and it is never printed
(`havoc.tiktok.redact` turns values into a length).

## Running it

**Locally, visibly** (the only way to debug a selector):

```bash
havoc tt run --window --minutes 10
```

**In the workflow** (`.github/workflows/tiktok.yml`): dispatch it, or push to
`havoc/tiktok/**`. It restores the jar, runs headless for ~285 minutes, re-saves
the jar every five minutes, commits the refreshed copy, and queues its own
successor — the same handover shape as `auth-and-run.yml`.

A **challenge stops the chain.** If TikTok shows a captcha, the run exits 1, no
successor is queued, and the reason is in `.reports/tiktok-status.json`. Clear
the challenge on a phone, then dispatch again. Retrying into a captcha is the
fastest way to lose an account.

## Commands

In any DM, with the same prefix as Telegram:

```
.like <url>                  .comment <url> <text>
.follow @someone             .unfollow @someone
.block @someone              .post <file> [caption]
.draw <prompt>               .remind 30m water
.status                      .help
.stop                        .yura <anything>
```

Anything that writes is **owner-only**, spends a daily allowance, and is paced.
Anything that is not a command is answered by her — same persona, same memory,
same provider as the Telegram side.

## What is ported, and what is not

| Ported | Not ported, and why |
|---|---|
| Persona, mood, kaomoji, the composed line bank | Telegram cards, panels, inline keyboards — a DM is text |
| Memory, people, lessons (`yura_history`, same table) | Voice notes (`tts`) — the web client has no recorder path this drives |
| AI router, tools, intent | Group admin commands — TikTok has no groups |
| `.remind` (scheduler), `.draw` (imagegen), `.fight` (her voice) | `.privacy`/`.sessions` — TikTok's settings pages, not mine to automate |
| Command vocabulary and prefix | The 150-character comment ceiling: receipts are written short |

Chat ids are derived from the TikTok thread as negative integers, so a TikTok
conversation can never collide with a Telegram chat id in the shared history
table.

## Limits (the numbers that keep the account alive)

Per UTC day, per account:

| action | default |
|---|---|
| like | 120 |
| comment | 20 |
| follow / unfollow | 30 each |
| block | 10 |
| post | 6 |
| dm | 60 |

`HAVOC_TIKTOK_DAILY_<ACTION>` moves any of them; `0` switches one off. The
allowance is charged **before** the action and persisted, so a handover cannot
mint a second one, and a crash still counts. Gaps between writes are jittered —
a fixed interval is itself a fingerprint.

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `no_session` | no jar in the workspace | `scripts/tiktok_login.py`, commit the file |
| `expired` | TikTok signed the jar out | log in again; the old file is kept until a new one works |
| exit 3 | a selector moved | `havoc tt probe` prints what is actually on the page |
| exit 1 + challenge | captcha | clear it by hand, dispatch again |
| `browser engine not installed` | Playwright or its Chromium is missing | the two install commands above |
