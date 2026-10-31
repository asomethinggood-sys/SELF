"""Every message that leaves her is a message Telegram will accept and a person can read.

The failures these tests hold are all of one family: the code *answered*, and the
answer arrived looking like machinery. An entity escaped twice shows the reader
``&amp;lt;b&amp;gt;Sara``; markdown written for a Markdown parser shows ``**okay**``
in a chat that renders HTML; a dropped variable leaves ``Quick action  removed.``;
a boolean printed raw says ``dry_run: False``.

Static tests never see any of that, because nothing throws. These check the bytes.
"""

from __future__ import annotations

import html

import pytest
from conftest import last_text

from havoc.services.actions import Turn
from havoc.services.tools import build_tools
from havoc.utils.formatting import bold, code, escape, italic, prepare
from havoc.utils.markup import rescue_inline, rich


# ---------------------------------------------------------------------------
# escaping is a fixed point
# ---------------------------------------------------------------------------
def test_escaping_twice_is_the_same_as_escaping_once() -> None:
    """``bold(escape(name))`` is what a hundred call sites wrote; it must not double-escape."""
    for raw in ("<b>Sara", "AT&T", "a & b", "&amp;", "&copy;", "plain", "{name}"):
        once = escape(raw)
        assert escape(once) == once, raw


@pytest.mark.parametrize("wrap", [bold, italic, code])
def test_a_wrapper_escapes_its_argument_exactly_once(wrap) -> None:
    assert wrap(escape("<b>Sara & <i>co</i>")) == wrap("<b>Sara & <i>co</i>")
    assert "&amp;lt;" not in wrap(escape("<b>Sara"))


def test_a_foreign_entity_is_still_escaped() -> None:
    """A person typing ``&copy;`` must see ``&copy;``, not a copyright sign."""
    assert escape("&copy;") == "&amp;copy;"


# ---------------------------------------------------------------------------
# markdown that reached an HTML chat
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**okay**", "<b>okay</b>"),
        ("_soft_", "<i>soft</i>"),
        ("~~gone~~", "<s>gone</s>"),
        ("`fal`", "<code>fal</code>"),
        ("[look](https://example.com)", '<a href="https://example.com">look</a>'),
    ],
)
def test_a_locale_string_written_in_markdown_still_renders(markdown: str, expected: str) -> None:
    assert rescue_inline(escape(markdown)) == expected


def test_rescue_leaves_code_alone() -> None:
    """A star inside ``<pre>`` is content, not half an italic run."""
    body = escape("see __this__") + "<pre>**not bold**</pre>"
    assert "<pre>**not bold**</pre>" in rescue_inline(body)


def test_a_kaomoji_is_not_an_italic_run() -> None:
    face = "╰(*´︶`*)╯"
    assert "*" in rescue_inline(face)


def test_a_fence_keeps_its_backticks_out_of_the_chat() -> None:
    out = prepare(rich("a fence:\n```python\nprint(1)\n```\nafter"))
    joined = "\n".join(out)
    assert "```" not in joined
    assert "<pre>" in joined


def test_three_blank_lines_collapse_to_one() -> None:
    assert prepare(rich("a\n\n\n\nb")) == ["a\n\nb"]


async def test_an_empty_answer_sends_nothing(runtime) -> None:
    """The sender refuses an empty body: an "answer" of no words is not one."""
    from havoc.services.sender import SendError

    with pytest.raises(SendError):
        await runtime.sender.send(100, "   ")


# ---------------------------------------------------------------------------
# commands that name what they need
# ---------------------------------------------------------------------------
async def test_quick_remove_without_a_name_asks_for_one(runtime) -> None:
    from conftest import invoke

    await invoke(runtime, ".quick remove")
    text = html.unescape(last_text(runtime))
    assert "Missing argument" in text
    assert "Quick action  removed" not in text


async def test_policy_without_a_person_asks_for_one(runtime) -> None:
    from conftest import invoke

    await invoke(runtime, ".policy ignore")
    text = html.unescape(last_text(runtime))
    assert "Missing argument" in text
    assert "called  —" not in text


async def test_the_config_card_says_on_and_off_not_python(runtime) -> None:
    from conftest import invoke

    await invoke(runtime, ".config")
    text = html.unescape(last_text(runtime))
    assert "False" not in text
    # The card is real HTML: the key is bold, so match the two halves.
    assert "dry_run" in text and ("off" in text.split("dry_run")[1] or "on" in text.split("dry_run")[1])
    assert "None" not in text


async def test_the_clock_card_renders_its_template(runtime) -> None:
    from conftest import invoke

    await invoke(runtime, ".clock status")
    text = html.unescape(last_text(runtime))
    assert "{time}" not in text


async def test_a_quoted_trigger_pattern_with_a_space_is_kept(runtime) -> None:
    """``.trigger add x "good morning" notify`` stored "good" and refused "morning"."""
    from conftest import invoke

    await invoke(runtime, '.trigger add probe "good morning" notify')
    await invoke(runtime, ".trigger list")
    text = html.unescape(last_text(runtime))
    assert "good morning" in text


# ---------------------------------------------------------------------------
# the toolbox validates what a model hands it
# ---------------------------------------------------------------------------
def _tool(runtime, name: str):
    return next(tool for tool in build_tools(runtime) if tool.name == name)


async def test_a_non_numeric_repeat_does_not_jam_send_message(runtime) -> None:
    runtime.reach.resolve = _never_resolves  # type: ignore[method-assign]
    tool = _tool(runtime, "send_message")
    turn = Turn(chat_id=100, actor_id=42, language="en")
    result = await tool.run(
        turn,
        tool.validate(
            {"to": "someone", "text": "hi", "repeat": "a few times", "delete_after": "soon"}
        ),
    )
    # It refuses the *recipient* (there is nobody called "someone"), not the
    # numbers: the arguments were read, not crashed on.
    assert result.ok is False
    assert "int(" not in str(result.detail)


async def _never_resolves(*_args, **_kwargs):
    return None


async def test_a_tool_card_escapes_what_the_model_wrote(runtime) -> None:
    from havoc import ui

    tool = _tool(runtime, "remember")
    turn = Turn(chat_id=100, actor_id=42, language="en")
    result = await tool.run(turn, tool.validate({"fact": "<b>Sara & <i>co</i>"}))
    rendered = ui.card(result.title, tuple(result.lines))
    assert "<b>Sara" not in rendered
    assert "&lt;b&gt;Sara" in rendered
