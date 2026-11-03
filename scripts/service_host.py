#!/usr/bin/env python3
"""Host one service on a GitHub runner and keep it alive for six hours.

GitHub's hosted runners are outbound-only and disposable. That is fine for a bot
and awkward for a service: there is no address to call, and nothing survives the
job. This script is the whole answer, in one place, for all three services:

    install and start the upstream  ->  127.0.0.1:<port>
    start the token gate in front   ->  127.0.0.1:<gate>
    start a tunnel to the gate      ->  https://<random>.trycloudflare.com
    publish .services/<name>.json   ->  once, and again only if the URL changes
    supervise                       ->  until the deadline, restarting what died

Nothing here is service-specific except ``start_upstream``; the gate, the tunnel,
the publishing and the supervision are identical, which is why there is one file
instead of three.

    python scripts/service_host.py --service search --deadline-minutes 340
    python scripts/service_host.py --service comfy --no-tunnel      # self-hosted
    python scripts/service_host.py --service ollama --gate-port 9002

What it deliberately does not do:
  * no heartbeat commits. The published file changes when the URL or the run
    changes, and never on a timer — a commit a minute would be 2 880 a day;
  * no secrets in the published file. The token is derived on both sides from
    ``SERVICE_SHARED_SECRET`` and never written anywhere;
  * no silent success. If the tunnel cannot open, the job says so and keeps the
    local service running so a self-hosted runner still works.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from havoc.services.service_token import (  # noqa: E402 - path set up above
    is_public,
    secret_for,
    token_for,
)

SERVICES_DIR = ROOT / ".services"

#: Ports chosen once and never moved: `.services/<name>.json` publishes them and
#: the bot's fallbacks know them for the `--no-tunnel` case.
PORTS = {"search": 8080, "comfy": 8188, "ollama": 11434}
GATE_PORTS = {"search": 9001, "comfy": 9002, "ollama": 9003}

#: Models. One small model per service, because a runner gives us four cores.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or "qwen2.5:0.5b"
#: Pinned on purpose: master is a moving target and a service that worked
#: yesterday should still be the same program today.
COMFY_TAG = os.environ.get("COMFY_TAG") or "v0.43.2"
#: Modules ComfyUI imports without listing: `app/database/db.py` wants `filelock`
#: and nothing in requirements.txt says so (measured on v0.43.2).
EXTRA_MODULES = ("filelock",)
COMFY_CHECKPOINT = os.environ.get("COMFY_CKPT") or "sd-turbo.safetensors"


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[host {stamp}] {message}", flush=True)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _capture(command: list[str], *, timeout: float = 120.0) -> str | None:
    """stdout of a short command, or ``None`` when it fails."""
    try:
        done = subprocess.run(  # noqa: S603 - our own command
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return (done.stdout or "").strip()


def _run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> int:
    """Run a command to completion, echoing its output to the job log."""
    log("$ " + " ".join(command))
    try:
        return subprocess.run(command, env=env, cwd=str(cwd) if cwd else None, timeout=900, check=False).returncode
    except FileNotFoundError:
        log(f"not installed: {command[0]}")
        return 127
    except subprocess.TimeoutExpired:
        log(f"command timed out: {command[0]}")
        return 124


def _port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _spawn(command: list[str], *, log_path: Path, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.Popen:
    """Start a long-lived process with its output in a file we can read back."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    handle.write(f"\n=== {' '.join(command)} ===\n".encode())
    handle.flush()
    log("& " + " ".join(command) + f"  (log: {log_path.name})")
    return subprocess.Popen(  # noqa: S603 - our own commands, our own arguments
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _http(url: str, *, timeout: float = 5.0, token: str | None = None) -> tuple[int, bytes]:
    """One GET. Never raises: a dead service is a status code, not a traceback."""
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback or our own tunnel
            return response.status, response.read(4096)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, str(getattr(exc, "reason", exc)).encode()[:200]


def tail(path: Path, lines: int = 6) -> str:
    try:
        body = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(line.strip() for line in body[-lines:])[:600]


# ---------------------------------------------------------------------------
# Upstreams
# ---------------------------------------------------------------------------
def start_search(port: int, workdir: Path) -> subprocess.Popen | None:
    """SearXNG in docker, with the JSON format enabled.

    The image ships with ``formats: [html]`` only, and a JSON request answers
    ``403`` — the single most common way this integration "just does not work".
    The settings file is ours, mounted in, so the format is on by design.
    """
    settings = workdir / "searxng" / "settings.yml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    secret = os.environ.get("SEARXNG_SECRET") or os.urandom(24).hex()
    settings.write_text(
        "\n".join(
            [
                "use_default_settings: true",
                "general:",
                "  instance_name: yura",
                "server:",
                f'  secret_key: "{secret}"',
                "  limiter: false",
                "  image_proxy: false",
                "  port: 8080",
                "  bind_address: 0.0.0.0",
                "search:",
                "  safe_search: 0",
                "  formats:",
                "    - html",
                "    - json",
                "engines:",
                "  - name: duckduckgo",
                "    disabled: false",
                "  - name: brave",
                "    disabled: false",
                "  - name: wikipedia",
                "    disabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if shutil.which("docker") is None:
        log("docker is not installed — SearXNG cannot start")
        return None
    _run(["docker", "rm", "-f", "havoc-searxng"], env=os.environ.copy())
    started = _spawn(
        [
            "docker", "run", "--rm", "--name", "havoc-searxng",
            "-p", f"127.0.0.1:{port}:8080",
            # One file, not the directory: the image keeps uwsgi.ini and the rest
            # of its config in /etc/searxng, and mounting over the directory hides
            # them — the container then exits before it ever listens.
            "-v", f"{settings}:/etc/searxng/settings.yml:rw",
            "-e", "SEARXNG_BASE_URL=http://127.0.0.1:8080/",
            "searxng/searxng:latest",
        ],
        log_path=workdir / "logs" / "searxng.log",
    )
    return started


def start_comfy(port: int, workdir: Path) -> subprocess.Popen | None:
    """ComfyUI on CPU with a fast single-file checkpoint, 8 steps by default.

    A CPU SD1.5 image at 20 steps is 1-3 minutes. Eight steps of a turbo-style
    checkpoint is 30-50 seconds, which is the difference between a command someone
    uses and one they stop using. ``--quality`` switches the *graph the bot sends*
    to 20 steps; the checkpoint stays the fast one, because a slow checkpoint cannot
    be made fast.

    Three things here are scars from real runs:

    * The checkout is **pinned to a tag**. Master changes under you, and a service
      that worked yesterday is a different program today.
    * The requirements install runs **on every start**, cache hit or not. The
      Actions cache restores the ComfyUI directory but not site-packages, so
      skipping it on a hit is how the second run died with
      ``ModuleNotFoundError: No module named 'filelock'``.
    * ``filelock`` is installed by hand: ComfyUI imports it from
      ``app/database/db.py`` and does not list it in ``requirements.txt``.
    """
    checkout = workdir / "ComfyUI"
    wanted = os.environ.get("COMFY_TAG") or COMFY_TAG
    if not (checkout / "main.py").exists():
        if _run([
            "git", "clone", "--depth", "1", "--branch", wanted,
            "https://github.com/comfyanonymous/ComfyUI.git", str(checkout),
        ]) != 0:
            log(f"could not clone ComfyUI {wanted}")
            return None
    installed = _run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(checkout / "requirements.txt")],
    )
    if installed != 0:
        log("ComfyUI requirements failed to install")
        return None
    # CPU wheels, explicitly: the default index pulls a CUDA build that is 2.5 GB
    # and cannot run here. Only reinstall when what is there is a CUDA build —
    # `--force-reinstall` on every start would undo the pip cache for nothing.
    version = _capture([sys.executable, "-c", "import torch; print(torch.__version__)"])
    if not version or "+cu" in version or version.startswith("2.5.1+cu"):
        _run([
            sys.executable, "-m", "pip", "install", "-q", "--force-reinstall",
            "--index-url", "https://download.pytorch.org/whl/cpu", "torch", "torchvision",
        ])
    missing = [
        module for module in EXTRA_MODULES if _capture([sys.executable, "-c", f"import {module}"]) is None
    ]
    if missing:
        log(f"ComfyUI imports these but does not require them: {', '.join(missing)}")
        _run([sys.executable, "-m", "pip", "install", "-q", *missing])
    check = _capture(
        [
            sys.executable,
            "-c",
            "import torch, filelock; print(torch.__version__, 'cpu' if not torch.cuda.is_available() else 'cuda')",
        ]
    )
    log(f"torch: {check or '(could not import torch and filelock)'}")

    checkpoints = checkout / "models" / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    target = checkpoints / COMFY_CHECKPOINT
    if not target.exists():
        fetched = _run([
            sys.executable, str(ROOT / "scripts" / "fetch_artifact.py"),
            "--name", "sd-turbo", "--into", str(checkpoints),
            "--as", COMFY_CHECKPOINT,
        ])
        if fetched != 0 or not target.exists():
            log("no checkpoint: see scripts/fetch_artifact.py output above")
            return None
    log(f"checkpoint: {target.name} ({target.stat().st_size // (1024 * 1024)} MB)")
    return _spawn(
        [sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", str(port),
         "--disable-auto-launch", "--dont-print-server"],
        log_path=workdir / "logs" / "comfy.log",
        cwd=checkout,
    )


def start_ollama(port: int, workdir: Path) -> subprocess.Popen | None:
    """Ollama, plus one pulled model. Nothing to clone, one script to install."""
    if shutil.which("ollama") is None:
        if _run(["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]) != 0:
            log("could not install ollama")
            return None
    # ollama's installer registers a systemd unit that starts a server on 11434
    # immediately. Binding the same port then fails with "address already in use",
    # which is exactly what the first run on a real runner reported. Take the port
    # back: our server is the one with the models directory in the workdir, so it
    # is the one that survives the run.
    if os.environ.get("GITHUB_ACTIONS"):
        _run(["bash", "-c", "sudo systemctl stop ollama >/dev/null 2>&1 || true; sudo systemctl disable ollama >/dev/null 2>&1 || true"])
    _run(["bash", "-c", "pkill -f 'ollama serve' >/dev/null 2>&1 || true"])
    time.sleep(2)
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}", OLLAMA_MODELS=str(workdir / "ollama-models"), OLLAMA_KEEP_ALIVE="30m")
    process = _spawn(["ollama", "serve"], log_path=workdir / "logs" / "ollama.log", env=env)
    # The pull is the slow part (2 GB for a 3B); it must finish before the gate
    # reports healthy, or the first chat request arrives to an empty library.
    for _ in range(120):
        if _http(f"http://127.0.0.1:{port}/api/tags")[0] == 200:
            break
        time.sleep(2)
    log(f"pulling {OLLAMA_MODEL} (this is the slow part)")
    _run(["ollama", "pull", OLLAMA_MODEL], env=env)
    return process


STARTERS = {"search": start_search, "comfy": start_comfy, "ollama": start_ollama}
#: One real request per service, for the self-test. `search` and `ollama` are
#: answered by a live model; ComfyUI's picture path is proven by
#: `scripts/probe_services.py` and costs a minute, so the self-test asks for
#: system stats instead.
PROBES: dict[str, tuple[str, str]] = {
    "search": ("/search", "/search?q=transformers&format=json"),
    # The label is what the transcript says; the path is what `_probe` looks at,
    # and "/prompt" is the marker for the picture cycle (see `_probe`).
    "comfy": ("a picture", "/prompt"),
    "ollama": ("/api/tags", "/api/tags"),
}

HEALTH = {
    "search": "/search?q=ping&format=json",
    "comfy": "/system_stats",
    "ollama": "/api/tags",
}


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------
class Tunnel:
    """A cloudflared process and whatever URL it printed."""

    URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def __init__(self, process: subprocess.Popen, log_path: Path, url: str, mode: str) -> None:
        self.process = process
        self.log_path = log_path
        self.url = url
        self.mode = mode

    @property
    def alive(self) -> bool:
        return self.process.poll() is None


def _cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    # A single static binary; the apt repo needs a key and a sources line.
    target = Path("/tmp/cloudflared")
    if target.exists() and os.access(target, os.X_OK):
        return str(target)
    if _run(["bash", "-c", f"curl -fsSL -o {target} https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x {target}"]) != 0:
        return None
    return str(target)


def open_tunnel(gate_port: int, workdir: Path) -> Tunnel | None:
    """A public URL for the gate, or ``None`` (self-hosted means none needed)."""
    binary = _cloudflared()
    if binary is None:
        log("cloudflared is unavailable — no public URL")
        return None
    log_path = workdir / "logs" / "cloudflared.log"
    token = (os.environ.get("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
    if token:
        process = _spawn([binary, "tunnel", "--no-autoupdate", "run", "--token", token],
                         log_path=log_path)
        # A named tunnel's hostname comes from the dashboard; the gate port is
        # what an ingress rule must point at. No URL to parse.
        mode, url = "named", (os.environ.get("CLOUDFLARE_HOSTNAME") or "").strip()
        log(f"named tunnel ({mode}) — expecting the dashboard to route to 127.0.0.1:{gate_port}")
        if not url:
            log("CLOUDFLARE_HOSTNAME is unset, so there is nothing to publish")
            return Tunnel(process, log_path, "", mode)
        return Tunnel(process, log_path, url, mode)

    process = _spawn([binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{gate_port}"],
                     log_path=log_path)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        body = ""
        try:
            body = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        found = Tunnel.URL_PATTERN.search(body)
        if found:
            return Tunnel(process, log_path, found.group(0), "quick")
        if process.poll() is not None:
            log(f"cloudflared exited: {tail(log_path)}")
            return None
        time.sleep(1.0)
    log("cloudflared did not print a URL within 90s")
    return None


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def payload_for(
    service: str,
    *,
    url: str,
    run_id: str,
    started_at: str,
    gate_port: int,
    model: str,
    version: str,
) -> dict[str, Any]:
    """Exactly what goes into ``.services/<name>.json``. Nothing secret.

    ``gated`` says whether a caller needs the token for this run, and ``public``
    whether the owner asked for a service without one at all. The bot reads both:
    a header on a public service is harmless, but a *missing* header on a gated one
    is a 401 that reads like a bug.
    """
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00")) if started_at else datetime.now(UTC)
    expires = started + timedelta(hours=6)
    return {
        "service": service,
        "url": url,
        "run_id": run_id,
        "started_at": started_at,
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gate_port": gate_port,
        "upstream_port": PORTS.get(service, 0),
        "model": model,
        "version": version,
        "published_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gated": not is_public(),
        "public": is_public(),
    }


def write_local(service: str, body: dict[str, Any]) -> Path:
    SERVICES_DIR.mkdir(parents=True, exist_ok=True)
    path = SERVICES_DIR / f"{service}.json"
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def commit(message: str, *paths: str, attempts: int = 3) -> bool:
    """Commit and push, retrying through the data-snapshot commits.

    The repository is written to by the bot every few minutes (a data snapshot),
    so a push almost always races. Rebase-and-retry, never force: this branch is
    shared with a running bot.
    """
    branch = os.environ.get("GITHUB_REF_NAME") or "arena/b9a136e7-self"
    if not os.environ.get("GITHUB_TOKEN") and os.environ.get("HOST_DRY_RUN") != "1":
        log("no GITHUB_TOKEN: publishing locally only")
        return False
    if not os.environ.get("GITHUB_ACTIONS") and os.environ.get("SERVICE_LOCAL_PUSH") != "1":
        # A local run must not push to the branch the bot is serving from: the
        # address it would commit is 127.0.0.1, and the bot would then believe
        # a service is up and knock on its own loopback. Twice that happened
        # before this guard existed.
        log("not in CI: writing the address locally, not committing it")
        return False
    # Build a one-file commit on the latest branch with a private index, just as
    # the bot status publisher does. Never rebase the running checkout: a pending
    # conflict or stalled Git command otherwise freezes the entire service before
    # it can even start. All subprocesses are bounded and never log credentials.
    if len(paths) != 1:
        log("service publisher requires exactly one path")
        return False
    path = Path(paths[0]).resolve()
    try:
        relative = path.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        log("service report is outside the repository")
        return False
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Yura service host",
        "GIT_AUTHOR_EMAIL": "services@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Yura service host",
        "GIT_COMMITTER_EMAIL": "services@users.noreply.github.com",
    }

    def git(*args: str, extra: dict[str, str] | None = None) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=ROOT, env={**env, **(extra or {})},
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    for attempt in range(1, attempts + 1):
        fetched = git("fetch", "-q", "origin", branch)
        base = git("rev-parse", "FETCH_HEAD") if fetched is not None else None
        blob = git("hash-object", "-w", "--", str(path)) if base else None
        if base and blob:
            with tempfile.TemporaryDirectory(prefix="service-index-") as tmp:
                index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
                if git("read-tree", base, extra=index) is not None and git(
                    "update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", extra=index
                ) is not None:
                    tree = git("write-tree", extra=index)
                    commit_sha = git("commit-tree", tree, "-p", base, "-m", message) if tree else None
                    if commit_sha and git("push", "-q", "origin", f"{commit_sha}:refs/heads/{branch}") is not None:
                        log(f"published on attempt {attempt}")
                        return True
        if attempt < attempts:
            time.sleep(2 * attempt)
    log("could not publish: bounded attempts failed")
    return False


# ---------------------------------------------------------------------------
# Supervision
# ---------------------------------------------------------------------------
class Host:
    """One service, its gate, its tunnel, and the loop that keeps them up."""

    def __init__(
        self,
        service: str,
        *,
        deadline: float,
        no_tunnel: bool = False,
        tunnel_url: str = "",
        self_test_only: bool = False,
        no_self_test: bool = False,
    ) -> None:
        self.service = service
        self.deadline = deadline
        self.no_tunnel = no_tunnel or self_test_only
        self.self_test_only = self_test_only
        self.no_self_test = no_self_test
        self.forced_url = tunnel_url
        self.run_id = (os.environ.get("GITHUB_RUN_ID") or "local").strip()
        self.started_at = (os.environ.get("SERVICE_STARTED_AT") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")).strip()
        self.workdir = Path(os.environ.get("SERVICE_WORKDIR") or (ROOT / ".runtime" / f"service-{service}"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "logs").mkdir(parents=True, exist_ok=True)
        self.upstream: subprocess.Popen | None = None
        self.gate: subprocess.Popen | None = None
        self.tunnel: Tunnel | None = None
        self.published_url = ""
        self.restarts = 0
        self.published_committed = False
        self.upstream_port = PORTS[service]
        self.gate_port = GATE_PORTS[service]
        self.model_name = (
            (os.environ.get("OLLAMA_MODEL") or OLLAMA_MODEL)
            if service == "ollama"
            else (os.environ.get("COMFY_CKPT") or COMFY_CHECKPOINT)
        )

    # -- health -------------------------------------------------------
    def upstream_healthy(self) -> bool:
        return _http(f"http://127.0.0.1:{self.upstream_port}{HEALTH[self.service]}", timeout=10.0)[0] == 200

    def gate_healthy(self) -> bool:
        """Is *our* gate answering on the port — not somebody's leftovers?

        A stale gate from a killed run answers `/healthz` too, and then proxies
        to an upstream that died with it: every request 502s while the host
        happily reports "gate is up". So the run id has to match as well.
        """
        status, payload = _http(f"http://127.0.0.1:{self.gate_port}/healthz", timeout=5.0)
        if status != 200:
            return False
        try:
            body = json.loads(payload.decode("utf-8", "replace") or "{}")
        except ValueError:
            return False
        return str(body.get("run_id", "")) == self.run_id

    def public_healthy(self) -> bool:
        if not self.published_url:
            return False
        return _http(f"{self.published_url}/healthz", timeout=15.0)[0] == 200

    # -- lifecycle ----------------------------------------------------
    def start_upstream(self, *, wait: float = 300.0) -> bool:
        log(f"starting {self.service} on 127.0.0.1:{self.upstream_port}")
        self.upstream = STARTERS[self.service](self.upstream_port, self.workdir)
        if self.upstream is None:
            return False
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.upstream.poll() is not None:
                log(f"{self.service} exited during startup: {tail(self.workdir / 'logs' / (self.service + '.log'))}")
                return False
            if self.upstream_healthy():
                log(f"{self.service} is healthy")
                return True
            time.sleep(3.0)
        log(f"{self.service} did not become healthy in {wait:.0f}s: {tail(self.workdir / 'logs' / (self.service + '.log'))}")
        return False

    def start_gate(self) -> bool:
        env = dict(os.environ)
        env.update(
            SERVICE_NAME=self.service,
            SERVICE_RUN_ID=self.run_id,
            SERVICE_STARTED_AT=self.started_at,
            SERVICE_GATE_PORT=str(self.gate_port),
        )
        # No secret is not a failure any more: `service_token.secret_for` falls
        # back to the committed default (so nothing has to be configured), and
        # `SERVICE_PUBLIC=1` means no token at all.
        secret = secret_for()
        env["SERVICE_SECRET"] = secret
        self._gate_reason = ""
        if _port_taken(self.gate_port):
            # Binding after the check is not atomic, but it turns the common
            # case — a previous run's gate still holding the port — into a
            # sentence instead of a mystery 502 twenty minutes later.
            log(f"port {self.gate_port} already has something listening on it")
            self._gate_reason = f"port {self.gate_port} is already in use"
            return False
        self.gate = _spawn(
            [sys.executable, str(ROOT / "scripts" / "service_gate.py"),
             "--port", str(self.gate_port),
             "--upstream", f"127.0.0.1:{self.upstream_port}"],
            log_path=self.workdir / "logs" / "gate.log",
            env=env,
        )
        for _ in range(30):
            if self.gate_healthy():
                log(f"gate is up on 127.0.0.1:{self.gate_port}")
                return True
            if self.gate.poll() is not None:
                log(f"gate exited: {tail(self.workdir / 'logs' / 'gate.log')}")
                return False
            time.sleep(0.5)
        log("gate did not answer /healthz")
        return False

    def start_tunnel(self) -> str:
        if self.no_tunnel:
            url = f"http://127.0.0.1:{self.gate_port}"
            log(f"no tunnel: publishing the loopback address {url}")
            return url
        if self.forced_url:
            log(f"using the given URL {self.forced_url}")
            return self.forced_url
        self.tunnel = open_tunnel(self.gate_port, self.workdir)
        if self.tunnel is None or not self.tunnel.url:
            log("no public URL: the service stays local for this run")
            return ""
        log(f"public URL: {self.tunnel.url}")
        return self.tunnel.url

    def publish(self, url: str) -> bool:
        """Write the address, and say whether it actually left this runner."""
        if url and url == self.published_url and self.published_committed:
            return True
        version = ""
        if self.service == "search":
            version = "searxng"
        elif self.service == "comfy":
            version = f"comfyui/cpu/{COMFY_CHECKPOINT}"
        elif self.service == "ollama":
            version = f"ollama/{OLLAMA_MODEL}"
        body = payload_for(
            self.service,
            url=url,
            run_id=self.run_id,
            started_at=self.started_at,
            gate_port=self.gate_port,
            model=OLLAMA_MODEL if self.service == "ollama" else (COMFY_CHECKPOINT if self.service == "comfy" else "searxng"),
            version=version,
        )
        path = write_local(self.service, body)
        log(f"wrote {path.relative_to(ROOT)} → {url or '(local only)'}")
        self.published_url = url
        self.published_committed = commit(f"services: {self.service} up on run {self.run_id} [skip ci]", str(path))
        return self.published_committed

    # -- the loop -----------------------------------------------------
    def run(self) -> int:
        self.status(f"starting {self.service} on this runner")
        if not self.start_upstream():
            log("the upstream never came up; publishing nothing (a wrong URL is worse than none)")
            self.report(
                "\n".join(
                    [
                        f"the {self.service} upstream never came up",
                        "",
                        "--- log tail ---",
                        tail(self.workdir / "logs" / f"{self.service}.log", 25)
                        or tail(self.workdir / "logs" / "searxng.log", 25)
                        or "(the starter produced no log)",
                    ]
                )
            )
            self.stop()
            return 1
        self.status(f"{self.service} is up behind the gate")
        if not self.start_gate():
            log("the gate never came up; stopping the upstream rather than exposing it")
            self.report(
                "\n".join(
                    [
                        "the gate never came up, so the upstream was stopped rather than exposed",
                        "",
                        f"reason: {getattr(self, '_gate_reason', '') or 'the gate exited'}",
                        "",
                        "--- gate log ---",
                        tail(self.workdir / "logs" / "gate.log", 25) or "(no log: the gate never started)",
                    ]
                )
            )
            self.stop()
            return 1
        # Prove it before publishing it. This is a real picture, a real search
        # and a real completion, through the gate, on this runner — so the run's
        # committed transcript says "it works" rather than "it started". It costs
        # a minute; a service that publishes without ever having answered is worth
        # less than a minute.
        if not self.no_self_test:
            log("self-test before going public (a real request through the gate)")
            self.status("proving it through the gate with a real request")
            if self.self_test() != 0:
                log("the self-test failed; not publishing an address for something that does not answer")
                self.stop()
                return 1
        if self.self_test_only:
            log("--self-test: nothing will be published and no tunnel will be opened")
            self.stop()
            return 0
        url = self.start_tunnel()
        published = self.publish(url)
        self.status(f"published {url or '(loopback only)'}" if published else f"the address did not reach the branch yet: {url or '(loopback only)'}")
        if url and self.published_url:
            self._confirm_public(url)

        checked_upstream = checked_public = time.monotonic()
        checked_publish = time.monotonic()
        while time.monotonic() < self.deadline:
            time.sleep(20.0)

            if self.gate is None or self.gate.poll() is not None:
                log("the gate died; restarting it")
                self.start_gate()

            if self.upstream is None or self.upstream.poll() is not None:
                self.restarts += 1
                log(f"[restart {self.restarts}] {self.service} died: {tail(self.workdir / 'logs' / (self.service + '.log'))}")
                self.start_upstream()

            if time.monotonic() - checked_upstream > 60:
                checked_upstream = time.monotonic()
                if not self.upstream_healthy():
                    self.restarts += 1
                    log(f"[restart {self.restarts}] {self.service} stopped answering; restarting")
                    self.stop_upstream_only()
                    self.start_upstream()

            if not self.no_tunnel and (self.tunnel is None or not self.tunnel.alive):
                if self.tunnel is not None:
                    log(f"the tunnel died: {tail(self.tunnel.log_path)}")
                url = self.start_tunnel()
                self.publish(url)

            if self.published_url and not self.published_committed and time.monotonic() - checked_publish > 120:
                # The address is written locally but the push lost its race with
                # a data snapshot. Without this the service serves for six hours
                # and the repository says it never started.
                checked_publish = time.monotonic()
                self.published_committed = self.publish(self.published_url)
                if self.published_committed:
                    self.status(f"published {self.published_url} (second attempt)")

            if self.published_url and time.monotonic() - checked_public > 90:
                checked_public = time.monotonic()
                if not self.public_healthy():
                    log("the public URL stopped answering /healthz; checking the gate locally")
                    if self.gate_healthy():
                        # The gate is fine, so the tunnel is the problem.
                        if self.tunnel is not None:
                            self.stop_tunnel_only()
                        url = self.start_tunnel()
                        self.publish(url)

            if time.monotonic() > self.deadline - 60:
                log("about to hand over: the successor run is already queued")
        log(f"deadline reached after {self.restarts} restart(s)")
        return 0

    # -- evidence -----------------------------------------------------
    def status(self, line: str) -> None:
        """One timestamped line of progress, in the repository, while it happens.

        `.services/last-<name>.txt` only appears when something fails or when a
        run publishes, so a run that hangs in the middle — a picture that never
        comes back, a push that keeps losing the race — looks identical to a run
        that is serving happily. This file says where it is right now, in one
        commit per step, which is cheap and answers "is it stuck?" without a job
        log nobody can read.
        """
        path = SERVICES_DIR / f"status-{self.service}.txt"
        SERVICES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%H:%M:%SZ")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} run {self.run_id} · {line}\n")
        log(line)
        if os.environ.get("GITHUB_ACTIONS"):
            commit(f"services: {self.service} {line[:60]} [skip ci]", str(path))

    def report(self, text: str, *, append: bool = False) -> None:
        """Write what happened to a file in the repo, and commit it from CI.

        GitHub's job logs are not readable from this sandbox (`gh run view --log`
        and the logs API both answer EOF), and "it worked on the runner" is worth
        nothing without a transcript. So a run leaves one in the repository:
        `.services/last-<name>.txt`, a few kilobytes, `[skip ci]`.
        """
        body = text.strip()
        if not body:
            return
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = SERVICES_DIR / f"last-{self.service}.txt"
        SERVICES_DIR.mkdir(parents=True, exist_ok=True)
        header = f"{self.service} · run {self.run_id} · {stamp}"
        if append and path.exists():
            # A later line about the same run (the public check, a restart) must
            # not throw away the self-test that came before it — that is exactly
            # how the comfy run lost its 6/6 picture transcript.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{body}\n")
        else:
            path.write_text(f"{header}\n{'-' * len(header)}\n{body}\n", encoding="utf-8")
        try:
            shown = path.relative_to(ROOT)
        except ValueError:  # a patched SERVICES_DIR in a test, or a symlinked checkout
            shown = path
        log(f"wrote {shown}")
        if os.environ.get("GITHUB_ACTIONS"):
            commit(f"services: {self.service} run report [skip ci]", str(path))

    # -- self-test ----------------------------------------------------
    def self_test(self) -> int:
        """Prove the gate and the upstream work, on this machine, without publishing.

        This is what a run does when `SERVICE_SHARED_SECRET` is not set: nothing
        becomes public, but everything is still exercised for real — the service
        is installed, started, put behind the gate, and asked a real question
        with a real token. The transcript lands in the job log, so "it installed"
        and "it answered" are different lines.
        """
        secret = secret_for()

        token = token_for(self.service, self.run_id, self.started_at, secret)
        base = f"http://127.0.0.1:{self.gate_port}"
        upstream = f"http://127.0.0.1:{self.upstream_port}"
        checks: list[tuple[str, bool, str]] = []

        code, body = _http(f"{base}/healthz", timeout=10.0)
        checks.append(("gate /healthz without a token", code == 200, f"{code} {body[:80].decode('utf-8', 'replace')}"))

        code, _ = _http(f"{upstream}{HEALTH[self.service]}", timeout=15.0)
        checks.append((f"upstream {HEALTH[self.service]} directly", code == 200, str(code)))

        code, body = _http(f"{base}{HEALTH[self.service]}", timeout=15.0)
        checks.append(("the same path through the gate without a token", code == 401, f"{code} {body[:80].decode('utf-8', 'replace')}"))

        code, body = _http(f"{base}{HEALTH[self.service]}", timeout=15.0, token="0" * 64)
        checks.append(("a wrong token is refused", code == 401, str(code)))

        probe = PROBES[self.service]
        code, body = self._probe(probe, base, token)
        text = body.decode("utf-8", "replace")
        checks.append((f"{probe[0]} through the gate with the real token", code == 200, f"{code} {text[:120]}"))
        if self.service == "search":
            checks.append(("the answer is JSON with results", '"results"' in text, text[:200]))
        if self.service == "ollama":
            checks.append(("the model is listed", self.model_name.split(":")[0] in text, text[:200]))
            reply = self._generate(base, token)
            answer = (reply or "").strip()
            checks.append(("a real reply through the gate", bool(answer), answer[:200] or "no answer"))
        if self.service == "comfy":
            # `_probe` returned the picture itself, not JSON, for this service.
            from havoc.services.imagegen import _suffix_of

            kind = _suffix_of(body)
            magic = body[:4].hex()
            # `_suffix_of` defaults to png so a file still has a name; the check
            # has to be stricter than that, or a JSON body passes as an image.
            looks_like_an_image = (
                magic == "89504e47"          # PNG
                or magic.startswith("ffd8ff")  # JPEG
                or magic == "47494638"         # GIF
                or magic == "52494646"         # RIFF (webp)
            )
            checks.append(
                (
                    "a real image came back through the gate",
                    looks_like_an_image and len(body) > 1024,
                    f"{len(body)} bytes, {kind}, starts {magic}",
                )
            )

        failures = 0
        lines: list[str] = []
        for label, ok, detail in checks:
            line = f"  {'ok  ' if ok else 'FAIL'} {label} — {detail}"
            print(line, flush=True)
            lines.append(line)
            failures += 0 if ok else 1
        summary = f"self-test: {len(checks) - failures}/{len(checks)} checks passed"
        print(summary, flush=True)
        lines.append(summary)
        if failures:
            for name, path in (
                ("upstream", self.workdir / "logs" / f"{self.service}.log"),
                ("gate", self.workdir / "logs" / "gate.log"),
            ):
                tail_text = tail(path, 12)
                if tail_text:
                    lines.append(f"--- {name} log tail ---")
                    lines.append(tail_text)
        self.report("\n".join(lines))
        return 1 if failures else 0

    def _probe(self, probe: tuple[str, str], base: str, token: str) -> tuple[int, bytes]:
        """One real request through the gate, per service."""
        label, path = probe
        if self.service == "comfy" and path == "/prompt":
            # Not system_stats: a *picture*. It costs 30-50 s at 8 steps, and it
            # is the only thing that proves the checkpoint loads and a PNG comes
            # out the other end. The client doing it is the bot's own
            # ImageGenerator, pointed at the gate through a directory override, so
            # the self-test exercises the same code `.draw` does.
            picture = self._picture(base, token)
            if picture is None:
                reason = getattr(self, "_picture_reason", "") or "no file"
                return 0, f"the picture path did not produce a file: {reason}"[:300].encode()
            return 200, picture
        if self.service == "ollama" and path == "/api/generate":
            body = json.dumps({"model": self.model_name, "prompt": "Say OK.", "stream": False}).encode()
            request = urllib.request.Request(f"{base}/api/generate", data=body, method="POST")
            request.add_header("Authorization", f"Bearer {token}")
            request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 - loopback
                    return response.status, response.read(4096)
            except urllib.error.HTTPError as exc:
                return exc.code, b""
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                return 0, str(exc).encode()[:200]
        return _http(f"{base}{path}", timeout=60.0, token=token)

    def _generate(self, base: str, token: str) -> str:
        """One short completion through the gate. The point is a reply, not a tag list."""
        body = json.dumps(
            {"model": self.model_name, "prompt": "In one short sentence: what is a transformer?", "stream": False}
        ).encode()
        request = urllib.request.Request(f"{base}/api/generate", data=body, method="POST")
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310 - loopback
                payload = json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            log(f"the completion failed: {type(exc).__name__}: {exc}")
            return ""
        return str(payload.get("response") or "")

    def _picture(self, base: str, token: str) -> bytes | None:
        """Ask for one 8-step picture through the gate and return the PNG bytes.

        The secret comes from :func:`secret_for`, not straight from the
        environment: with nothing configured the host signs with the committed
        default, and reading `SERVICE_SECRET` here produced an empty token — the
        gate refused the request and the self-test reported "the picture path did
        not produce a file" while the gate was simply saying 401.
        """
        import asyncio
        import tempfile

        from havoc.services.imagegen import ImageGenerator
        from havoc.services.service_directory import ServiceDirectory

        now = datetime.now(UTC)
        directory = ServiceDirectory(
            override={
                "comfy": {
                    "service": "comfy",
                    "url": base,
                    "run_id": self.run_id,
                    "started_at": self.started_at,
                    "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "expires_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "model": self.model_name,
                    "gated": True,
                }
            },
            secret=secret_for(),
            branch="",
        )
        generator = ImageGenerator.from_env(directory=directory) or ImageGenerator(
            url="",
            directory=directory,
        )
        folder = Path(tempfile.mkdtemp())
        try:
            path = asyncio.run(generator.generate("a lighthouse in fog, pen and ink", outdir=folder))
        except Exception as exc:  # noqa: BLE001 - the reason is the finding
            reason = f"{type(exc).__name__}: {exc}"
            log(f"the picture failed: {reason}")
            self._picture_reason = reason
            return None
        if not path.exists():
            self._picture_reason = "the generator returned a path that does not exist"
            return None
        return path.read_bytes()

    def _confirm_public(self, url: str) -> None:
        """Ask the tunnel itself, from the outside, and put the answer in the report.

        `public_healthy()` goes out to the tunnel hostname and back into the gate,
        so a pass means the address really works from the internet — not that a
        process is listening on loopback. That line is the difference between
        "published" and "reachable", and it is the one worth reading later.
        """
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if self.public_healthy():
                body = "\n".join(
                    [
                        "",
                        f"public: {url}",
                        "reachable from the internet: yes — GET /healthz through the tunnel answered 200",
                        f"the bot needs no configuration: it reads .services/{self.service}.json",
                    ]
                )
                log(f"public and answering: {url}")
                self.report(body, append=True)
                return
            time.sleep(5.0)
        log(f"{url} did not answer /healthz publicly within 90s (it may have just rotated)")
        self.report(
            "\n".join(
                [
                    "",
                    f"public: {url}",
                    "reachable from the internet: no — /healthz through the tunnel did not answer within 90s",
                ]
            ),
            append=True,
        )

    # -- teardown -----------------------------------------------------
    def stop_upstream_only(self) -> None:
        if self.upstream is not None and self.upstream.poll() is None:
            self.upstream.terminate()
            try:
                self.upstream.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.upstream.kill()

    def stop_tunnel_only(self) -> None:
        if self.tunnel is not None and self.tunnel.alive:
            self.tunnel.process.terminate()

    def stop(self) -> None:
        killed: list[str] = []
        for name, process in (("gate", getattr(self, "gate", None)),
                              ("upstream", getattr(self, "upstream", None))):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                killed.append(name)
        if self.tunnel is not None and self.tunnel.alive:
            self.tunnel.process.terminate()
            killed.append("tunnel")
        # docker containers outlive their client process; the daemon keeps them.
        subprocess.call(["bash", "-c", "docker rm -f havoc-searxng >/dev/null 2>&1 || true"])
        subprocess.call(["bash", "-c", "pkill -f 'ollama serve' >/dev/null 2>&1 || true"])
        log(f"stopped: {', '.join(killed) if killed else 'nothing was running'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="host one service on this runner")
    parser.add_argument("--service", required=True, choices=sorted(PORTS))
    parser.add_argument("--deadline-minutes", type=float, default=float(os.environ.get("SERVICE_MINUTES") or 340))
    parser.add_argument("--no-tunnel", action="store_true", help="bind loopback only (self-hosted runner)")
    parser.add_argument("--tunnel-url", default="", help="publish this URL instead of opening one")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="start everything on loopback, prove it through the gate, publish nothing",
    )
    parser.add_argument(
        "--no-self-test",
        action="store_true",
        help="publish without proving the service first (not recommended)",
    )
    args = parser.parse_args(argv)

    def _stop(signum: int, _frame: Any) -> None:
        log(f"signal {signum}: stopping")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    host = Host(
        args.service,
        deadline=time.monotonic() + args.deadline_minutes * 60,
        no_tunnel=args.no_tunnel,
        self_test_only=args.self_test,
        no_self_test=args.no_self_test,
        tunnel_url=args.tunnel_url,
    )
    try:
        return host.run()
    finally:
        host.stop()


if __name__ == "__main__":
    raise SystemExit(main())
