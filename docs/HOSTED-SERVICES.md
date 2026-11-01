# Hosted services

Three things the bot can use without a key: a search engine, a picture server, and
a small local model. They do not run inside the bot's job — they run on **their own
runners**, in parallel, each with its own six hours, and they publish where they
are so the bot can find them.

```
service-search.yml   →  .services/search.json   →  .web
service-comfy.yml    →  .services/comfy.json    →  .draw
service-ollama.yml   →  .services/ollama.json   →  the router's last step, and the engine when there are no keys
```

The bot reads those three files. It never needs to be told a URL, and a URL that
changes every six hours is not a broken configuration.

---

## Why a tunnel, and what it costs

A GitHub runner has no inbound network. Nothing outside can reach a port on it. The
only bridge is an outbound connection from the runner to a public relay, which is
what `cloudflared` does: `scripts/service_host.py` starts a quick tunnel, parses the
`https://<random>.trycloudflare.com` it is given, publishes it, and re-opens it if it
dies.

Flows through Cloudflare's edge, ephemeral hostname, per run. If Cloudflare's terms
for quick tunnels are a problem for you, or you want a stable address, use a named
tunnel:

| | quick tunnel | named tunnel |
|---|---|---|
| hostname | random, changes every run | yours, stable |
| setup | none | a Cloudflare account + a tunnel token |
| secrets | — | `CLOUDFLARE_TUNNEL_TOKEN`, variable `CLOUDFLARE_HOSTNAME` |

With both set, `service_host.py` runs `cloudflared tunnel run` with the token
instead of `--url`, and publishes `https://$CLOUDFLARE_HOSTNAME`.

A third option, which involves no tunnel at all: a self-hosted runner on a machine
you control, with `--no-tunnel` (it binds loopback and the bot reaches it directly,
so both must be on the same network).

---

## The gate

A public URL in front of an expensive endpoint is an invitation. So nothing is
exposed directly: `scripts/service_gate.py` sits in front of every upstream and
refuses anything without a token derived from the current run.

```
bot ──► https://<run>.trycloudflare.com ──► service_gate ──► SearXNG / ComfyUI / Ollama
```

* **The token is derived, never stored.** `HMAC-SHA256(secret, "<service>:<run_id>:<started_at>")`
  — see `havoc/services/service_token.py`. Both sides compute it; `.services/*.json`
  contains no secret, because the repository is public. A token from yesterday's run
  is refused by today's gate.
* **`/healthz` needs no token** — that is what the tunnel check and `.services` ask.
* **Per-minute cap** (600 by default) → `429` with `retry_after`.
* **8 MB body cap** → `413`. A ComfyUI prompt is a few KB.
* **`502` with the reason** when the upstream is down, instead of a hang.
* **`Cache-Control: private, max-age=600` on `image/*`**, so a picture that is asked
  for twice is not generated twice.
* **The gate refuses to start without `SERVICE_SHARED_SECRET`.** A gate with no
  secret is an open proxy, so the failure is at startup, not in a log nobody reads.

### The one secret you have to add

`SERVICE_SHARED_SECRET` — any long random string, the same one for all three
services. In GitHub: **Settings → Secrets and variables → Actions → New repository
secret**. Locally:

```bash
python -c "import secrets; print(secrets.token_hex(32))" | gh secret set SERVICE_SHARED_SECRET
```

Once it is there, every service run opens a tunnel and publishes an address.

### Without it, the run is a self-test

A run with no secret does not fail and does not go public. It installs everything,
starts the service, puts the gate in front, and asks it a real question over
loopback with a real token — then prints the transcript and exits:

```
  ok   gate /healthz without a token — 200
  ok   upstream /search?q=ping&format=json directly — 200
  ok   the same path through the gate without a token — 401
  ok   a wrong token is refused — 401
  ok   /search through the gate with the real token — 200 {"results": [...]}
  ok   the answer is JSON with results
self-test: 6/6 checks passed
```

That is a green run with evidence, not a red run with nothing. It also proves the
install path works on a runner — which is the part that breaks.

---

## What each service is

| service | upstream | port | model / image | notes |
|---|---|---|---|---|
| `search` | SearXNG (docker) | 8080 | `searxng/searxng:latest` | `formats: [html, json]` is written into a generated `settings.yml`; limiter off; DDG, Brave, Wikipedia |
| `comfy` | ComfyUI (CPU torch) | 8188 | `sd-turbo.safetensors` | 8 steps normally (30–50 s), 20 with `.draw --quality` (1–3 min) |
| `ollama` | Ollama | 11434 | `qwen2.5:0.5b` | the router's last step; the only engine when there are no keys |

Gates listen on `9001`, `9002`, `9003` and are the only thing the tunnel points at.

`docker` is already on `ubuntu-latest`, so SearXNG costs a pull (~200 MB), not a
build. ComfyUI is cloned and its requirements installed with **CPU wheels forced**
from `download.pytorch.org/whl/cpu`: a CUDA torch build is 2.5 GB of nothing on a
runner with no GPU.

---

## Staying alive

Each service workflow does what the bot's does:

1. **Queues its successor first**, before anything can fail (`scripts/handover.py`).
2. Asks `handover.py` once a minute whether that successor is still there.
3. Runs under its own concurrency group with `cancel-in-progress: false`, so a new
   run waits for the old one instead of killing it mid-flight.
4. `timeout-minutes: 355` — five minutes under the six-hour ceiling, so the handover
   happens before GitHub pulls the rug.

`handover.py` asks the API for the runs of **this workflow file**, not the whole
repository. That distinction is not academic: `/actions/runs` returns the 50 most
recent runs repo-wide, and with four long-lived workflows on one branch a service
drops out of that window within hours — which reads as "nothing is alive" and queues
a second copy of something that is running.

The last-resort path is `scripts/service_supervisor.py`, run by the bot's workflow
at start-up and by `supervisor.yml` every five minutes. It reads
`.services/<name>.json` and dispatches any service that is missing, expired, or
within an hour of its ceiling — but only if no run of that workflow is queued,
running, or recent (45 minutes). A service that is failing must not turn into a
dispatch every five minutes: that burns the queue and hides the reason.

---

## Reading the states

`.services` (owner only, aliases `.service`, `.hosted`) answers "where are they":

| state | what it means | what happens next |
|---|---|---|
| `up` | a published address, fresh, answering | nothing |
| `between runs` | the last run ended and the next has not published yet (a few minutes each six hours) | it comes back on its own |
| `expired` | the address is past its six hours and nothing replaced it | the supervisor dispatches a run |
| `never` | no address has ever been published — the workflow has not run | the supervisor dispatches a run |
| `not configured` | no file and no workflow (a fork, or a local checkout) | set `SEARXNG_URL` / `COMFY_URL` / `OLLAMA_HOST` by hand |
| `broken` | three failures in a row; skipped for a minute | the next resolve tries again |

The same words appear in `.web` and `.draw` when the service is not usable, with the
age of the last publish — "between runs, its last address went quiet 40m ago" is a
different sentence from "not configured", and it has a different fix (none).

---

## Local, without any of this

The environment variables still work, and take precedence:

```bash
SEARXNG_URL=http://127.0.0.1:8080 python -m havoc     # your own SearXNG
COMFY_URL=http://127.0.0.1:8188  python -m havoc      # your own ComfyUI
OLLAMA_HOST=http://127.0.0.1:11434 python -m havoc    # your own Ollama
```

Nothing is gated in that case (`gated: false`): the gate is for what is public.

---

## Honest limits

* **A CPU runner is not a GPU.** 8 steps is 30–50 s; 20 steps is 1–3 minutes. SDXL
  is not viable, and a batch of four is four times the wait.
* **Datacenter IPs get captcha'd.** DuckDuckGo in particular refuses quickly.
  SearXNG reports which engine refused (`unresponsive_engines`); `.web` names it and
  falls back to an in-process engine (`ddgs`) before it says "nothing to show".
* **Quick tunnels are ephemeral** and their hostnames are random. A new address every
  six hours is normal, not a bug.
* **A dead service is not a silent fallback.** The bot never quietly reaches for a
  cloud API when a hosted one is down: it names the state.
* **The service files are committed** — one small JSON per service, one commit per
  start, `[skip ci]`. They are the only thing those workflows write.
