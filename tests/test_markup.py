"""Markdown from the model, turned into Telegram HTML — safely.

The bug this guards against: her answers were correct markdown and the owner saw
asterisks, because everything went through ``escape()``. The danger in fixing it
is the opposite one — letting message text inject markup, or turning a kaomoji
into half an italic tag. Both directions are tested here.
"""

from __future__ import annotations

import re

import pytest

from havoc.utils.markup import BULLET, looks_formatted, plain_preview, rich


# ---------------------------------------------------------------------------
# The formatting the owner asked for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("**bold**", "<b>bold</b>"),
        ("__bold__", "<b>bold</b>"),
        ("*italic*", "<i>italic</i>"),
        ("_italic_", "<i>italic</i>"),
        ("~~gone~~", "<s>gone</s>"),
        ("||secret||", "<tg-spoiler>secret</tg-spoiler>"),
        ("`code`", "<code>code</code>"),
        ("# Headline", "<b>Headline</b>"),
        ("## Two", "<b>Two</b>"),
        ("- one\n- two", f"{BULLET} one\n{BULLET} two"),
        ("1. one\n2. two", "1. one\n2. two"),
    ],
)
def test_it_renders_what_the_model_writes(raw: str, expected: str) -> None:
    assert rich(raw) == expected


def test_a_quote_becomes_a_blockquote() -> None:
    assert rich("> be quiet") == "<blockquote>be quiet</blockquote>"


def test_consecutive_quote_lines_share_one_blockquote() -> None:
    assert rich("> first\n> second") == "<blockquote>first\nsecond</blockquote>"


def test_a_quote_interrupted_starts_a_new_block() -> None:
    rendered = rich("> one\nnot a quote\n> two")
    assert rendered.count("<blockquote>") == 2
    assert rendered.count("</blockquote>") == 2


def test_a_fenced_block_keeps_its_language_and_its_asterisks() -> None:
    rendered = rich("```python\nx = *p\n```")
    assert rendered == '<pre><code class="language-python">x = *p</code></pre>'


def test_a_fence_without_a_language_is_plain_pre() -> None:
    assert rich("```\na * b\n```") == "<pre>a * b</pre>"


def test_markdown_inside_code_is_not_formatting() -> None:
    assert rich("`**not bold**`") == "<code>**not bold**</code>"


def test_a_bullet_may_be_a_star_without_becoming_italic() -> None:
    assert rich("* item") == f"{BULLET} item"


# ---------------------------------------------------------------------------
# Staying safe and staying readable
# ---------------------------------------------------------------------------
def test_html_in_the_message_cannot_become_markup() -> None:
    rendered = rich('<b>fake</b> & <script>x</script>')
    assert "<b>" not in rendered
    assert "&lt;b&gt;" in rendered
    assert "&amp;" in rendered


def test_an_unbalanced_star_stays_a_star() -> None:
    assert rich("2 * 3 = 6") == "2 * 3 = 6"
    assert rich("*half open") == "*half open"


def test_persian_underscores_are_not_italics() -> None:
    assert rich("خیلی_خوب") == "خیلی_خوب"
    assert rich("اسمم یورا_ست") == "اسمم یورا_ست"


def test_a_kaomoji_is_never_mangled() -> None:
    """Offline lines end like this; the markers must not pair up across them."""
    kaomoji = "╰(*´︶`*)╯"
    assert rich(kaomoji) == kaomoji
    rendered = rich(f"متن یه چیز می‌گم {kaomoji}")
    assert rendered.endswith(kaomoji)
    assert "<i>" not in rendered


def test_a_real_sentence_next_to_a_kaomoji_still_formats() -> None:
    rendered = rich("**hey** ╰(*´︶`*)╯")
    assert "<b>hey</b>" in rendered
    assert rendered.endswith("╰(*´︶`*)╯")


def test_every_tag_it_opens_it_closes() -> None:
    sample = (
        "# title\n**bold** and *italic* and `code`\n> quote\n- bullet\n"
        "```py\nprint('x')\n```\n~~s~~ ||sp||"
    )
    rendered = rich(sample)
    for tag in ("b", "i", "code", "pre", "blockquote", "s", "tg-spoiler"):
        opened = len(re.findall(rf"<{tag}[ >]", rendered))
        assert opened == rendered.count(f"</{tag}>"), tag
    # No stray markers survive except inside code, where they are content.
    assert not re.search(r"(?<!code>)\*\*", rendered)


def test_empty_text_is_empty() -> None:
    assert rich("") == ""


def test_it_never_raises_on_odd_input() -> None:
    for sample in ("*", "**", "`", "```", "> ", "#", "||", "~~", "\x00", "＿"):
        assert isinstance(rich(sample), str)


# ---------------------------------------------------------------------------
# The helpers next to it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("**bold**", True),
        ("# heading", True),
        ("- item", True),
        ("1. item", True),
        ("> quote", True),
        ("`code`", True),
        ("just words", False),
        ("", False),
        ("خیلی_خوب", False),
    ],
)
def test_it_knows_when_there_is_something_to_render(text: str, expected: bool) -> None:
    assert looks_formatted(text) is expected


def test_plain_preview_strips_the_tags_it_added() -> None:
    preview = plain_preview("**bold** and *italic*")
    assert "<" not in preview
    assert "bold" in preview and "italic" in preview


def test_plain_preview_honours_the_limit() -> None:
    assert len(plain_preview("x" * 200, limit=10)) == 10
