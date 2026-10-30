"""Structured, rotating, redacted logging.

* Rotating file handler under ``LOG_DIR`` (10 MB × 5) plus a console handler.
* :class:`RedactionFilter` scrubs credentials and phone numbers from every
  record, including third-party loggers such as Telethon.
* A dedicated ``error`` log keeps full stack traces locally; the part sent to
  Telegram is always sanitized and short.
* Message *content* is never logged at INFO. Debug mode logs metadata only
  (feature areas 82, 83, 9).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Iterable
from pathlib import Path

from .utils.security import redact_text

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RedactionFilter(logging.Filter):
    """Scrub secrets from log messages and exception text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def add_secret(self, secret: str | None) -> None:
        if secret and secret not in self.secrets:
            self.secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format string
            message = str(record.msg)
        record.msg = redact_text(message, self.secrets)
        record.args = ()
        if record.exc_info:
            # Keep the traceback but scrub strings inside it.
            record.exc_text = None
        return True


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str = LOG_FORMAT, datefmt: str = DATE_FORMAT, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt, datefmt)
        self.secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact_text(text, self.secrets)


def setup_logging(
    *,
    log_dir: Path | str = "logs",
    level: str = "INFO",
    debug: bool = False,
    secrets: Iterable[str] = (),
    console: bool = True,
    filename: str = "havoc.log",
) -> logging.Logger:
    """Configure root logging once and return the HAVOC logger."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric = getattr(logging, level.upper(), logging.INFO)
    if debug:
        numeric = logging.DEBUG

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(numeric)

    redaction = RedactionFilter(secrets)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / filename, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(RedactingFormatter(secrets=secrets))
    file_handler.addFilter(redaction)
    file_handler.setLevel(logging.DEBUG if debug else numeric)
    root.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        log_dir / "error.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(RedactingFormatter(secrets=secrets))
    error_handler.addFilter(redaction)
    root.addHandler(error_handler)

    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(RedactingFormatter("%(levelname)-8s %(name)s: %(message)s", secrets=secrets))
        console_handler.addFilter(redaction)
        console_handler.setLevel(numeric)
        root.addHandler(console_handler)

    # Quiet noisy libraries; Telethon stays at WARNING unless debugging.
    logging.getLogger("telethon").setLevel(logging.DEBUG if debug else logging.WARNING)
    logging.getLogger("telethon.crypto").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    logger = logging.getLogger("havoc")
    logger.info("logging initialised (level=%s, dir=%s)", logging.getLevelName(numeric), log_dir)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"havoc.{name}")


def recent_errors(log_dir: Path | str = "logs", limit: int = 5) -> list[str]:
    """Tail the error log for the diagnostics command (already redacted)."""
    path = Path(log_dir) / "error.log"
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-200:]
    except OSError:
        return []
    errors = [line.strip() for line in lines if "| ERROR" in line or "| CRITICAL" in line]
    return errors[-limit:]


