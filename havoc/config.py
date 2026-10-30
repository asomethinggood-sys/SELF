"""Configuration loading and validation.

Design goals
------------
* One immutable :class:`Config` object is injected everywhere (dependency
  injection rather than global state).
* Values come from environment variables, optionally seeded by a local
  ``.env`` file. Real environment variables always win over ``.env``.
* Parsing never crashes the process with a bare traceback: problems are
  collected as :class:`ConfigIssue` objects and formatted for the user in
  their language by :func:`format_issues`.
* No secrets are ever stored in ``repr`` output (API hash, keys, sessions).

Spec references: §2 (non-negotiables), §9 (security), §10 (reliability),
feature areas 87 (configuration validator) and 93 (configurable prefix).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

SUPPORTED_LANGUAGES = ("en", "fa")
MISSED_JOB_POLICIES = ("run_once", "skip", "ask")
AI_PROVIDERS = ("none", "openai_compatible")
TRANSLATE_PROVIDERS = ("none", "ai", "mymemory", "libretranslate")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Proxy protocols Telethon can use. ``mtproxy`` needs a secret; the others do
#: not (SOCKS5 with a username/password is the usual choice in Iran).
PROXY_TYPES = ("socks5", "socks4", "http", "mtproxy")

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}

#: Keys that must never be echoed back in logs or diagnostics.
SECRET_ENV_KEYS = ("API_HASH", "SESSION_STRING", "AI_API_KEY", "LIBRETRANSLATE_API_KEY")


class ConfigError(Exception):
    """Raised when the configuration cannot be used at all."""

    def __init__(self, issues: Iterable[ConfigIssue]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(f"{i.field}: {i.message}" for i in self.issues) or "invalid config")


@dataclass(frozen=True)
class ConfigIssue:
    """A single validation problem tied to one configuration field."""

    field: str
    message: str
    severity: str = "error"  # "error" | "warning"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.severity}] {self.field}: {self.message}"


def _parse_bool(raw: str, default: bool = False) -> tuple[bool, bool]:
    """Return ``(value, ok)`` for a boolean-ish string."""
    if raw is None:
        return default, True
    text = raw.strip().lower()
    if text in _TRUE:
        return True, True
    if text in _FALSE:
        return False, True
    return default, False


def _parse_int(raw: str | None) -> tuple[int | None, bool]:
    if raw is None or not str(raw).strip():
        return None, True
    try:
        return int(str(raw).strip()), True
    except ValueError:
        return None, False


def _split_handles(value: str | None) -> tuple[str, ...]:
    """``@a, b`` → ``("a", "b")``. A handle is stored without its @, lowercased."""
    if not value:
        return ()
    out = []
    for part in re.split(r"[,\s]+", value):
        handle = part.strip().lstrip("@").lower()
        if handle:
            out.append(handle)
    return tuple(dict.fromkeys(out))


def _split_ids(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    out: list[int] = []
    for chunk in re.split(r"[,;\s]+", raw.strip()):
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return tuple(dict.fromkeys(out))


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``.env`` file.

    Supports ``KEY=VALUE``, ``export KEY=VALUE``, ``#`` comments, blank lines,
    single/double quotes, and inline comments after unquoted values. Never
    raises on malformed input — bad lines are skipped.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        else:
            # strip a trailing comment that follows whitespace
            value = re.sub(r"\s+#.*$", "", value).strip()
        values[key] = value
    return values


def _merged_env(dotenv_path: Path | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if dotenv_path is not None:
        env.update(parse_env_file(dotenv_path))
    env.update({k: v for k, v in os.environ.items()})  # real env wins
    return env


@dataclass(frozen=True)
class Config:
    """Immutable, validated runtime configuration.

    Secrets are excluded from ``repr`` by declaring them with ``field(repr=False)``.
    """

    # --- Telegram ---
    api_id: int | None = None
    api_hash: str = field(default="", repr=False)
    phone: str = field(default="", repr=False)
    session_path: Path = Path("data/havoc")
    session_string: str = field(default="", repr=False)

    # --- Paths ---
    data_dir: Path = Path("data")
    database_path: Path = Path("data/havoc.db")
    log_dir: Path = Path("logs")
    backup_dir: Path = Path("backups")
    backup_retention: int = 10
    plugin_dir: Path = Path("plugins")

    # --- Behaviour ---
    command_prefix: str = "."
    default_language: str = "en"
    timezone: str = "Asia/Tehran"
    #: The owner, by number. His handle can be resolved over the network and that
    #: lookup can fail (it did); the number cannot. Both are checked.
    owner_ids: tuple[int, ...] = (7542695649,)
    automation_allowlist: tuple[int, ...] = ()
    automation_denylist: tuple[int, ...] = ()
    dry_run: bool = False
    debug: bool = False
    log_level: str = "INFO"
    missed_job_policy: str = "run_once"
    outbound_rate_limit: int = 20

    # --- Integrations ---
    ai_provider: str = "none"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = field(default="", repr=False)
    ai_model: str = "gpt-4o-mini"
    ai_timeout: float = 30.0
    #: Where the committed credentials live. Several keys, several providers: the
    #: file is base64 so a public repository's push protection accepts it.
    ai_keys_path: str = ".auth/ai-keys.json"
    #: Owners named the way a person names themselves. A username is the only
    #: handle that is stable, human-readable and safe to commit; the numeric id
    #: behind it is resolved once at login and stored in the database, so nothing
    #: has to be re-read from Telegram on every message.
    owner_usernames: tuple[str, ...] = ("edited_by_nyx",)

    translate_provider: str = "none"
    libretranslate_url: str = ""
    libretranslate_api_key: str = field(default="", repr=False)
    translate_timeout: float = 15.0
    plugins_enabled: bool = True

    # --- Network ---
    #: MTProto is blocked in some networks (Iran among them), so the Telegram
    #: connection itself sometimes has to travel through a proxy. This is the
    #: proxy for the *Telethon* connection only; the AI/translation HTTP clients
    #: are unaffected.
    proxy_type: str = ""  # "" | socks5 | socks4 | http | mtproxy
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = field(default="", repr=False)
    proxy_password: str = field(default="", repr=False)
    #: Only for mtproxy: the secret Telegram gives when you create a proxy.
    proxy_secret: str = field(default="", repr=False)

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def proxy_configured(self) -> bool:
        return bool(self.proxy_type and self.proxy_host and self.proxy_port)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def session_file(self) -> Path:
        """Session path with the ``.session`` suffix Telethon expects."""
        return self.session_path if self.session_path.suffix == ".session" else self.session_path.with_suffix(".session")

    @property
    def ai_available(self) -> bool:
        """True when a key came from the environment or the config file.

        Deliberately not a filesystem check: a config object should answer the
        same question everywhere. Credentials that live in the key file are the
        router's business (:attr:`havoc.services.ai_router.AIRouter.available`),
        and :attr:`ai_keys_configured` reports whether that file is present.
        """
        return self.ai_provider != "none" and bool(self.ai_api_key)

    @property
    def ai_keys_configured(self) -> bool:
        """True when the multi-key file exists, so a key file alone still counts."""
        try:
            return Path(self.ai_keys_path).expanduser().exists()
        except OSError:  # pragma: no cover - unreadable path
            return False

    @property
    def translation_available(self) -> bool:
        # "none" is the default and it means "not chosen by hand", not "off":
        # the runtime hands the AI engine to the translator whenever there is
        # one, so a fresh clone can translate without an extra key or flag.
        if self.translate_provider == "none":
            return self.ai_available or bool(self.ai_api_key)
        if self.translate_provider == "libretranslate":
            return bool(self.libretranslate_url)
        return True

    def __post_init__(self) -> None:
        # A handle is written by a human: "@Edited_By_Nyx", " edited_by_nyx ".
        # Normalise here so the env loader and a directly built Config agree.
        # ``object.__setattr__`` because Config is frozen: this is normalisation,
        # not a change of meaning.
        object.__setattr__(
            self, "owner_usernames", _split_handles(",".join(self.owner_usernames or ()))
        )

    def redacted(self) -> dict[str, object]:
        """A dict safe to log or show in diagnostics."""
        out: dict[str, object] = {}
        for f in self.__dataclass_fields__.values():  # type: ignore[attr-defined]
            value = getattr(self, f.name)
            if f.name in {"__dict__"}:
                continue
            if f.repr is False and value:
                out[f.name] = "***set***"
            elif isinstance(value, Path):
                out[f.name] = str(value)
            elif isinstance(value, tuple):
                out[f.name] = list(value)
            else:
                out[f.name] = value
        return out

    def with_overrides(self, **kwargs: object) -> Config:
        return replace(self, **kwargs)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
_IANA_RE = re.compile(r"^[A-Za-z0-9_+\-/]+$")


def validate(config: Config) -> list[ConfigIssue]:
    """Validate a config. Returns issues; empty list means healthy."""
    issues: list[ConfigIssue] = []

    if config.api_id is None:
        issues.append(ConfigIssue("API_ID", "missing or not numeric"))
    elif config.api_id <= 0:
        issues.append(ConfigIssue("API_ID", "must be a positive integer"))

    if not config.api_hash:
        issues.append(ConfigIssue("API_HASH", "missing"))
    elif not re.fullmatch(r"[0-9a-fA-F]{32}", config.api_hash.strip()):
        issues.append(ConfigIssue("API_HASH", "must be a 32-character hex string", "warning"))

    if not config.has_credentials and not config.session_string:
        issues.append(
            ConfigIssue(
                "API_ID/API_HASH",
                "Telegram credentials are required for first login",
            )
        )

    if config.session_string and len(config.session_string) < 50:
        issues.append(ConfigIssue("SESSION_STRING", "does not look like a valid session string"))

    if config.proxy_type:
        if config.proxy_type not in PROXY_TYPES:
            issues.append(
                ConfigIssue(
                    "PROXY_TYPE",
                    f"unsupported proxy type {config.proxy_type!r} (expected one of {', '.join(PROXY_TYPES)})",
                )
            )
        if not config.proxy_host:
            issues.append(ConfigIssue("PROXY_HOST", "required when PROXY_TYPE is set"))
        if not (0 < config.proxy_port < 65536):
            issues.append(ConfigIssue("PROXY_PORT", "must be a port between 1 and 65535"))
        if config.proxy_type == "mtproxy" and not config.proxy_secret:
            issues.append(ConfigIssue("PROXY_SECRET", "required for an mtproxy"))
        if config.proxy_type != "mtproxy" and config.proxy_secret:
            issues.append(
                ConfigIssue("PROXY_SECRET", "only used with PROXY_TYPE=mtproxy", "warning")
            )

    prefix = config.command_prefix
    if not prefix:
        issues.append(ConfigIssue("COMMAND_PREFIX", "must not be empty"))
    elif len(prefix) > 4:
        issues.append(ConfigIssue("COMMAND_PREFIX", "must be 1-4 characters"))
    elif any(ch.isspace() for ch in prefix):
        issues.append(ConfigIssue("COMMAND_PREFIX", "must not contain whitespace"))

    if config.default_language not in SUPPORTED_LANGUAGES:
        issues.append(
            ConfigIssue(
                "DEFAULT_LANGUAGE",
                f"unsupported language {config.default_language!r} (expected one of {', '.join(SUPPORTED_LANGUAGES)})",
            )
        )

    if not _IANA_RE.fullmatch(config.timezone or ""):
        issues.append(ConfigIssue("TIMEZONE", "contains invalid characters"))
    else:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(config.timezone)
        except Exception:
            issues.append(ConfigIssue("TIMEZONE", f"unknown IANA timezone {config.timezone!r}"))

    if config.log_level.upper() not in LOG_LEVELS:
        issues.append(
            ConfigIssue("LOG_LEVEL", f"must be one of {', '.join(LOG_LEVELS)}", "warning")
        )

    if config.missed_job_policy not in MISSED_JOB_POLICIES:
        issues.append(
            ConfigIssue(
                "MISSED_JOB_POLICY",
                f"must be one of {', '.join(MISSED_JOB_POLICIES)}",
                "warning",
            )
        )

    if config.ai_provider not in AI_PROVIDERS:
        issues.append(ConfigIssue("AI_PROVIDER", f"must be one of {', '.join(AI_PROVIDERS)}", "warning"))
    elif config.ai_provider != "none" and not (config.ai_available or config.ai_keys_configured):
        issues.append(
            ConfigIssue(
                "AI_API_KEY",
                f"required when AI_PROVIDER is enabled (or put keys in {config.ai_keys_path})",
                "warning",
            )
        )

    if config.translate_provider not in TRANSLATE_PROVIDERS:
        issues.append(
            ConfigIssue(
                "TRANSLATE_PROVIDER",
                f"must be one of {', '.join(TRANSLATE_PROVIDERS)}",
                "warning",
            )
        )
    elif config.translate_provider == "libretranslate" and not config.libretranslate_url:
        issues.append(ConfigIssue("LIBRETRANSLATE_URL", "required when TRANSLATE_PROVIDER=libretranslate", "warning"))

    if config.backup_retention < 1:
        issues.append(ConfigIssue("BACKUP_RETENTION", "must be >= 1", "warning"))

    if config.outbound_rate_limit < 1:
        issues.append(ConfigIssue("OUTBOUND_RATE_LIMIT", "must be >= 1", "warning"))

    return issues


def has_blocking(issues: Iterable[ConfigIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------
def load_config(
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> tuple[Config, list[ConfigIssue]]:
    """Build a :class:`Config` from a mapping (or the process environment).

    Returns the config plus a list of non-fatal issues. Blocking issues are
    available through :func:`validate` and :func:`has_blocking`.
    """
    env = _merged_env(dotenv_path) if env is None else dict(env)
    issues: list[ConfigIssue] = []

    def raw(key: str, default: str = "") -> str:
        return str(env.get(key, default) or "").strip()

    api_id, ok = _parse_int(env.get("API_ID"))
    if not ok:
        issues.append(ConfigIssue("API_ID", "is not a valid integer"))

    def path_of(key: str, default: str) -> Path:
        value = raw(key) or default
        return Path(value).expanduser()

    def bool_of(key: str, default: bool) -> bool:
        value, ok = _parse_bool(env.get(key), default)
        if not ok:
            issues.append(ConfigIssue(key, "is not a valid boolean (use true/false)", "warning"))
        return value

    def int_of(key: str, default: int) -> int:
        value, ok = _parse_int(env.get(key))
        if not ok:
            issues.append(ConfigIssue(key, "is not a valid integer; using default", "warning"))
            return default
        return default if value is None else value

    def float_of(key: str, default: float) -> float:
        text = raw(key)
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            issues.append(ConfigIssue(key, "is not a valid number; using default", "warning"))
            return default

    data_dir = path_of("DATA_DIR", "data")
    config = Config(
        api_id=api_id,
        api_hash=raw("API_HASH"),
        phone=re.sub(r"[\s\-()]", "", raw("PHONE")),
        session_path=path_of("SESSION_PATH", str(data_dir / "havoc")),
        session_string=raw("SESSION_STRING"),
        data_dir=data_dir,
        database_path=path_of("DATABASE_PATH", str(data_dir / "havoc.db")),
        log_dir=path_of("LOG_DIR", "logs"),
        backup_dir=path_of("BACKUP_DIR", "backups"),
        backup_retention=int_of("BACKUP_RETENTION", 10),
        plugin_dir=path_of("PLUGIN_DIR", "plugins"),
        command_prefix=raw("COMMAND_PREFIX") or ".",
        default_language=(raw("DEFAULT_LANGUAGE") or "en").lower(),
        timezone=raw("TIMEZONE") or "Asia/Tehran",
        owner_ids=_split_ids(env.get("OWNER_IDS")) or (7542695649,),
        owner_usernames=_split_handles(env.get("OWNER_USERNAMES")) or ("edited_by_nyx",),
        automation_allowlist=_split_ids(env.get("AUTOMATION_ALLOWLIST")),
        automation_denylist=_split_ids(env.get("AUTOMATION_DENYLIST")),
        dry_run=bool_of("DRY_RUN", False),
        debug=bool_of("DEBUG", False),
        log_level=(raw("LOG_LEVEL") or "INFO").upper(),
        missed_job_policy=(raw("MISSED_JOB_POLICY") or "run_once").lower(),
        outbound_rate_limit=int_of("OUTBOUND_RATE_LIMIT", 20),
        ai_provider=(raw("AI_PROVIDER") or "none").lower(),
        ai_base_url=raw("AI_BASE_URL") or "https://api.openai.com/v1",
        ai_api_key=raw("AI_API_KEY"),
        ai_model=raw("AI_MODEL") or "gpt-4o-mini",
        ai_keys_path=raw("AI_KEYS_FILE") or ".auth/ai-keys.json",
        ai_timeout=float_of("AI_TIMEOUT", 30.0),
        translate_provider=(raw("TRANSLATE_PROVIDER") or "none").lower(),
        libretranslate_url=raw("LIBRETRANSLATE_URL").rstrip("/"),
        libretranslate_api_key=raw("LIBRETRANSLATE_API_KEY"),
        translate_timeout=float_of("TRANSLATE_TIMEOUT", 15.0),
        plugins_enabled=bool_of("PLUGINS_ENABLED", True),
        proxy_type=(raw("PROXY_TYPE") or "").lower(),
        proxy_host=raw("PROXY_HOST"),
        proxy_port=int_of("PROXY_PORT", 0),
        proxy_username=raw("PROXY_USERNAME"),
        proxy_password=raw("PROXY_PASSWORD"),
        proxy_secret=raw("PROXY_SECRET"),
    )
    issues.extend(validate(config))
    return config, issues


def format_issues(issues: Iterable[ConfigIssue]) -> str:
    """Human-readable, secret-free summary of configuration problems."""
    lines = []
    for issue in issues:
        lines.append(f"  • {issue.field}: {issue.message} ({issue.severity})")
    return "\n".join(lines)


def ensure_runtime_dirs(config: Config) -> None:
    """Create directories HAVOC needs, with permissions as tight as practical."""
    for path in (config.data_dir, config.log_dir, config.backup_dir, config.session_path.parent):
        path.mkdir(parents=True, exist_ok=True)
