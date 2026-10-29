"""Command-line style parsing for Telegram command text.

Telegram gives us a raw string like ``.remind 30m drink water``. We need to
split the command name, subcommand and free text without a shell, without
``eval``, and without surprising the user (arguments may contain spaces and
Persian text).

Command syntax
--------------
``{prefix}{name} [subcommand] [arguments...]``

* Quoted groups are supported: ``.template add party "Hello {name}"``
* ``--flag`` / ``--key=value`` tokens are extracted (used by ``--dry-run``,
  ``--in 30m`` etc.) and removed from positional arguments.
* The raw remainder is preserved for features that need the literal text
  (templates, scheduled messages, reminders).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

_TELEGRAM_EMOJI_ONLY = re.compile(r"^[\W_]+$")


class ParseError(ValueError):
    """Raised for malformed command input (user-facing)."""


@dataclass(frozen=True)
class ParsedCommand:
    """A parsed command invocation.

    Two views are kept on purpose:

    ``args`` holds the arguments proper, i.e. everything after the command name
    *and* after the subcommand when one is present:

    * ``.remind 30m water``  -> subcommand ``None``, args ``("30m", "water")``
    * ``.autoreply add hi``  -> subcommand ``"add"``, args ``("hi",)``

    Whether the first token really is a subcommand depends on the command, so
    the dispatcher consults :attr:`Command.subcommands`. When the token is not
    one of them it is demoted back into ``args``. That keeps positional and
    action-style commands unambiguous without string juggling in handlers.
    """

    prefix: str
    name: str
    subcommand: str | None
    args: tuple[str, ...]
    flags: dict[str, str] = field(default_factory=dict)
    raw_args: str = ""
    raw: str = ""
    rest: tuple[str, ...] = ()
    rest_raw: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.name} {self.subcommand}".strip() if self.subcommand else self.name

    def arg(self, index: int, default: str = "") -> str:
        return self.args[index] if len(self.args) > index else default

    def rest_arg(self, index: int, default: str = "") -> str:
        return self.rest[index] if len(self.rest) > index else default

    def require_arg(self, index: int, what: str) -> str:
        if len(self.args) <= index or not self.args[index].strip():
            raise ParseError(f"missing {what}")
        return self.args[index]

    def joined_args(self, start: int = 0) -> str:
        return " ".join(self.args[start:])

    def joined_rest(self, start: int = 0) -> str:
        return " ".join(self.rest[start:])

    def flag(self, name: str, default: str = "") -> str:
        return self.flags.get(name, default)

    def has_flag(self, name: str) -> bool:
        return name in self.flags


def strip_prefix(text: str, prefix: str) -> str | None:
    """Return the text after ``prefix`` if the message starts with a command."""
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith(prefix):
        return None
    remainder = stripped[len(prefix) :]
    if not remainder:
        return None
    # A command must start with a word character; ". " or "..." is not a command.
    if not re.match(r"[\w\u0600-\u06FF]", remainder[0]):
        return None
    return remainder


def tokenize(text: str) -> list[str]:
    """Split respecting quotes; falls back to quote-tolerant whitespace splitting."""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return [token.strip("'\"") for token in re.split(r"\s+", text.strip()) if token.strip("'\"")]


#: Flags that take a value, so ``--repeat daily`` is not ambiguous.
VALUE_FLAGS: frozenset[str] = frozenset(
    {
        "to", "repeat", "every", "type", "lang", "match", "event", "cooldown",
        "format", "intensity", "name", "last", "bio", "limit", "chat", "timezone",
        "caption", "source", "path",
    }
)


def extract_flags(tokens: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
    """Pull ``--flag``, ``--key=value`` and ``--key value`` out of a token list.

    Only flags listed in :data:`VALUE_FLAGS` consume the following token;
    everything else is a boolean flag. This keeps message bodies intact for
    commands like ``.schedule 2h "hello world" --to @chat``.
    """
    positional: list[str] = []
    flags: dict[str, str] = {}
    consumed: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and len(token) > 2:
            body = token[2:]
            key, sep, value = body.partition("=")
            key = key.lower().replace("-", "_")
            if sep:
                flags[key] = value
            elif key in VALUE_FLAGS and index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                flags[key] = tokens[index + 1]
                consumed.add(key)
                index += 1
            else:
                flags[key] = "true"
        else:
            positional.append(token)
        index += 1
    return positional, flags, consumed


def strip_flag_text(text: str, flags: dict[str, str], consumed: set[str]) -> str:
    """Remove flag syntax from a raw remainder, keeping the body intact.

    Rebuilding the body from tokens would destroy newlines and the user's
    spacing, so the flag segments are cut out of the original text instead.
    """
    out = text
    for key, value in flags.items():
        name = re.escape(key.replace("_", "-"))
        pattern = rf"(?:(?<=\s)|^)--{name}(?:={re.escape(value)})?"
        if key in consumed:
            pattern += rf"\s+{re.escape(value)}"
        out = re.sub(pattern, " ", out, count=1)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def parse_command(text: str, prefix: str = ".") -> ParsedCommand | None:
    """Parse a command message; returns ``None`` when it is not a command."""
    remainder = strip_prefix(text, prefix)
    if remainder is None:
        return None
    tokens, flags, consumed = extract_flags(tokenize(remainder))
    if not tokens:
        return None
    name = tokens[0].lstrip("/").lower()
    tokens_after_name = tokens[1:]
    subcommand = tokens_after_name[0].lower() if tokens_after_name and not tokens_after_name[0].startswith("-") else None
    rest = tuple(tokens_after_name[1:]) if subcommand else tuple(tokens_after_name)

    # Raw remainder keeps everything after the command name untouched, which
    # matters for message bodies containing quotes or newlines.
    if remainder.startswith(tokens[0]):
        raw_args = remainder[len(tokens[0]) :].strip()
    else:
        raw_args = " ".join(tokens[1:])
    raw_args = strip_flag_text(raw_args, flags, consumed)
    rest_raw = raw_args
    if subcommand and raw_args.lower().startswith(subcommand):
        rest_raw = raw_args[len(subcommand) :].strip()

    # Quoted segments were already unquoted by the tokenizer; prefer that view
    # so bodies never reach the user wrapped in quotes.
    if any(quote in raw_args for quote in ("\"", "'")):
        rest_raw = " ".join(rest)
        raw_args = rest_raw if subcommand is None else " ".join((subcommand, rest_raw)).strip()

    return ParsedCommand(
        prefix=prefix,
        name=name,
        subcommand=subcommand,
        args=rest,
        flags=flags,
        raw_args=raw_args,
        raw=text,
        rest=rest,
        rest_raw=rest_raw,
    )


def demote_subcommand(parsed: ParsedCommand) -> ParsedCommand:
    """Re-interpret the first token as a positional argument.

    Used by the dispatcher when a command does not declare that subcommand, so
    ``.remind 30m water`` keeps ``30m`` in ``args`` and ``subcommand`` is None.
    """
    if parsed.subcommand is None:
        return parsed
    return ParsedCommand(
        prefix=parsed.prefix,
        name=parsed.name,
        subcommand=None,
        args=(parsed.subcommand, *parsed.args),
        flags=parsed.flags,
        raw_args=parsed.raw_args,
        raw=parsed.raw,
        rest=(parsed.subcommand, *parsed.args),
        rest_raw=parsed.raw_args,
    )


def split_command_and_body(text: str, prefix: str) -> tuple[str, str]:
    """Split ``.cmd body text`` into ``("cmd", "body text")``."""
    remainder = strip_prefix(text, prefix)
    if remainder is None:
        return "", text
    first, _, rest = remainder.partition(" ")
    return first.lower(), rest.strip()


def parse_keyword_list(text: str) -> list[str]:
    """Split a comma/newline separated keyword list, deduplicated."""
    parts = re.split(r"[,;\n]+", text)
    seen: list[str] = []
    for part in parts:
        item = part.strip()
        if item and item.lower() not in {s.lower() for s in seen}:
            seen.append(item)
    return seen


def parse_labels(text: str) -> list[str]:
    """Labels are lowercase, dash/underscore friendly, max 24 chars."""
    labels: list[str] = []
    for raw in parse_keyword_list(text):
        label = re.sub(r"\s+", "-", raw.strip().lower())
        label = re.sub(r"[^\w\u0600-\u06FF\-]", "", label)[:24]
        if label and label not in labels:
            labels.append(label)
    return labels


def parse_pair(text: str) -> tuple[str, str]:
    """Parse ``name=value`` or ``name: value``."""
    match = re.match(r"\s*([^=:]+?)\s*[=:]\s*(.+)$", text, re.DOTALL)
    if not match:
        raise ParseError("expected name=value")
    return match.group(1).strip(), match.group(2).strip()


def paginate(items: list[str], page: int = 1, per_page: int = 10) -> tuple[list[str], int]:
    """Return a page of items and the total page count."""
    per_page = max(1, per_page)
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    start = (page - 1) * per_page
    return items[start : start + per_page], total_pages


def split_target(raw: str) -> tuple[str, str]:
    """Split ``@user hello there`` into ``("@user", "hello there")``."""
    pieces = raw.strip().split(maxsplit=1)
    if not pieces:
        return "", ""
    return pieces[0], pieces[1] if len(pieces) > 1 else ""


def looks_like_command(text: str, prefix: str) -> bool:
    return strip_prefix(text, prefix) is not None


def is_emoji_only(text: str) -> bool:
    return bool(text) and bool(_TELEGRAM_EMOJI_ONLY.match(text))
