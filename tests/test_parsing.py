"""Command parsing: prefix rules, quotes, flags, aliases."""

from __future__ import annotations

import pytest

from havoc.utils.parsing import (
    ParseError,
    demote_subcommand,
    paginate,
    parse_command,
    parse_keyword_list,
    parse_labels,
    parse_pair,
    strip_prefix,
    tokenize,
)


def test_non_commands_are_rejected() -> None:
    for text in ("hello", "how are you?", "", ". leading dot", "...", ". "):
        assert strip_prefix(text, ".") is None
        assert parse_command(text, ".") is None


def test_simple_command() -> None:
    parsed = parse_command(".ping", ".")
    assert parsed is not None
    assert parsed.name == "ping"
    assert parsed.subcommand is None
    assert parsed.args == ()


def test_subcommand_and_args() -> None:
    parsed = parse_command(".autoreply add hello => Hi there", ".")
    assert parsed is not None
    assert parsed.name == "autoreply"
    assert parsed.subcommand == "add"
    # ``args`` excludes the subcommand, ``raw_args`` keeps the whole remainder.
    assert parsed.args == ("hello", "=>", "Hi", "there")
    assert parsed.rest == ("hello", "=>", "Hi", "there")
    assert parsed.joined_rest() == "hello => Hi there"
    assert parsed.raw_args == "add hello => Hi there"
    assert parsed.rest_raw == "hello => Hi there"


def test_quoted_arguments() -> None:
    parsed = parse_command('.template add party "Hello  friend"', ".")
    assert parsed is not None
    assert parsed.args == ("party", "Hello  friend")


def test_flags_extraction() -> None:
    parsed = parse_command(".remind 30m water --repeat daily --chat", ".")
    assert parsed is not None
    assert parsed.flags == {"repeat": "daily", "chat": "true"}
    # Without knowing the command, the parser tentatively treats "30m" as a
    # subcommand; the dispatcher demotes it because remind declares none.
    assert parsed.subcommand == "30m"
    assert parsed.args == ("water",)
    demoted = demote_subcommand(parsed)
    assert demoted.subcommand is None
    assert demoted.args == ("30m", "water")
    assert demoted.rest == ("30m", "water")
    assert demoted.raw_args == "30m water"


def test_custom_prefix() -> None:
    parsed = parse_command("!status", "!")
    assert parsed is not None and parsed.name == "status"
    assert parse_command(".status", "!") is None


def test_persian_command_names() -> None:
    parsed = parse_command(".زبان fa", ".")
    assert parsed is not None
    assert parsed.name == "زبان"
    assert parsed.subcommand == "fa"
    assert parsed.args == ()
    assert parsed.joined_args() == ""


def test_require_arg_raises() -> None:
    parsed = parse_command(".calc", ".")
    assert parsed is not None
    with pytest.raises(ParseError):
        parsed.require_arg(0, "expression")


def test_tokenize_falls_back_on_bad_quotes() -> None:
    assert tokenize('a "b c') == ["a", "b", "c"]


def test_keyword_and_label_parsing() -> None:
    assert parse_keyword_list("a, b ; a\nc") == ["a", "b", "c"]
    assert parse_labels("Work Stuff, personal") == ["work-stuff", "personal"]


def test_parse_pair() -> None:
    assert parse_pair("name=value") == ("name", "value")
    assert parse_pair("name: value") == ("name", "value")
    with pytest.raises(ParseError):
        parse_pair("nope")


def test_paginate() -> None:
    items = [str(i) for i in range(25)]
    page, pages = paginate(items, 2, 10)
    assert pages == 3
    assert page == [str(i) for i in range(10, 20)]
    page, pages = paginate(items, 99, 10)
    assert page == items[20:]
