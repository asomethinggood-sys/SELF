"""Model markdown, made safe for Telegram.

Every message a model writes is escaped first and *then* a fixed set of patterns
is recognised, so nothing a model (or a screenshot pasted into it) says can
introduce a tag. These tests hold both halves: the formatting renders, and the
formatting cannot be forged.
"""

from __future__ import annotations

import html

import pytest

from havoc.utils.markup import looks_formatted, plain_preview, rich


def test_bold_italic_and_code_render() -> None:
    assert rich("**loud**") == "<b>loud</b>"
    assert rich("__loud__") == "<b>loud</b>"
    assert rich("*soft*") == "<i>soft</i>"
    assert rich("_soft_") == "<i>soft</i>"
    assert rich("`cargo build`") == "<code>cargo build</code>"
    assert rich("~~gone~~") == "<s>gone</s>"
    assert rich("||secret||") == "<tg-spoiler>secret</tg-spoiler>"


def test_the_project_marks_render() -> None:
    assert rich("++under++") == "<u>under</u>"
    assert rich("^^hush^^") == "ʜᴜꜱʜ"  # the small-caps glyphs are the styling
    assert rich("%%spread%%") == "s p r e a d"


def test_a_fence_becomes_pre_not_a_paragraph_of_code_spans() -> None:
    out = rich("```python\nprint(1)\n```")
    assert out.startswith("<pre>") and "<code" in out
    assert "print(1)" in out


def test_a_quote_line_becomes_a_blockquote() -> None:
    out = rich("> the sea was angry")
    assert "<blockquote>" in out and "the sea was angry" in out


def test_a_whisper_is_collapsible() -> None:
    out = rich(">> come closer")
    assert "expandable" in out
    assert "come closer" in out


def test_a_heading_is_bold_and_a_list_is_a_list() -> None:
    assert "<b>Morning</b>" in rich("# Morning")
    assert "•" in rich("- one\n- two")
    assert "1." in rich("1. first\n2. second")


# ---------------------------------------------------------------------------
# The half that matters: it cannot be forged
# ---------------------------------------------------------------------------
def test_html_in_the_message_is_escaped_not_executed() -> None:
    assert "<script>alert(1)</script>" not in rich("<script>alert(1)</script>")
    assert "&lt;script&gt;" in rich("<script>alert(1)</script>")
    out = rich("<b>not bold</b> & <i>nope</i>")
    assert "<b>not bold</b>" not in out
    assert "&lt;b&gt;" in out


def test_a_fence_cannot_smuggle_a_tag() -> None:
    out = rich("```\n</pre><b>x</b>\n```")
    assert "</pre><b>" not in out
    assert "&lt;/pre&gt;" in out


def test_unbalanced_markers_stay_characters() -> None:
    assert rich("*unclosed") == "*unclosed"
    assert rich("2 * 3 * 4") == "2 * 3 * 4"


def test_persian_words_with_underscores_are_not_italics() -> None:
    assert rich("خیلی_خوب") == "خیلی_خوب"


def test_a_kaomoji_is_not_half_an_italic() -> None:
    """The owner's lines end with faces made of the same glyphs as markdown."""
    for kaomoji in ("╰(*´︶`*)╯", "(¬‿¬)", ">_<", "ᵕ̈", "<3"):
        # Escaped, never turned into a tag: what Telegram renders is the face.
        assert html.unescape(rich(kaomoji)) == kaomoji
        assert "<blockquote>" not in rich(kaomoji)


def test_text_with_nothing_to_format_is_unchanged() -> None:
    assert rich("just words") == "just words"
    assert rich("") == ""
    assert rich("سلام، خوبی؟") == "سلام، خوبی؟"


def test_rich_never_raises_and_never_leaves_a_stray_tag() -> None:
    nasty = "*a**b**c* ╰(`*´)╯ ``` < > & || ++ ^^ %%"
    out = rich(nasty)
    for tag in ("<b>", "<i>", "<code>", "<s>", "<u>", "<pre>", "<blockquote>"):
        assert out.count(tag) == out.count(tag.replace("<", "</"))


# ---------------------------------------------------------------------------
# Recognisers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["**hi**", "*hi*", "`code`", "~~s~~", "||s||", "++u++", "^^s^^", "%%s%%", "> q", "# h", "- b", "1. n"],
)
def test_markup_is_recognised(text: str) -> None:
    assert looks_formatted(text)


def test_plain_speech_is_not_marked_as_formatted() -> None:
    assert not looks_formatted("hello there")
    assert not looks_formatted("سلام")
    assert not looks_formatted("")


def test_the_preview_strips_no_words() -> None:
    preview = plain_preview("**bold** and *soft* and `code`")
    assert "bold" in preview and "soft" in preview and "code" in preview
