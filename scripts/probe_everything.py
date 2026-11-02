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
                # A name the account cannot find is a refusal, not a silent
                # failure: "I can't find probe-person in your chats" is the
                # honest answer to a round trip that named nobody real.
                "can't find",
                "cannot find",
                "don't know anyone",
                "do not know anyone",
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
    ("remember that i like tea", "note · i like tea"),
    ("yura unsend your last message", "unsend last"),
    ("yura mute this chat for an hour", "mute here 1h"),
    ("yura archive this chat", "archive here"),
    ("yura what can you do", "abilities"),
    ("یورا ساعت رو خاموش کن", "clock off"),
    ("یورا یادم بنداز ده دقیقه دیگه چای دم کنم", "remind 10m"),
    # The ones the audit found doing nothing at all: an order said out loud in a
    # chat with somebody else, with no name in it. Each must leave a trace.
    ("clear the history here", "clearhistory"),
    ("how many messages have i sent", "outbox"),
    ("who am i", "chatinfo"),
    ("make sara an admin", "promote · sara"),
    ("ban this person", "ban"),
    ("what did sara say yesterday", "read · sara"),
    ("every friday at 18:00 remind me to stretch", "remind weekly"),
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
    from conftest import FakeEvent, FakeMessage

    results: list[dict[str, Any]] = []
    for index, (sentence, must) in enumerate(SENTENCES):
        runtime = await build_runtime()
        try:
            before = len(runtime.client.sent)
            crash = None
            # Through the router, not straight into the engine: these are the
            # owner's *own* messages (that is how they arrive — the account is
            # his), and the bare-name conversion, the command path and the
            # persona gates only exist on the router's road.
            try:
                message = FakeMessage(
                    id=700 + index,
                    chat_id=100,
                    sender_id=Harness.OWNER,
                    text=sentence,
                    out=True,
                    is_private=True,
                    is_group=False,
                )
                await runtime.handlers.on_outgoing(FakeEvent(message, out=True))
                await runtime.handlers.on_incoming(FakeEvent(message, out=False))
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
                elif must == "remind weekly":
                    # A weekday on the sentence must survive as a weekday on the
                    # row: a one-shot reminder on the right day is still wrong,
                    # and so is a weekly one that drifts off Friday.
                    repeat = str(dict(reminders[0]).get("recurrence") or "")
                    if not repeat.startswith("weekly:"):
                        findings.append(f"recurrence:{repeat or 'none'}")
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



async def probe_surfaces(runtime: Any) -> list[dict[str, Any]]:
    """The ways into a command, not only its own name.

    An alias that resolves to the wrong command, a ``.help <name>`` that knows
    nothing about it, or a menu that advertises a command the registry does not
    have are all broken doors into a room that exists.
    """
    from conftest import invoke

    results: list[dict[str, Any]] = []
    registry = runtime.registry
    known = {command.name for command in registry.visible()}
    for command in registry.visible():
        for alias in command.aliases:
            resolved = registry.resolve(alias)
            entry: dict[str, Any] = {"surface": f"alias:{alias}", "of": command.name}
            if resolved is None or resolved.name != command.name:
                entry["findings"] = ["alias_misroutes"]
                results.append(entry)
                continue
            if not alias or not alias.strip("?!/") or alias.startswith(("?", "/")):
                # An alias made of punctuation is not dispatchable by a person
                # either; the resolver check above is the whole test for it.
                results.append(entry)
                continue
            sent = await invoke(runtime, f".{alias}", sender_id=Harness.OWNER)
            findings: list[str] = []
            for message in sent:
                if not (message.text or "").strip() and "file" in message.kwargs:
                    continue
                findings.extend(check_message(message.text))
            if not sent:
                # Aliases of commands that need arguments may refuse; a *silent*
                # alias is the bug.
                findings.append("silent")
            if findings:
                entry["findings"] = sorted(set(findings))
                entry["said"] = " ⏎ ".join(m.text.replace("\n", " / ") for m in sent)[:300]
            results.append(entry)

    for name in sorted(known):
        sent = await invoke(runtime, f".help {name}", sender_id=Harness.OWNER)
        entry = {"surface": f"help:{name}"}
        findings: list[str] = []
        text = " ".join(message.text for message in sent)
        for message in sent:
            if not (message.text or "").strip() and "file" in message.kwargs:
                continue
            findings.extend(check_message(message.text))
        if not sent:
            findings.append("silent")
        elif name.lower() not in text.lower():
            # The help screen for `.send` must mention `.send`.
            findings.append("does_not_name_itself")
        if findings:
            entry["findings"] = sorted(set(findings))
            entry["said"] = text.replace("\n", " / ")[:300]
        results.append(entry)
    return results


async def probe_groups(runtime: Any) -> list[dict[str, Any]]:
    """A room full of people: the rules he set are the rules that must hold.

    These are *his* requirements, not mine: her answer to a message that answers
    her is always sent, she answers in a group when she is named, and a rule he
    wrote about a person wins over all of it.
    """
    from conftest import FakeMessage, incoming

    results: list[dict[str, Any]] = []
    group = -1001234567890
    replies = runtime.persona_replies

    async def say(text: str, *, sender: int = Harness.OWNER, reply: Any = None) -> list[Any]:
        message = FakeMessage(
            id=random_id(),
            chat_id=group,
            sender_id=sender,
            text=text,
            out=sender == Harness.OWNER,
            reply=reply,
            is_private=False,
            is_group=True,
        )
        await incoming(runtime, message)
        return message

    async def sent_by(call) -> list[Any]:
        before = len(runtime.client.sent)
        await call()
        return runtime.client.sent[before:]

    def record(label: str, sent: list[Any], *, want: bool, extra: list[str] | None = None) -> None:
        findings: list[str] = []
        if want and not sent:
            findings.append("not_answered")
        if not want and sent:
            findings.append("answered_when_it_should_not")
        for message in sent:
            if not (message.text or "").strip() and "file" in message.kwargs:
                continue
            findings.extend(check_message(message.text))
        findings.extend(extra or [])
        results.append(
            {
                "group": label,
                "findings": sorted(set(findings)),
                "said": " ⏎ ".join(m.text.replace("\n", " / ") for m in sent)[:300],
            }
        )

    # 1. her name in a group is an invitation.
    replies._group_last.clear()
    replies._group_window.clear()
    sent = await sent_by(lambda: say("yura, hello there", sender=1234))
    record("named by a stranger", sent, want=True)

    # 2. a rule about that person wins: "don't answer him".
    replies._group_last.clear()
    replies._group_window.clear()
    await runtime.store.set_person_rule(1234, "ignore", chat_id=group, name="stranger")
    sent = await sent_by(lambda: say("yura, hello again", sender=1234))
    record("a rule he wrote about the person", sent, want=False)

    # 3. a reply to *her* message is always answered, even mid-cooldown.
    replies._group_last.clear()
    replies._group_window.clear()
    her_message = FakeMessage(id=random_id(), chat_id=group, sender_id=42, text="hm.", out=True, is_private=False, is_group=True)
    await replies._note_own(group, her_message.id)
    replies._group_last[group] = __import__("time").monotonic()
    answered = await sent_by(lambda: say("so?", sender=555, reply=her_message))
    record("a reply to her own message", answered, want=True)

    # 4. chatter that is not aimed at her stays his.
    replies._group_last.clear()
    replies._group_window.clear()
    sent = await sent_by(lambda: say("did you see the match last night", sender=555))
    record("a conversation between two people", sent, want=False)

    # 5. the actual bug he photographed: a stranger's sentence must not *do*
    #    anything. Not the clock, not a command, not a memory.
    replies._group_last.clear()
    replies._group_window.clear()
    clock_before = await runtime.store.get_bool("profile.clock", False)
    memory_before = len(await runtime.memory.recall(group))
    sent = await sent_by(lambda: say("yura turn the clock on", sender=666))
    problems: list[str] = []
    if await runtime.store.get_bool("profile.clock", False) != clock_before:
        problems.append("a_guest_ran_a_command")
    if len(await runtime.memory.recall(group)) != memory_before:
        problems.append("a_guest_wrote_a_memory")
    if list(getattr(runtime.yura_chat, "_intent_done", ())):
        problems.append("intent_ran_for_a_guest")
    record("a guest ordering her around", sent, want=True, extra=problems)
    return results


async def probe_ownership() -> list[dict[str, Any]]:
    """What the model is actually told about who is talking.

    The prompt is the only thing standing between a stranger and her account, so
    it is read here for the pieces that must never appear on a guest's turn — and
    for the pieces that must appear on his.
    """
    from conftest import FakeMessage, incoming

    class Recorder:
        available = True
        model = "probe"
        name = "probe"

        def __init__(self) -> None:
            self.calls: list[Any] = []
            self.reply = "ok"

        async def converse(self, messages, max_tokens: int = 400, **kwargs: Any) -> str:
            self.calls.append(list(messages))
            return self.reply

        async def complete(self, *args: Any, **kwargs: Any) -> str:
            return "ok"

    results: list[dict[str, Any]] = []
    stranger = 6714832104
    cases = (
        ("a stranger in a group", stranger, None, "guest"),
        ("the owner in a group", Harness.OWNER, None, "owner"),
        # The fail-open itself, before any room exists to read.
        ("nobody at all", None, "nobody", "guest"),
        # A caller that passes the wrong boolean must not be able to promote.
        ("a stranger a caller called the owner", stranger, "promote", "guest"),
    )
    for index, (label, sender, private, expected) in enumerate(cases):
        runtime = await build_runtime()
        provider = Recorder()
        runtime.ai = provider
        await runtime.feature_manager.set_enabled("persona", True)
        await runtime.memory.remember(Harness.OWNER, "he takes his coffee black")
        try:
            if private is None:
                message = FakeMessage(
                    id=9100 + index,
                    chat_id=group_chat(),
                    sender_id=sender,
                    text="yura hello",
                    is_group=True,
                )
                message.sender = type("Sender", (), {"first_name": "Reza", "id": sender, "username": "peza"})()
                await incoming(runtime, message)
            elif private == "nobody":
                await runtime.yura_chat.answer("hello", chat_id=group_chat())
            else:
                await runtime.yura_chat.answer(
                    "hello", chat_id=group_chat(), is_owner=True, sender_id=sender
                )
        finally:
            await runtime.db.close()
        prompt = str(provider.calls[-1][0].content) if provider.calls else ""
        findings: list[str] = []
        if not prompt:
            findings.append("no_prompt")
        guest_block = "this turn is not his" in prompt or "این پیام مالِ او نیست" in prompt
        if expected == "guest":
            if not guest_block:
                findings.append("guest_turn_read_as_the_owner")
            if "The hands you have" in prompt:
                findings.append("hands_promised_to_a_guest")
            if "coffee black" in prompt:
                findings.append("his_memory_leaked_to_a_guest")
            if "@edited_by_nyx" in prompt:
                findings.append("his_handle_leaked_to_a_guest")
        else:
            if guest_block:
                findings.append("owner_turn_read_as_a_guest")
            if "The hands you have" not in prompt:
                findings.append("owner_has_no_hands")
            if "coffee black" not in prompt:
                findings.append("owner_forgot_himself")
        results.append(
            {
                "who": label,
                "expected": expected,
                "findings": sorted(set(findings)),
                "prompt": prompt[-160:],
            }
        )
    return results


def group_chat() -> int:
    return -1001234567890


def random_id() -> int:
    """A fresh message id: the engine drops a repeated one as a duplicate."""
    global _NEXT_ID
    _NEXT_ID += 1
    return _NEXT_ID


_NEXT_ID = 20_000



async def probe_delivery(runtime: Any) -> list[dict[str, Any]]:
    """What actually happens to a message when it lands.

    Two failures look identical from inside a chat — "she ignored me" and "the
    process never saw it" — so this drives the real router and counts what the
    router did, instead of trusting a unit test that calls the same function by
    hand with a message built to pass.

    * every raw update used to log "connection lost" and "connection established"
      because the client hooked a catch-all ``events.Raw`` handler when Telethon
      turned out to have no ``Disconnect`` event; the flag flipped with it and
      the connection watcher reported disconnects for a socket that never moved.
    * a catch-up replay is a message that arrived while she was offline. It must
      be answered when it is seconds old and dropped when it is hours old.
    """
    import logging
    from datetime import UTC, datetime, timedelta

    from conftest import FakeMessage

    from havoc.features.persona import MIN_INTERVAL_SECONDS

    class _Sink(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    class _Event:
        """The handful of attributes ``Router._handle_outgoing`` reads."""

        def __init__(self, message: Any) -> None:
            self.message = message
            for name in ("chat_id", "id", "out", "sender_id", "is_private", "is_group",
                         "is_channel", "raw_text", "text", "media", "reply_to_msg_id",
                         "mentioned", "date"):
                setattr(self, name, getattr(message, name, None))

    results: list[dict[str, Any]] = []
    await runtime.feature_manager.set_enabled("persona", True)
    runtime.permissions.set_self_id(Harness.OWNER)
    sink = _Sink()
    runtime.logger.addHandler(sink)

    async def _say(text: str, *, minutes_old: int = 0, chat_id: int | None = None) -> int:
        before = len(runtime.client.sent)
        message = FakeMessage(
            id=random_id(),
            chat_id=chat_id or Harness.OWNER,
            sender_id=Harness.OWNER,
            text=text,
            out=True,
            is_private=True,
            is_group=False,
            date=datetime.now(UTC) - timedelta(minutes=minutes_old),
        )
        await runtime.handlers.on_outgoing(_Event(message))
        return len(runtime.client.sent) - before

    try:
        for _ in range(40):
            await _say("nothing to see here")
        noise = [line for line in sink.lines if "Telegram connection" in line]
        results.append(
            {
                "case": "40 raw updates through the router",
                "answered": 0,
                "findings": ["phantom connection noise"] if noise else [],
                "count": len(noise),
            }
        )

        sink.lines.clear()
        # Past the two-second valve between unsolicited answers. Forty messages in
        # a row is a firehose nobody types; waiting is what a person does.
        await asyncio.sleep(MIN_INTERVAL_SECONDS + 0.2)
        fresh = await _say("hey yura are you up")
        results.append(
            {
                "case": "a fresh line to her",
                "answered": fresh,
                "findings": [] if fresh else ["not answered"],
            }
        )

        # One message, one answer. His own chat is routed by two subscribers (the
        # self-chat handler and the private-chat one) and for a while both of them
        # spoke: two cards, two sentences, interleaved.
        await asyncio.sleep(MIN_INTERVAL_SECONDS + 0.2)
        ones = await _say("nothing much, just thinking out loud")
        results.append(
            {
                "case": "one message, one answer",
                "answered": ones,
                "findings": [] if ones == 1 else [f"{ones} answers to one message"],
            }
        )

        # An order with no name in it still executes. "how many messages have i
        # sent" is an instruction, not a remark, and it must not depend on her
        # being called first.
        await asyncio.sleep(MIN_INTERVAL_SECONDS + 0.2)
        before = len(runtime.client.sent)
        ordered = await _say("how many messages have i sent")
        said_order = " ".join(message.text for message in runtime.client.sent[before:]).lower()
        findings = []
        if "outbox" not in said_order:
            findings.append("the order did not run")
        if ordered != 1:
            findings.append(f"{ordered} answers")
        results.append(
            {
                "case": "an unnamed order in his own chat",
                "answered": ordered,
                "said": said_order[:120],
                "findings": findings,
            }
        )

        old = await _say("hey yura are you up", minutes_old=180)
        results.append(
            {
                "case": "the same line from three hours ago",
                "answered": old,
                "findings": [] if not old else ["a catch-up replay answered late"],
            }
        )

        # The connection flag, driven through the real service: connecting must
        # not install a catch-all handler, and the flag must follow the socket.
        from havoc.client import TelegramService

        class _Socket:
            def __init__(self) -> None:
                self.handlers: list[Any] = []
                self.live = True

            async def connect(self) -> None:
                return None

            def is_connected(self) -> bool:
                return self.live

            def add_event_handler(self, callback: Any, event: Any) -> None:
                self.handlers.append((callback, event))

        service = TelegramService(runtime.config, runtime.logger)
        socket = _Socket()
        service.client = socket
        service.connected = False
        await service.connect()
        results.append(
            {
                "case": "connecting the client",
                "handlers": len(socket.handlers),
                "findings": ["a catch-all handler was installed"] if socket.handlers else [],
            }
        )
        socket.live = False
        dropped = service.refresh_connection()
        results.append(
            {
                "case": "the socket dies",
                "connected": dropped,
                "findings": [] if dropped is False else ["still claims to be connected"],
            }
        )

        stale = "stale" if runtime.persona_replies.last_skip == "stale" else runtime.persona_replies.last_skip
        results.append(
            {
                "case": "the reason the old line was dropped",
                "reason": stale,
                "findings": [] if stale == "stale" else [f"dropped as {stale!r}"],
            }
        )
    finally:
        runtime.logger.removeHandler(sink)
    return results


async def probe_limits() -> list[dict[str, Any]]:
    """What he sees, and what the process does, when the model says no.

    "It just hit rate limit" and "the bot is dead" are the same silence from
    inside a chat, and this is the section that keeps them apart: the answer must
    say why, the model must not be asked again during the cooldown, and the
    things that never needed a model must still work.
    """
    from conftest import FakeEvent, FakeMessage

    from havoc.services.ai_provider import AIError

    class Limit:
        model = "probe-limit"
        available = True
        active_model = "probe-limit"

        def __init__(self, message: str) -> None:
            self.message = message
            self.calls = 0

        async def converse(self, *args: Any, **kwargs: Any) -> str:
            self.calls += 1
            raise AIError(self.message, kind="rate_limited")

        async def complete(self, *args: Any, **kwargs: Any) -> str:
            self.calls += 1
            raise AIError(self.message, kind="rate_limited")

    def _owner_line(text: str, index: int) -> Any:
        message = FakeMessage(
            id=9600 + index,
            chat_id=Harness.OWNER,
            sender_id=Harness.OWNER,
            text=text,
            out=True,
            is_private=True,
            is_group=False,
        )
        return FakeEvent(message, out=True)

    results: list[dict[str, Any]] = []

    for label, error, must in (
        ("a per-minute limit", "429: too many requests", "throttled"),
        ("a spent daily allowance", "every credential is spent or disabled", "allowance"),
    ):
        runtime = await build_runtime()
        provider = Limit(error)
        runtime.ai = provider
        await runtime.feature_manager.set_enabled("persona", True)
        try:
            before = len(runtime.client.sent)
            await runtime.handlers.on_outgoing(_owner_line("are you there", 0))
            said = [message.text for message in runtime.client.sent[before:]]
        finally:
            await runtime.db.close()
        findings: list[str] = []
        if not said:
            findings.append("silent")
        elif must not in " ".join(said).lower():
            findings.append("said nothing about the limit")
        if provider.calls != 1:
            findings.append(f"asked {provider.calls} times")
        results.append({"case": label, "said": " ⏎ ".join(said)[:200], "findings": findings})

    # A command does not need the model, so a spent allowance must not touch it.
    runtime = await build_runtime()
    provider = Limit("every credential is spent or disabled")
    runtime.ai = provider
    await runtime.feature_manager.set_enabled("persona", True)
    try:
        before = len(runtime.client.sent)
        await runtime.handlers.on_outgoing(_owner_line("who am i", 1))
        ran = list(getattr(runtime.yura_chat, "_intent_done", ()))
        said = [message.text for message in runtime.client.sent[before:]]
    finally:
        await runtime.db.close()
    findings = []
    if not ran:
        findings.append("the command did not run")
    if not said:
        findings.append("silent")
    results.append({"case": "a command while the model is spent", "did": ran, "findings": findings})
    return results

async def probe_downloader() -> list[dict[str, Any]]:
    """A link in Saved Messages, walked through the real subscriber.

    The feature's whole promise is behavioural — the video arrives and nothing
    else does — so this drives the account's own event path with the network
    stubbed at the boundary, then reads what the chat would have seen.
    """
    from conftest import FakeMessage

    from havoc.event_engine import normalize_message
    from havoc.services import downloader as service
    from havoc.services.downloader import Item, Quality

    def _item(url: str) -> Item:
        return Item(
            url=url,
            site="youtube",
            title="a probe video",
            uploader="probe",
            duration=90,
            thumbnail="https://img.example/t.jpg",
            qualities=[
                Quality(index=1, label="1080p", height=1080, filesize=50 * 1024 * 1024),
                Quality(index=2, label="720p", height=720, filesize=20 * 1024 * 1024),
                Quality(index=3, label="sound only", audio_only=True),
            ],
        )

    original = {name: getattr(service, name) for name in ("installed", "inspect", "fetch", "thumbnail", "discard")}
    state: dict[str, Any] = {"fetched": [], "asked": 0, "calls": 0}

    class Model:
        model = "probe-model"
        available = True
        active_model = "probe-model"

        async def converse(self, *args: Any, **kwargs: Any) -> str:
            state["asked"] += 1
            return "i would have described that video for you."

        async def complete(self, *args: Any, **kwargs: Any) -> str:
            state["asked"] += 1
            return "i would have described that video for you."

    async def _inspect(url: str, **_kwargs: Any) -> Item:
        return _item(url)

    async def _fetch(url: str, quality: Quality, **_kwargs: Any) -> Any:
        state["fetched"].append(quality.label)
        return f"/tmp/probe-{quality.index}.mp4"

    async def _thumb(url: str, **_kwargs: Any) -> bytes:
        return b"\x89PNG probe"

    service.installed = lambda: True  # type: ignore[assignment]
    service.inspect = _inspect  # type: ignore[assignment]
    service.fetch = _fetch  # type: ignore[assignment]
    service.thumbnail = _thumb  # type: ignore[assignment]
    service.discard = lambda path: None  # type: ignore[assignment]

    results: list[dict[str, Any]] = []
    try:
        runtime = await build_runtime()
        runtime.ai = Model()
        await runtime.feature_manager.set_enabled("downloader", True)
        await runtime.feature_manager.set_enabled("persona", True)
        owner = Harness.OWNER
        try:
            def _line(text: str, index: int) -> Any:
                message = FakeMessage(
                    id=9700 + index,
                    chat_id=owner,
                    sender_id=owner,
                    text=text,
                    out=True,
                    is_private=True,
                    is_group=False,
                )
                return normalize_message(message, self_id=owner, prefix=".")

            async def _push(text: str, index: int) -> None:
                await runtime.event_engine.emit(_line(text, index))

            # 1 — a link becomes a picture and a menu
            await _push("https://www.youtube.com/watch?v=probe1", 0)
            sent = [message.text for message in runtime.client.sent]
            files = [message for message in runtime.client.sent if "file" in message.kwargs]
            findings: list[str] = []
            joined = " ".join(sent)
            if not files:
                findings.append("no thumbnail")
            if "a probe video" not in joined:
                findings.append("no title")
            for expected in ("1080p", "720p", "sound only"):
                if expected not in joined:
                    findings.append(f"missing {expected}")
            if state["asked"]:
                findings.append("the model was asked about the link")
            results.append({"case": "a link in Saved Messages", "findings": findings, "said": joined[:160]})

            # 2 — the number is an order
            before = len(runtime.client.sent)
            await _push("2", 1)
            delivered = [
                message for message in runtime.client.sent[before:] if "file" in message.kwargs
            ]
            findings = []
            if state["fetched"] != ["720p"]:
                findings.append(f"fetched {state['fetched']}")
            if not delivered:
                findings.append("no file")
            if state["asked"]:
                findings.append("the model was asked about the pick")
            results.append(
                {"case": "the quality picked by number", "findings": findings, "said": str(state["fetched"])}
            )

            # 3 — a stranger's link is not an order
            state["fetched"].clear()
            stranger = FakeMessage(
                id=9799,
                chat_id=8899,
                sender_id=8899,
                text="https://www.youtube.com/watch?v=probe2",
                out=False,
                is_private=True,
            )
            await runtime.event_engine.emit(normalize_message(stranger, self_id=owner, prefix="."))
            findings = []
            if state["fetched"]:
                findings.append("downloaded for a stranger")
            if runtime.downloader.sessions:
                findings.append("a stranger opened a menu")
            results.append({"case": "a link from a stranger", "findings": findings})

            # 4 — cancel leaves nothing behind
            await _push("https://www.youtube.com/watch?v=probe3", 2)
            await _push("0", 3)
            findings = [] if not runtime.downloader.sessions else ["the menu outlived the cancel"]
            results.append({"case": "0 cancels", "findings": findings})
        finally:
            await runtime.db.close()
    finally:
        for name, value in original.items():
            setattr(service, name, value)
    return results


async def probe_voices() -> list[dict[str, Any]]:
    """The two personalities, seen from the outside.

    A switch that only changes a stored string is theatre. This asks the same
    question twice, once in each voice, and looks at what the model was actually
    told — plus the one path that must change with it: the answer when there is
    no model at all.
    """
    from conftest import FakeMessage

    from havoc import persona
    from havoc.event_engine import normalize_message

    class Model:
        model = "probe-model"
        available = True
        active_model = "probe-model"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def converse(self, messages: Any, **_kwargs: Any) -> str:
            self.prompts.append(str(getattr(messages[0], "content", messages[0])))
            return "fine."

        async def complete(self, messages: Any, **_kwargs: Any) -> str:
            return await self.converse(messages)

    results: list[dict[str, Any]] = []
    prompts: dict[str, str] = {}
    for mode in persona.MODES:
        runtime = await build_runtime()
        model = Model()
        runtime.ai = model
        await runtime.feature_manager.set_enabled("persona", True)
        try:
            await persona.set_mode(runtime.store, mode)
            message = FakeMessage(
                id=9800,
                chat_id=Harness.OWNER,
                sender_id=Harness.OWNER,
                text="how do i center a div",
                out=True,
                is_private=True,
            )
            await runtime.event_engine.emit(
                normalize_message(message, self_id=Harness.OWNER, prefix=".")
            )
            prompts[mode] = model.prompts[-1] if model.prompts else ""
            findings: list[str] = []
            if not model.prompts:
                findings.append("the model was never asked")
            if mode == "assistant":
                if "assistant mode" not in prompts[mode]:
                    findings.append("the prompt is not the assistant voice")
                if 'a little feral for him' in prompts[mode]:
                    findings.append("the egirl block leaked in")
            else:
                if "e-kitten" not in prompts[mode]:
                    findings.append("the prompt is not the egirl voice")
                if "assistant mode" in prompts[mode]:
                    findings.append("the assistant block leaked in")
            # The safety lines must survive both voices: a tone switch is not a
            # moral system.
            if "Never insult" not in prompts[mode]:
                findings.append("the safety rules are missing")
            results.append({"case": f"{mode} voice reaches the model", "findings": findings})
        finally:
            await runtime.db.close()

    if len(prompts) == 2 and prompts.get("assistant") == prompts.get("egirl"):
        results.append({"case": "the two voices differ", "findings": ["identical prompts"]})

    # The offline voice follows the switch too.
    plain = persona.assistant_line("en")
    findings = []
    if not plain:
        findings.append("assistant mode has no offline line")
    if any(glyph in plain for glyph in ("ᵕ", "╰", "♡", ">_<")):
        findings.append("the assistant fallback is wearing kaomoji")
    results.append({"case": "the offline answer keeps the voice", "findings": findings, "said": plain[:120]})
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
        surfaces = await probe_surfaces(runtime)
        groups = await probe_groups(await build_runtime())
        ownership = await probe_ownership()
        delivery = await probe_delivery(await build_runtime())
        limits = await probe_limits()
        downloader = await probe_downloader()
        voices = await probe_voices()
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
        "surfaces": surfaces,
        "groups": groups,
        "ownership": ownership,
        "delivery": delivery,
        "limits": limits,
        "downloader": downloader,
        "voices": voices,
    }
    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))

    bad_commands = [c for c in commands if c.get("findings") or c.get("crash")]
    bad_tools = [t for t in tools if t.get("findings") or t.get("crash")]
    bad_visuals = [v for v in visuals if v.get("findings")]
    bad_voice = [v for v in voice if v.get("findings")]
    bad_plain = [v for v in plain if v.get("findings") or v.get("crash")]
    bad_trips = [v for v in round_trips if v.get("findings") or v.get("crash")]
    bad_sentences = [v for v in sentences if v.get("findings") or v.get("crash")]
    bad_surfaces = [v for v in surfaces if v.get("findings") or v.get("crash")]
    bad_groups = [v for v in groups if v.get("findings") or v.get("crash")]
    bad_ownership = [v for v in ownership if v.get("findings") or v.get("crash")]
    bad_delivery = [v for v in delivery if v.get("findings") or v.get("crash")]
    bad_limits = [v for v in limits if v.get("findings") or v.get("crash")]
    bad_downloader = [v for v in downloader if v.get("findings") or v.get("crash")]
    bad_voices = [v for v in voices if v.get("findings") or v.get("crash")]

    for title, rows, key in (
        ("COMMANDS", bad_commands, "command"),
        ("TOOLS", bad_tools, "tool"),
        ("VISUALS", bad_visuals, "visual"),
        ("VOICE", bad_voice, "voice"),
        ("PLAIN", bad_plain, "plain"),
        ("ROUND TRIPS", bad_trips, "trip"),
        ("SENTENCES", bad_sentences, "sentence"),
        ("SURFACES", bad_surfaces, "surface"),
        ("GROUPS", bad_groups, "group"),
        ("OWNERSHIP", bad_ownership, "turn"),
        ("DELIVERY", bad_delivery, "case"),
        ("LIMITS", bad_limits, "case"),
        ("DOWNLOADER", bad_downloader, "case"),
        ("VOICES", bad_voices, "case"),
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
        f"{len(bad_sentences)}/{len(sentences)} sentences, "
        f"{len(bad_surfaces)}/{len(surfaces)} surfaces, {len(bad_groups)}/{len(groups)} group cases, "
        f"{len(bad_ownership)}/{len(ownership)} ownership turns, "
        f"{len(bad_delivery)}/{len(delivery)} delivery cases, "
        f"{len(bad_limits)}/{len(limits)} limit cases, "
        f"{len(bad_downloader)}/{len(downloader)} downloader cases, "
        f"{len(bad_voices)}/{len(voices)} voice cases"
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
            or bad_surfaces
            or bad_groups
            or bad_ownership
            or bad_delivery
            or bad_limits
            or bad_downloader
            or bad_voices
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
