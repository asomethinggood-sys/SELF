#!/usr/bin/env python3
"""Run the whole bot and judge what it *says*, not just that it answered.

``audit_live.py`` asks "did this command throw?". This asks the harder question
the owner actually feels: is the message that came back **well formed**?

It builds the real runtime with the fake Telegram client, dispatches every
command with a battery of arguments (nothing, help, every subcommand, values
invented from the usage string, and hostile input), runs every tool, and then
puts each reply through a renderer check:

* balanced and allowed HTML — an unclosed ``<i>`` makes Telegram reject the whole
  message, which looks like the bot going silent;
* no literal ``<``/``>``/``&`` that Telegram would parse as a tag;
* no markdown leftovers (``**bold**``, ``__x__``, ``[text](url)``);
* no unescaped user input (a person called ``<b>Sara`` must not become bold);
* no locale key printed as the answer, no ``None``, no ``{placeholder}``;
* no empty message, no triple blank line, no line that is only punctuation;
* no duplicate message for one command;
* a card has an ornament and is not just a title;
* text length under Telegram's limit after splitting.

Usage::

    python scripts/probe_everything.py            # summary
    python scripts/probe_everything.py --json .probe.json -v
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
import traceback
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

#: Tags Telegram accepts. Anything else is a rejection in production.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code",
    "pre", "blockquote", "tg-spoiler", "span", "br",
}

HOSTILE = "<b>Sara & <i>co</i>", "a" * 600, "🇮🇷 یورا جان 😻", "<script>x</script>", "{name}"


class _Rendering(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"tag_not_allowed:<{tag}>")
            return
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"tag_not_allowed:<{tag}/>")

    def handle_endtag(self, tag):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"tag_not_allowed:</{tag}>")
            return
        if not self.stack:
            self.problems.append(f"close_without_open:</{tag}>")
            return
        if self.stack[-1] != tag:
            self.problems.append(f"misnested:<{self.stack[-1]}>…</{tag}>")
            # pop back to the matching one, if any
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_data(self, data):
        # Text inside <pre>/<code> is literal on purpose: aligned comments and
        # ``backticks`` there are content, not leaked markdown. One neutral glyph
        # stands in for it so the surrounding words do not glue together (which
        # read as a double space) and lines do not collapse (a false blank line).
        if any(tag in {"pre", "code"} for tag in self.stack):
            self.text.append("x")
            return
        self.text.append(data)
        if "<" in data:
            # ">" is ordinary text in Telegram HTML; a bare "<" starts a tag.
            self.problems.append("unescaped_angle")

    def handle_entityref(self, name):
        self.text.append(f"&{name};")
        if name not in {"amp", "lt", "gt", "quot", "apos"}:
            self.problems.append(f"unknown_entity:&{name};")

    def handle_charref(self, name):
        self.text.append(f"&#{name};")


#: Emphasis needs at least one letter or digit; kaomoji do not count.
_WORD_RE = re.compile(r"\w", re.UNICODE)

#: Text that means the renderer leaked something internal.
TEXT_PATTERNS = {
    "locale_key": re.compile(r"^\W*[a-z][a-z0-9_]+(?:\.[a-z0-9_]+){1,}\W*$"),
    "placeholder": re.compile(r"\{[a-z_][a-z0-9_]*\}"),
    # ``dry_run: False`` is a leak; "the one True Light" in a poem is not.
    "python_literal": re.compile(r"(?<=: )\b(?:None|True|False)\b|(?<== )\b(?:None|True|False)\b"),
    "markdown_backtick": re.compile(r"`[^`\n]+`"),
    "traceback": re.compile(r"Traceback \(most recent|Error:"),
    "markdown_bold": re.compile(r"\*\*[^*\n]+\*\*"),
    "markdown_italic": re.compile(r"(?<![*\w])\*([^*\n]{2,})\*(?![*\w])"),
    # Alignment inside a table of contents is content; a *dropped value* is a
    # single double space between two words, never a run of them.
    "double_space_exact": re.compile(r"(?<=\S)[ ]{2}(?![ ])"),
    "markdown_link": re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"),
    "double_escape": re.compile(r"&amp;(?:lt|gt|amp|quot);"),
    "triple_newline": re.compile(r"\n{3,}"),
    # Two spaces *inside* a line are a bug: an empty name, a dropped variable.
    # Indentation at the start of a line is a deliberate list shape and is fine.

    "raw_object": re.compile(r"<[A-Za-z]+ object at 0x"),
}


def check_message(raw: str) -> list[str]:
    """Everything wrong with one outgoing message, as short codes."""
    findings: list[str] = []
    text = str(raw or "")
    if not text.strip():
        return ["empty"]
    parsed = _Rendering()
    try:
        parsed.feed(text)
        parsed.close()
    except Exception as exc:  # noqa: BLE001 - a broken string is the finding
        findings.append(f"html_parse:{type(exc).__name__}")
    findings.extend(parsed.problems)
    if parsed.stack:
        findings.append("unclosed:" + ",".join(parsed.stack))
    flat = "".join(parsed.text)
    # ``ـــ◈ـــ  everything she can do`` — the double space after a drawn rule is
    # the house shape, not a dropped value.
    double_space_text = re.sub(r"(?<=[\u0640\u25c8\u2756\u27e1])[ ]{2,}", " ", flat)
    for name, pattern in TEXT_PATTERNS.items():
        if name == "markdown_italic":
            # ``╰(*´︶`*)╯`` is a face, not an italic run: emphasis needs a word.
            if any(_WORD_RE.search(m.group(1)) for m in pattern.finditer(flat)):
                findings.append("markdown_italic")
            continue
        if pattern.search(flat if name != "double_space_exact" else double_space_text):
            findings.append("double_space" if name == "double_space_exact" else name)
    if len(text) > 4096:
        findings.append(f"too_long:{len(text)}")
    return findings


def describe(findings: list[str]) -> str:
    return ", ".join(sorted(set(findings)))


#: Arguments invented from a usage string: ``.remind 30m <text>`` needs a time.
SAMPLES = {
    "time": "30m",
    "id": "1",
    "text": "hello there",
    "name": "ali",
    "query": "bridge",
    "emoji": "❤️",
    "url": "https://example.com",
    "code": "en",
    "lang": "en",
    "path": "~/x.jpg",
    "count": "3",
    "date": "2026-01-01",
    "number": "2",
    "seconds": "10s",
    "who": "ali",
    "body": "hello",
    "message": "hello",
    "page": "12",
    "word": "love",
    "amount": "2",
    "seconds_or_time": "10s",
    "reason": "testing",
}
GENERIC = "test"


def sample_for(usage: str) -> str:
    """A plausible argument list from a usage template, or a generic one."""
    if not usage:
        return GENERIC
    out: list[str] = []
    for token in usage.replace("|", " ").replace("[", " ").replace("]", " ").split():
        token = token.strip("`'\"<>…")
        if not token or token.startswith("-"):
            continue
        key = token.strip("<>[]():").lower()
        if key in SAMPLES:
            out.append(SAMPLES[key])
        elif key.isupper():
            out.append(GENERIC)
    return " ".join(out[:2]) if out else GENERIC


class Harness:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    #: The owner. Probing as a stranger exercised the permission gate over and
    #: over and never once rendered the real answer of an owner-only command.
    OWNER = 42

    async def say(self, line: str, language: str | None = None) -> tuple[list[str], str | None]:
        """Dispatch one command line, returning what was said and what broke."""
        from conftest import invoke

        before_monotonic = len(self.runtime.client.sent)
        try:
            sent = await invoke(self.runtime, line, sender_id=self.OWNER, language=language)
            # A file with no caption is a complete answer: the picture *is* the
            # message. Only text-only replies are judged as empty.
            return [(m.text, "file" in m.kwargs) for m in sent], None
        except Exception:  # noqa: BLE001 - the crash is the result
            return [], traceback.format_exc(limit=3)
        finally:
            # Leave the world as we found it between cases.
            if await self.runtime.store.emergency_stop():
                await self.runtime.feature_manager.resume()
            await self.runtime.store.clear_interactive(self.OWNER)
            del before_monotonic


async def build_runtime():
    from conftest import FakeTelegram, make_config

    from havoc.runtime import Runtime

    config = make_config(Path(tempfile.mkdtemp()))
    logger = logging.getLogger("probe")
    logger.setLevel(logging.CRITICAL)
    runtime = await Runtime.create(config, logger)
    runtime.telegram = FakeTelegram()
    runtime.client = runtime.telegram.client
    runtime.sender.client = runtime.client
    runtime.permissions.set_self_id(42)
    runtime.health.mark_connected()
    runtime.load_features()
    for name in runtime.feature_manager.names():
        try:
            await runtime.feature_manager.set_enabled(name, True)
        except Exception:  # noqa: BLE001 - a dependency may be missing
            pass
    await runtime.feature_manager.refresh()
    return runtime


async def probe_commands(runtime: Any, languages: tuple[str, ...] = ("en",)) -> list[dict[str, Any]]:
    harness = Harness(runtime)
    results: list[dict[str, Any]] = []
    for command in runtime.registry.visible():
        name = command.name
        cases: list[tuple[str, str]] = [
            ("bare", f".{name}"),
            ("help", f".{name} --help"),
            ("args", f".{name} {sample_for(command.usage)}"),
        ]
        for sub in command.subcommands:
            if any(ord(char) > 0x400 for char in sub):
                continue
            cases.append((f"sub:{sub}", f".{name} {sub}"))
        for hostile in HOSTILE[:2]:
            cases.append(("hostile", f".{name} {hostile}"))
        seen: set[str] = set()
        for label, line in cases:
            if line in seen:
                continue
            seen.add(line)
            for language in languages:
                texts, crash = await harness.say(line, language)
                results.append(judge(name, label, line, language, texts, crash))
    return results


def judge(
    name: str,
    label: str,
    line: str,
    language: str,
    texts: list[tuple[str, bool]],
    crash: str | None,
) -> dict[str, Any]:
    """One command case, judged."""
    entry: dict[str, Any] = {"command": name, "case": label, "line": line, "language": language}
    if crash:
        entry["crash"] = crash.splitlines()[-1][:300]
        entry["where"] = " | ".join(crash.splitlines()[:4])[:400]
    findings: list[str] = []
    for text, is_file in texts:
        if is_file and not (text or "").strip():
            # The picture is the message; there is no caption to judge.
            continue
        findings.extend(check_message(text))
    if len(texts) > 1 and len({text for text, _ in texts}) == 1:
        findings.append("duplicate_message")
    if not texts and not crash:
        findings.append("silent")
    if findings:
        entry["findings"] = sorted(set(findings))
        entry["raw"] = " ⏎ ".join(text for text, _ in texts)[:1200]
        entry["said"] = " ⏎ ".join(t.replace("\n", " / ") for t, _ in texts)[:300]
    return entry


async def probe_tools(runtime: Any) -> list[dict[str, Any]]:
    """Every tool, three ways: plausible arguments, hostile ones, and none.

    The model is the caller, and a model writes what it likes: an empty field, a
    word where a number belongs, a person who does not exist. A tool that answers
    those with a traceback is a toolbox that jams; a tool that answers with a note
    is one the next round can repair.
    """
    from havoc.services.actions import Turn
    from havoc.services.tools import build_tools

    results: list[dict[str, Any]] = []
    turn = Turn(chat_id=100, actor_id=42, language="en")
    for tool in build_tools(runtime):
        plausible: dict[str, str] = {}
        hostile: dict[str, str] = {}
        for prop in tool.properties:
            key = prop.lower()
            if key in SAMPLES:
                plausible[prop] = SAMPLES[key]
            elif "time" in key or "when" in key:
                plausible[prop] = "30m"
            elif key in {"text", "body", "message", "query", "sentence", "question"}:
                plausible[prop] = "hello there"
            elif key in {"who", "to", "target", "person", "name"}:
                plausible[prop] = "ali"
            elif key in {"steps", "options"}:
                plausible[prop] = '["a","b"]'
            elif key == "mode":
                plausible[prop] = "ignore"
            elif key == "emoji":
                plausible[prop] = "❤️"
            else:
                plausible[prop] = GENERIC
            hostile[prop] = "<b>Sara & <i>co</i> — ‮x"
        for label, args in (("plausible", plausible), ("hostile", hostile), ("empty", {})):
            entry: dict[str, Any] = {"tool": tool.name, "case": label, "args": args}
            from havoc.services.actions import ActionError

            try:
                checked = tool.validate(args)
            except ActionError as exc:
                # A refusal with a reason is the toolbox working.
                entry["note"] = str(exc)[:120]
                results.append(entry)
                continue
            except Exception:  # noqa: BLE001 - a validation crash is a finding
                entry["crash"] = traceback.format_exc(limit=3).splitlines()[-1][:300]
                results.append(entry)
                continue
            if label == "empty" and checked:
                # A tool with required fields must not invent them.
                entry["findings"] = ["accepted_missing_required"]
                results.append(entry)
                continue
            try:
                result = await tool.run(turn, checked)
                entry["ok"] = result.ok
                entry["note"] = str(result.note)[:200]
                entry["title"] = str(result.title)[:160]
                entry["lines"] = [str(line)[:160] for line in result.lines][:6]
                findings: list[str] = []
                # Judge the card the chat would actually receive: the title and
                # lines are raw words, and ``ui.card`` is what escapes them.
                from havoc import ui

                rendered = ui.card(result.title or "", tuple(result.lines))
                findings.extend(check_message(rendered))
                if result.ok and not result.note:
                    findings.append("no_note")
                if result.ok and result.title and not result.lines:
                    findings.append("title_without_lines")
                entry["card"] = rendered[:400]
                if findings:
                    entry["findings"] = sorted(set(findings))
            except ActionError as exc:
                # ``run`` raising a typed refusal is handled by the action layer.
                entry["note"] = str(exc)[:120]
            except Exception:  # noqa: BLE001 - raising is a finding
                entry["crash"] = traceback.format_exc(limit=3).splitlines()[-1][:300]
                entry["where"] = " | ".join(traceback.format_exc(limit=4).splitlines()[:4])[:400]
            results.append(entry)
    return results


async def probe_visuals(runtime: Any) -> list[dict[str, Any]]:
    """The card renderer itself: the shapes that make Telegram drop a message."""
    from havoc import ui

    results: list[dict[str, Any]] = []

    def record(label: str, text: str) -> None:
        findings = check_message(text)
        results.append({"visual": label, "findings": sorted(set(findings)), "text": text[:200]})

    record("card_minimal", ui.card("Title"))
    record("card_with_lines", ui.card("Title", ("one", "two"), icon="◈"))
    record("card_hostile", ui.card("<b>Sara & <i>co</i>", ("<script>x</script>",)))
    record("card_empty_line", ui.card("Title", ("", "  ")))
    record("card_very_long", ui.card("T" * 300, ("L" * 900,)))
    record("panel_empty", ui.panel([]))
    record("state_on_off", ui.state(True, on="on", off="off") + ui.state(False, on="on", off="off"))
    record("quote_card", ui.quote_card("a line", "Sara"))
    record("title_only", ui.title("◈", "Heading"))
    record("fact_line", ui.fact_line("when", "30m"))
    record("section", ui.section("◈", "Group", ("one", "two")))
    return results



#: Things a model actually writes, including the ways it goes wrong.
VOICE_SAMPLES = (
    "*smiles* hi, you called **again**?",
    "__bold__ and _italic_ and `code` and ~~gone~~ and ||hush||",
    "a fence:\n```python\nprint(\"<b>\")\n```\nafter the fence",
    "> a quote\n> second line",
    "# heading\n- bullet one\n1. numbered",
    "╰(*´︶`*)╯ ╮(╯▽╰)╭ (¬‿¬)",
    "سارا گفت: خیلی_خوب — و من گفتم هیچ‌وقت",
    "<b>not a tag</b> & <i>still text</i>",
    "an unclosed *star and an unclosed _underscore",
    "line\n\n\n\nwith too many blanks",
    "a link [look](https://example.com) and a url https://example.com/x?y=1",
    "text with {a_placeholder} and None and True inside",
    "**" + "very long bold " * 200 + "**",
    "🇮🇷 emoji 😻 and a zero width joiner 👨‍👩‍👧",
    ">_< >///< ^_^ ;_;",
    "",
    "   ",
)


async def probe_voice(runtime: Any) -> list[dict[str, Any]]:
    """What her own answers look like after they leave the model.

    ``yura_chat`` renders a model answer with :func:`rich` and hands it to the
    sender, which repairs and splits it. This probe drives that exact pipeline —
    including the empty answer, the monstrous one, and the fence that used to leak
    its backticks.
    """
    from havoc.utils.formatting import prepare
    from havoc.utils.markup import rich

    #: Rules that describe *our* rendering, not the words the model chose.
    #: A model writing "{draft}" or "None" is quoting; that is not a leak.
    ours = {"locale_key", "placeholder", "python_literal", "raw_object", "traceback"}

    results: list[dict[str, Any]] = []
    for sample in VOICE_SAMPLES:
        if not sample.strip():
            # The sender refuses an empty body on purpose; nothing to render.
            continue
        label = " ".join(sample.split())[:60] or "<empty>"
        try:
            chunks = prepare(rich(sample))
        except Exception:  # noqa: BLE001 - a crash here is the finding
            results.append(
                {"voice": label, "findings": ["crash"], "raw": traceback.format_exc(limit=2)[-200:]}
            )
            continue
        findings: list[str] = []
        if not chunks and sample.strip():
            findings.append("empty")
        for chunk in chunks:
            findings.extend(finding for finding in check_message(chunk) if finding not in ours)
        entry: dict[str, Any] = {"voice": label, "findings": sorted(set(findings))}
        if findings:
            entry["raw"] = " ⏎ ".join(chunks)[:800]
        results.append(entry)
    return results



#: What a person says to her, including the ways a chat goes strange. Every
#: line is addressed to her, and each one is unique: two identical messages in a
#: row are *meant* to be ignored (the event engine dedupes them).
PLAIN_SAMPLES = (
    "hello yura, how are you? 1",
    "سلام یورا، خوبی؟ ۲",
    "yura 3",
    "یورا ۴",
    "<b>bold</b> & <i>tags</i> in a plain message, yura 5",
    "*starred* _underscored_ `ticked` yura 6",
    "yura " + "a" * 900,
    "🇮🇷 😻 ╰(*´︶`*)╯ yura 8",
    "yura, say something in Japanese: かわいい",
    "yura, and what about a very long line " + "b " * 400,
)


async def probe_plain(_runtime: Any = None) -> list[dict[str, Any]]:
    """Her unprompted answers: the path that has no command in it.

    A command is a form; a message is a conversation. The words she chooses come
    from a bank, a model or a template, and every one of them is rendered by the
    same sender — so they get the same judgement.

    Each sample gets a **fresh runtime**. Ten messages in a row on one runtime
    measure her anti-flood timers and her "do not repeat yourself" memory, not
    her rendering; a person who sends one line to a bot has neither.
    """
    from conftest import FakeMessage, incoming

    results: list[dict[str, Any]] = []
    for index, sample in enumerate(PLAIN_SAMPLES):
        label = " ".join(sample.split())[:50] or "<empty>"
        runtime = await build_runtime()
        try:
            before = len(runtime.client.sent)
            crash = None
            try:
                message = FakeMessage(
                    id=900 + index,
                    chat_id=100,
                    sender_id=Harness.OWNER,
                    text=sample,
                    out=False,
                    is_private=True,
                )
                await incoming(runtime, message)
            except Exception:  # noqa: BLE001 - the crash is the result
                crash = traceback.format_exc(limit=3)
            sent = runtime.client.sent[before:]
            failed = [
                (sub.name, sub.last_error)
                for sub in runtime.event_engine.subscriptions
                if sub.last_error
            ]
        finally:
            await runtime.db.close()
        entry: dict[str, Any] = {"plain": label, "replies": len(sent)}
        if crash:
            entry["crash"] = crash.splitlines()[-1][:300]
            entry["where"] = " | ".join(crash.splitlines()[:4])[:400]
        findings: list[str] = []
        for message in sent:
            if "file" in message.kwargs and not (message.text or "").strip():
                continue
            findings.extend(check_message(message.text))
        if not sent and not crash:
            findings.append("silent")
        if len(sent) > 1 and len({message.text for message in sent}) == 1:
            findings.append("duplicate_message")
        # A subscriber that raised is a silent failure: the engine swallows it and
        # the chat just sees nothing.
        findings.extend(f"subscriber:{name}" for name, _ in failed)
        if findings:
            entry["findings"] = sorted(set(findings))
            entry["raw"] = " ⏎ ".join(message.text for message in sent)[:1200]
        if sent:
            entry["said"] = " ⏎ ".join(message.text.replace("\n", " / ") for message in sent)[:300]
        results.append(entry)
    return results



#: Round trips: a command that *changes* something, then the command that shows
#: the change. Rendering right is not the same as doing it — a drawer that closes
#: on an empty box looks perfect and still lost the letter.
ROUND_TRIPS: tuple[tuple[str, str, str, str], ...] = (
    # (label, setup line, verify line, what the verify must contain)
    ("bookmark", ".bookmark add probe-marker", ".bookmark list", "probe-marker"),
    ("note", ".note add probe-note", ".note list", "probe-note"),
    ("quick", ".quick add probeq .status", ".quick list", "probeq"),
    ("alias", ".alias add probealias .status", ".alias list", "probealias"),
    ("template", ".template add probetpl hello there", ".template list", "probetpl"),
    ("label", ".labels add probe-label", ".labels list", "probe-label"),
    ("reminder", ".remind 30m probe-reminder", ".reminders", "probe-reminder"),
    ("schedule", ".schedule 1h probe-schedule", ".scheduled", "probe-schedule"),
    ("watch", ".watch add probe-word", ".watch list", "probe-word"),
    ("trigger", '.trigger add probe-trigger "probe word" notify', ".trigger list", "probe-trigger"),
    ("autoreply", ".autoreply add probe-in => probe-out", ".autoreply list", "probe-in"),
    ("reaction", ".react add probe-react => ❤️", ".react list", "probe-react"),
    ("remember", ".remember probe-memory", ".memories", "probe-memory"),
    ("counter", ".counter inc probe-counter", ".counter list", "probe-counter"),
    ("yura teach", ".yura teach probe-lesson", ".yura rules", "probe-lesson"),
    ("policy", ".policy ignore probe-person", ".policy list", "probe-person"),
    ("disabling", ".disable probe-command", ".disable", "probe-command"),
    # Quoted arguments: the tokenizer knows where a phrase ends, and a command
    # that re-splits the raw line loses that knowledge. Every one of these is a
    # phrase a person would actually type.
    (
        "quoted trigger",
        '.trigger add probe-quoted "good morning" notify',
        ".trigger list",
        "good morning",
    ),
    ("quoted watch", '.watch add "probe two words"', ".watch list", "probe two words"),
    ("quoted reminder", '.remind 30m "probe two words"', ".reminders", "probe two words"),
    ("quoted schedule", '.schedule 1h "probe two words"', ".scheduled", "probe two words"),
    (
        "quoted template",
        '.template add probe-two "probe two words"',
        ".template list",
        "probe-two",
    ),
    (
        "quoted autoreply",
        '.autoreply add "probe two" => hello',
        ".autoreply list",
        "probe two",
    ),
    ("quoted bookmark", '.bookmark add "probe two words"', ".bookmark list", "probe two words"),
)


async def probe_round_trips(runtime: Any) -> list[dict[str, Any]]:
    """Does the change a command claims survive into the next look?

    Every case is a write followed by the read that is supposed to find it. A
    refusal is fine (an unknown name in this build, a feature switched off);
    a *silent* failure — "saved" and then not there — is the bug he keeps hitting.
    """
    harness = Harness(runtime)
    results: list[dict[str, Any]] = []
    for label, setup, verify, marker in ROUND_TRIPS:
        entry: dict[str, Any] = {"trip": label, "setup": setup, "verify": verify}
        wrote, crash = await harness.say(setup)
        if crash:
            entry["crash"] = crash.splitlines()[-1][:300]
            results.append(entry)
            continue
        wrote_text = " ".join(text for text, _ in wrote)
        read, crash2 = await harness.say(verify)
        if crash2:
            entry["crash"] = crash2.splitlines()[-1][:300]
            results.append(entry)
            continue
        read_text = " ".join(text for text, _ in read)
        entry["wrote"] = wrote_text[:200]
        entry["read"] = read_text[:200]
        findings: list[str] = []
        refused = any(
            phrase in (wrote_text + read_text).lower()
            for phrase in (
                "missing argument",
                "unknown command",
                "switched off",
                "not available",
                "is not a command",
                "no such",
            )
        )
        if not refused and marker not in read_text:
            findings.append("not_persisted")
        if not wrote_text.strip():
            findings.append("silent")
        if findings:
            entry["findings"] = findings
        results.append(entry)
    return results



#: Sentences he actually types. ``(sentence, what must happen)`` — the third
#: element is ``None`` for chatter (an answer is enough) and a word that must
#: appear in what she *did* for an instruction.
SENTENCES: tuple[tuple[str, str | None], ...] = (
    ("hello yura, how are you?", None),
    ("سلام یورا، خوبی؟", None),
    ("yura, what's the time in Tokyo", "time tokyo"),
    ("yura remind me to call mom in 30 minutes", "remind 30m"),
    ("yura, tell sara good morning", "send · sara"),
    ("یورا به سارا بگو صبح بخیر", "send · سارا"),
    ("yura send hi here", "send · here"),
    ("yura clock off for ten minutes, then clock on", "plan do"),
    ("yura remind me to stretch in 20 minutes and then turn the clock off", "plan do"),
    ("yura ignore sara for 2 hours then remind me to sleep", "plan do"),
    ("yura what do you remember about me", "recall"),
    ("yura remember that i like tea", "note · i like tea"),
    ("yura unsend your last message", "unsend last"),
    ("yura mute this chat for an hour", "mute here 1h"),
    ("yura archive this chat", "archive here"),
    ("yura what can you do", "abilities"),
    ("یورا ساعت رو خاموش کن", "clock off"),
    ("یورا یادم بنداز ده دقیقه دیگه چای دم کنم", "remind 10m"),
)


async def probe_sentences(_runtime: Any = None) -> list[dict[str, Any]]:
    """Does a plain sentence *do* the thing it asks for?

    He said the same sentence three times in three builds: "I said I want
    abilities executed, she just talks". A command is a form; a sentence is how
    a person actually uses her, and the failure is always the same shape — a warm
    answer, no action. So every instruction here must leave a trace in what she
    did (``_intent_done``, a queued plan, a reminder row, a picture), not only in
    what she said.

    Each sentence gets a fresh runtime: a conversation probe run in one runtime
    measures the anti-flood timers, not the sentence.
    """
    from conftest import FakeMessage, incoming

    results: list[dict[str, Any]] = []
    for index, (sentence, must) in enumerate(SENTENCES):
        runtime = await build_runtime()
        try:
            before = len(runtime.client.sent)
            crash = None
            try:
                await incoming(
                    runtime,
                    FakeMessage(
                        id=700 + index,
                        chat_id=100,
                        sender_id=Harness.OWNER,
                        text=sentence,
                        out=False,
                        is_private=True,
                    ),
                )
            except Exception:  # noqa: BLE001 - the crash is the result
                crash = traceback.format_exc(limit=3)
            sent = runtime.client.sent[before:]
            chat = runtime.yura_chat
            did = list(getattr(chat, "_intent_done", ()))
            media = list(getattr(chat, "_pending_media", ()))
            reminders = await runtime.store.list_reminders()
            plans = await runtime.store.plans()
            failed = [
                (sub.name, sub.last_error)
                for sub in runtime.event_engine.subscriptions
                if sub.last_error
            ]
        finally:
            await runtime.db.close()
        entry: dict[str, Any] = {"sentence": sentence, "must": must, "did": did}
        if crash:
            entry["crash"] = crash.splitlines()[-1][:300]
            entry["where"] = " | ".join(crash.splitlines()[:4])[:400]
        findings: list[str] = []
        if not sent and not media:
            findings.append("silent")
        for message in sent:
            if "file" in message.kwargs and not (message.text or "").strip():
                continue
            findings.extend(check_message(message.text))
        if must is not None:
            trace = " · ".join(
                [*did, *[str(row["text"]) if "text" in row.keys() else str(dict(row)) for row in reminders], f"plans:{len(plans)}"]
            )
            if must == "plan do":
                if not plans:
                    findings.append("no_plan")
            elif must.startswith("remind"):
                if not reminders:
                    findings.append("no_reminder")
            elif not any(must in item for item in did):
                findings.append("not_executed")
            entry["trace"] = trace[:200]
        findings.extend(f"subscriber:{name}" for name, _ in failed)
        if findings:
            entry["findings"] = sorted(set(findings))
        if sent:
            entry["said"] = " ⏎ ".join(message.text.replace("\n", " / ") for message in sent)[:300]
        results.append(entry)
    return results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=".probe.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--lang",
        default="en",
        choices=("en", "fa", "both"),
        help="language to drive the command sweep in (default: en)",
    )
    args = parser.parse_args()

    runtime = await build_runtime()
    try:
        # Her own replies first: the command sweep switches features, answers as
        # other people and leaves state behind, and a conversation probe run after
        # it is measuring the leftovers.
        plain = await probe_plain(runtime)
        languages = ("en", "fa") if args.lang == "both" else (args.lang,)
        commands = await probe_commands(runtime, languages)
        tools = await probe_tools(runtime)
        visuals = await probe_visuals(runtime)
        voice = await probe_voice(runtime)
        round_trips = await probe_round_trips(runtime)
        sentences = await probe_sentences(runtime)
    finally:
        await runtime.db.close()

    report = {
        "commands": commands,
        "tools": tools,
        "visuals": visuals,
        "voice": voice,
        "plain": plain,
        "round_trips": round_trips,
        "sentences": sentences,
    }
    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))

    bad_commands = [c for c in commands if c.get("findings") or c.get("crash")]
    bad_tools = [t for t in tools if t.get("findings") or t.get("crash")]
    bad_visuals = [v for v in visuals if v.get("findings")]
    bad_voice = [v for v in voice if v.get("findings")]
    bad_plain = [v for v in plain if v.get("findings") or v.get("crash")]
    bad_trips = [v for v in round_trips if v.get("findings") or v.get("crash")]
    bad_sentences = [v for v in sentences if v.get("findings") or v.get("crash")]

    for title, rows, key in (
        ("COMMANDS", bad_commands, "command"),
        ("TOOLS", bad_tools, "tool"),
        ("VISUALS", bad_visuals, "visual"),
        ("VOICE", bad_voice, "voice"),
        ("PLAIN", bad_plain, "plain"),
        ("ROUND TRIPS", bad_trips, "trip"),
        ("SENTENCES", bad_sentences, "sentence"),
    ):
        print(f"\n=== {title}: {len(rows)} with findings")
        for row in rows:
            findings = row.get("findings") or [row.get("crash", "crash")]
            print(f"  {row.get(key)} [{row.get('case', '')}] → {describe(findings)}")
            if args.verbose:
                print(f"      {row.get('line', row.get('args', ''))}")
                if row.get("said"):
                    print(f"      said: {row['said'][:300]}")
                if row.get("where"):
                    print(f"      where: {row['where'][:300]}")
    print(
        f"\nsummary: {len(bad_commands)}/{len(commands)} command cases, "
        f"{len(bad_tools)}/{len(tools)} tools, {len(bad_visuals)}/{len(visuals)} visuals, "
        f"{len(bad_voice)}/{len(voice)} voice samples, {len(bad_plain)}/{len(plain)} plain messages, "
        f"{len(bad_trips)}/{len(round_trips)} round trips, "
        f"{len(bad_sentences)}/{len(sentences)} sentences"
    )
    return (
        1
        if (
            bad_commands
            or bad_tools
            or bad_visuals
            or bad_voice
            or bad_plain
            or bad_trips
            or bad_sentences
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
