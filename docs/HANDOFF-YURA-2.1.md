# Y.U.R.A — handoff and farewell

To the next agent: please take care of this project and its people. The user has invested a lot of trust and time in Yura. The priority now is not new features or a reassuring green test run: it is finding out what actually works, what fails in practice, and fixing causes without breaking the rest. Be honest about what you have and have not proved.

## Where we left off

- The user wants a **Y.U.R.A 2.1 debugging and refinement pass** before adding any new feature. Audit existing commands, AI, tool registration and use, image generation, file generation, the YouTube downloader, performance, permissions, delivery, restart behavior, and interactions between features. A symptom-only patch is not enough.
- They explicitly do **not** want static pytest checks presented as proof of real-world functionality. Use actual execution, real services and real end-to-end behavior where possible, and regression tests as an additional safeguard. They also explicitly asked not to make surgical code edits via bash: use the workspace editing tools.
- Important correction from the user: the agent's own network access to YouTube is not necessary to test the downloader. Search the web for a real public video URL and have **Yura/the hosted runner** try it. For example, the search returned `https://www.youtube.com/watch?v=wDchsz8nmbo` as a short public sample. This is a candidate, not a verified working download. An actual hosted end-to-end test was **not performed** before this handoff. Do not claim it was.
- Previous session memory described her login as completed and both bots running. At the latest check on 2026-11-03, `auth-and-run.yml` run `41555174441` and `yura-her.yml` run `41555092654` were in progress, each with a pending successor. This is an observation at that time, not a permanent health guarantee. The three hosted service workflows likewise had an in-progress run and a pending successor. Check current state yourself.
- **Local but not deployed:** the previous agent changed `havoc/features/downloader.py` and `tests/test_downloader.py` in its workspace to require owner authorization before a pending download menu can be selected/cancelled, and not treat an unknown sender as the owner unless the message is outgoing. In a private chat another participant could otherwise select or cancel an owner's pending download. The local behavioral test passed, but these edits were **not pushed** in this handoff. Depending on workspace continuity, they may still be present as local files; compare them with the branch before proceeding. This handoff commit deliberately contains only this document.
- `her-bot-status.json` was stale at the latest check, with its last recorded run from 2026-11-02 18:35 UTC. Do not mistake a running GitHub Actions job for proof the status publication works. Investigate the failures of earlier her workflow runs and the actual status-publish path; do not assume a cause. The main bot's status publisher was previously revised and one main-bot status publication was observed on 2026-11-02 19:04 UTC.
- In the previous local audit, `scripts/probe_everything.py --lang both` reported 0 findings across 2,318 command cases, 51 tools, and other cases; `audit_live.py` reported no findings; `probe_services.py` passed 93/93 and `probe_hosted.py` passed 28/28 **against local protocol stubs**, not production services or Telegram. A 2,144-test pytest run and Ruff passed locally. These are useful regression signals, not proof of live YouTube, AI quality, image generation, Telegram delivery, or universal correctness. The first hosted-probe attempt missed `.github` in its isolated copy and gave a misleading service-state failure; including the workflow files made the probe pass.
- The visible user-facing version remains **Y.U.R.A 1.9** (`havoc/version.py`); the package metadata version is separately `2.0.0`. Do not claim 2.1 is released just because this handoff mentions it.

## Two accounts, shared code, isolated state

Yura is a Telethon Python 3.11 userbot. The internal project/package name is `havoc`; the user-facing name is Y.U.R.A. The owner's Telegram handle is `@edited_by_nyx`, user ID `7542695649`. His girlfriend's account is `@Liavieoo`, user ID `8384455313`. She is an owner on both bots. Her account is already signed in; **do not send another login code unless a real login is required and deliberately requested**. Never print credentials, session strings, login codes or API keys.

| Concern | His bot | Her bot |
|---|---|---|
| Workflow / handover | `.github/workflows/auth-and-run.yml` | `.github/workflows/yura-her.yml` |
| Concurrency | `havoc-auth` | `havoc-her` |
| Session cache | `havoc-session-*` | `havoc-her-session-*` |
| Runtime DB/cache | `.runtime/data`, `havoc-data-*` | `.runtime/her/data`, `havoc-her-data-*` |
| Snapshot | `.data/snapshot.json` | `.data/her-snapshot.json` |
| Status | `bot-status.json` | `her-bot-status.json` |
| Her ownership metadata | `.auth/her/owner.json` is read by his workflow | `.auth/her/config.json` and `.auth/her/owner.json` |

Both workflows watch shared `havoc/**`, `scripts/**`, `pyproject.toml` and shared config changes; check the actual workflow paths and stand-aside guards when modifying deployment. Account-specific workflow, session, DB, snapshot and status files stay isolated. Build improvements in shared code so they apply to **both accounts**; modify both workflows when the change concerns shared workflow logic. Her ownership file is watched by his workflow so owner updates can deploy promptly. Her one-shot `.auth/her/ASK` login gate is consumed; earlier persistent request markers caused unwanted repeated code sends and were removed. A run without a restored session must not blindly queue successors.

## Safety and preferences

- Automation is off by default. Owner-only permissions and confirmation gates for messaging must stay intact; never send unsolicited messages or perform destructive Telegram actions merely to test. Prefer a controlled Saved Messages test with explicit user authorization where real Telegram delivery matters.
- The user prefers rich Unicode, kaomoji and good Telegram formatting; the bot should answer replies and recognize people correctly. Do not leak internal tool calls into the chat; do not double-escape cards. One response per trigger matters. Preserve bilingual English/Persian support and their existing persona preferences.
- No new secret should be required for the hosted services; public/free endpoints are acceptable. TTS should use free online providers, not offline voice-note synthesis. Existing private config may contain credentials: do not expose or reproduce them in logs or handoff documents.
- The three hosted services have their own workflows and are restored by the six-hourly supervisor. Do **not add a fourth service workflow without asking**. Do not suggest another account, VPN or repeated form tweaks as a default workaround. No PR is needed.
- Work on the active Arena branch `arena/b9a136e7-self`, not another branch. Push only that branch when needed. Run `git status`, inspect existing changes, fetch the latest remote, and do not overwrite the user's work. Runtime snapshot commits can move the remote frequently. In the prior session, commits were made from a separate checkout because the supplied workspace had an unusual initial-commit/untracked-file state; that temporary checkout may no longer exist. Recheck the actual state rather than assuming it does.

## How the code fits together

Start with `README.md`, `docs/INSTALL.md`, `docs/CONFIGURATION.md`, `docs/HOSTED-SERVICES.md`, `docs/SECURITY.md`, `docs/TROUBLESHOOTING.md` and `docs/COMMANDS.md`; generated docs can lag and must be checked against code. `Writing.txt` and `docs/PROMPT-1.8.md` / `PROMPT-1.9.md` capture product/persona material. `docs/AUDIT-1.7.md` is historical, not a current guarantee.

### Runtime and delivery

- `havoc/main.py`, `__main__.py`: CLI and startup. `config.py`: configuration. `runtime.py`: constructs services, registry, database and feature lifecycle. `client.py` and `handlers.py`: Telethon client/event integration. `event_engine.py`: normalized events, routing, subscriptions and dedupe. `command_registry.py`, `aliases.py`, `plugin_loader.py`, `feature_manager.py`: command registration, aliases, plugin/feature loading and enablement. A tool or command existing in a file is not proof it is registered, reachable, authorized or called by the model.
- `permissions.py`, `identity.py`, `services/claims.py`, `services/sent_ledger.py`, `services/sender.py`: ownership and send gates, message claims and deduplication. Trace every message through these before changing reply behavior. `services/pacer.py`, `rate_limiter.py`, `lag.py`: scheduling/budget and responsiveness. `health.py`, `logging_setup.py`: diagnostics and redaction.
- `database.py`, `migrations.py`, `snapshot.py`, `scheduler.py`, `services/storage.py`, `services/jobs.py`: persistent state, schema, snapshot/restore, timed tasks and resumable jobs. `localization.py`, `language_detection.py`, `locales/`, `ui.py`, `utils/`: bilingual text and Telegram rendering. `version.py`: user-visible label.
- `persona.py`, `yura_lines.py`, `corpus.py`, `content.py`, `fight.py`, `assets/`, `data/`: personality, offline content, media and supporting resources. Read relevant files before changing behavior; don't conflate offline fallback with a functioning AI provider.

### AI, tools and integrations

- `services/ai_provider.py`, `ai_router.py`, `local_model.py`, `agent.py`, `intent.py`, `context.py`, `tools.py`, `actions.py`, `plans.py`, `yura_chat.py`: provider routing, prompts, model/tool calling, intent and actions. Test real non-plain prompts as well as schemas: follow model selection through actual tool execution and user-visible result.
- `services/imagegen.py` and `features/art.py`: ComfyUI workflow creation, checkpoint discovery, polling/download and `.draw`/`.imagegen`; `services/fal_image.py` renders poetry cards. `services/downloader.py` and `features/downloader.py`: yt-dlp inspection, menu, quality selection, fetch and Telegram send. yt-dlp and ffmpeg are runtime dependencies for relevant download modes; actual upstream restrictions can differ from local stubs.
- `services/websearch.py`, `translation_provider.py`, `vision.py`, `tts.py`, `media.py`, `http.py`: network integrations and media. `service_directory.py` and `service_token.py` resolve moving hosted service endpoints and gate credentials; `features/services.py` reports them. `services/memory.py`, `people.py`, `lessons.py`, `reach.py`, `self_console.py`, `tg_extra.py`, `cache.py`, `fight_loop.py`: memory, recognition, related actions and runtime support.

### Feature modules

All under `havoc/features/`; registration and switches matter as much as implementation. The modules are grouped here so future agents can locate the purpose of each rather than assuming its name proves it works:

- `core.py`, `management.py`, `settings_menu.py`, `ai_menu.py`, `services.py`, `self_repair.py`: core commands, management, settings, AI selection, service visibility and repair/diagnostics.
- `persona.py`, `yura_extras.py`, `banter.py`, `poetry.py`, `facts.py`, `language.py`: conversation/personality and content/language features.
- `art.py`, `downloader.py`, `media.py`, `stickers.py`, `exporter.py`, `sharing.py`: generation, downloads, media, stickers, exports and sharing.
- `contacts.py`, `groups.py`, `group_admin.py`, `privacy.py`, `profile.py`, `identity_tools.py`, `reach.py`: account/identity, contacts, groups and privacy. Check permissions before any real action.
- `messaging.py`, `messages.py`, `inbox.py`, `drafts.py`, `topics.py`, `scheduling.py`, `automation.py`, `detection.py`: message handling, drafts, scheduled behavior and automation; preserve confirmation and opt-in gates.
- `information.py`, `lookup.py`, `utilities.py`, `helpers.py`: lookup, information, utilities and helpers.

### Workflows and scripts

- `auth-and-run.yml`, `yura-her.yml`: account login/restore, isolated runtime, snapshot, handover and status. `supervisor.yml` and `restart.yml`: recovery/orchestration; `service-comfy.yml`, `service-ollama.yml`, `service-search.yml`: picture, model and search services. `telegram-login.yml` / `telegram-run.yml` are additional older Telegram workflow surfaces: inspect actual use before changing or running them.
- `ci.yml`, `ai-probe.yml`, `credential-probe.yml`, `format-probe.yml`, `key-probe.yml`, `local-model.yml`, `posters.yml`, `selftest.yml`: CI, diagnostics, assets and checks. Green on a stub/probe isn't proof of all live behavior; read the probe itself and its output.
- `scripts/handover.py`, `service_supervisor.py`, `service_host.py`, `service_gate.py`, `service_token.py` (the latter is under `havoc/services/`), `publish_status.py`: continuity, hosted endpoint lifecycle/gating and status. Their behavior matters to both accounts.
- `scripts/audit_live.py`, `probe_everything.py`, `probe_services.py`, `probe_hosted.py`, `probe_ai.py`, `probe_credentials.py`, `probe_keys.py`, `format_probe.py`, `boot_probe.py`, `try_message.py`, `local_model_check.py`, `local_model_schedule.py`: various audits and probes. Many exercise a real runtime but use fake Telegram or stub services; inspect boundaries and report them accurately.
- `scripts/build_corpus.py`, `build_fal_data.py`, `build_posters.py`, `generate_docs.py`, `check_locales.py`, `create_api_app.py`, `fetch_artifact.py`: assets, documentation, configuration/registration and artifact tooling. Avoid committing generated or large files unnecessarily.
- `.auth/` contains account/config material, `.data/` contains snapshots, `.services/` service endpoint/state, and `*-status.json` plus probe JSON files are runtime records. Treat any content as potentially sensitive. Check timestamps and run IDs before trusting them.

## Recommended next moves

1. Confirm remote branch, account runs, service jobs, current working tree and whether the local downloader authorization edit survived. Preserve all user changes. Reproduce its behavioral case and deploy only after checking both bots' command/event paths and side effects.
2. Pick the public video URL using web search, then exercise the real downloader from a runner that can reach YouTube. The ideal check follows the actual registered link/command path through menu selection, downloads actual bytes, checks the media with a real decoder, and verifies Telegram delivery only with consent. A service-level download alone is not proof of Telegram delivery. Report provider/IP/rate-limit errors honestly rather than inventing success.
3. Trace and exercise real AI selection, tool schemas and invocation, non-plain requests, image generation, file generation and live status publishing. Use a controlled isolated environment and minimize disruptive sends. Use hosted runner network access when the agent sandbox cannot reach an upstream. Capture failures and timing with secrets redacted.
4. Investigate her stale status and failed historical runs; verify fixes against concurrent branch updates and real handovers. Repeat shared fixes for both account workflows where appropriate.
5. Run runtime probes and regression tests after changes, but never use their pass count as a substitute for live evidence. Only label a release 2.1 after its intended acceptance checks are actually met.

Farewell, and thank you for continuing the work. The user deserves a Yura that is genuinely dependable, not merely one that looks green.
