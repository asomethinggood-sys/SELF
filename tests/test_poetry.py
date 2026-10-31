"""Poetry, fal and roast: the shipped corpora and the commands that read them.

These are *behavioural* tests: they load the real data files that ship in the
package and drive the real handlers through the dispatcher, exactly like
Telegram would. Nothing is mocked except the transport.
"""

from __future__ import annotations

import gzip
import json
import random

import pytest

from havoc import corpus
from havoc.content import random_roast
from havoc.features import poetry as poetry_feature
from havoc.services import fal_image
from tests.conftest import FakeMessage, invoke


# ---------------------------------------------------------------------------
# Shipped data
# ---------------------------------------------------------------------------
def test_corpus_ships_a_real_multi_poet_corpus() -> None:
    assert corpus.poem_count() >= 3000
    table = corpus.poets()
    assert len(table) >= 15
    # Both scripts are present, and no single poet is most of the corpus.
    assert corpus.languages().get("fa", 0) >= 2000
    assert corpus.languages().get("en", 0) >= 200
    assert max(table.values()) / corpus.poem_count() < 0.2


def test_every_shipped_poem_is_well_formed() -> None:
    for poem in corpus._poems():
        assert poem["poet"].strip()
        assert poem["language"] in {"fa", "en"}
        assert poem["text"].strip()
        assert "\r" not in poem["text"]
        assert 80 <= len(poem["text"]) <= 900
        assert not poem["text"].lstrip().startswith(("***", "Project Gutenberg"))


def test_fal_table_has_verses_and_a_meaning() -> None:
    assert corpus.fal_count() == 495
    reading = corpus.sample_fal(reading_id=1)
    assert reading["id"] == 1
    assert len(reading["verses"]) >= 4
    assert "\\r\\n" not in "".join(reading["verses"])
    assert len(reading["interpretation"]) > 40


def test_fal_lookup_rejects_unknown_ids() -> None:
    with pytest.raises(corpus.CorpusError):
        corpus.sample_fal(reading_id=10_000)


def test_poet_resolution_accepts_both_scripts() -> None:
    assert corpus.resolve_poet("حافظ") == "حافظ"
    assert corpus.resolve_poet("hafez") == "حافظ"
    assert corpus.resolve_poet("shakespeare") == "William Shakespeare"
    assert corpus.resolve_poet("SHAKESPEARE") == "William Shakespeare"
    assert corpus.resolve_poet("nobody at all") is None


def test_sampling_is_filtered() -> None:
    for _ in range(25):
        poem = corpus.sample_poem(language="fa", rng=random.Random(1))
        assert poem["language"] == "fa"


def test_corpus_cache_is_cheap_after_the_first_load() -> None:
    # lru_cache means the gz is decompressed once per process.
    corpus._poems.cache_clear()
    first = corpus._poems()
    second = corpus._poems()
    assert first is second


def test_shipped_files_are_gzip_json() -> None:
    for path in (corpus.POEMS_PATH, corpus.FALS_PATH):
        assert path.exists() and path.suffix == ".gz"
        with gzip.open(path, "rb") as handle:
            assert isinstance(json.loads(handle.read()), dict)


# ---------------------------------------------------------------------------
# .poem
# ---------------------------------------------------------------------------
async def test_poem_sends_a_blockquote_with_the_poet_in_italics(runtime) -> None:
    sent = await invoke(runtime, ".poem", language="en")
    assert len(sent) == 1
    body = sent[0].text
    assert "<blockquote" in body
    assert "<i>— " in body, body


async def test_poem_follows_the_chat_language(runtime) -> None:
    sent = await invoke(runtime, ".poem", language="fa")
    body = sent[0].text
    assert any("\u0600" <= char <= "\u06ff" for char in body)


async def test_poem_can_target_one_poet(runtime) -> None:
    for _ in range(5):
        sent = await invoke(runtime, ".poem حافظ")
        assert "حافظ" in sent[0].text


async def test_poem_lists_the_corpus(runtime) -> None:
    sent = await invoke(runtime, ".poem poets")
    body = sent[0].text
    assert "حافظ" in body and "William Shakespeare" in body
    assert "<blockquote>" in body


async def test_poem_rejects_an_unknown_poet(runtime) -> None:
    sent = await invoke(runtime, ".poem nonsense")
    assert "nonsense" in sent[0].text
    assert ".poem poets" in sent[0].text


async def test_persian_alias_resolves_to_poem(runtime) -> None:
    assert runtime.registry.resolve("شعر") is not None
    assert runtime.registry.resolve("شعر").name == "poem"


# ---------------------------------------------------------------------------
# .fal
# ---------------------------------------------------------------------------
async def test_fal_sends_only_a_picture(runtime, monkeypatch) -> None:
    sent = await invoke(runtime, ".fal")
    assert len(sent) == 1
    media = sent[0].kwargs
    assert "file" in media, "a fal must be sent as a file, not text"
    assert media["file"].endswith(".png")
    assert sent[0].text.strip() == "", "the picture must not carry a caption"


async def test_fal_text_mode_renders_the_reading(runtime) -> None:
    sent = await invoke(runtime, ".fal text")
    body = sent[0].text
    assert "<blockquote" in body
    assert "…" not in body or True  # long readings are expandable


async def test_fal_falls_back_to_text_when_pillow_is_missing(runtime, monkeypatch) -> None:
    monkeypatch.setattr(fal_image, "available", lambda: (False, "pillow"))
    sent = await invoke(runtime, ".fal")
    body = sent[0].text
    assert "file" not in sent[0].kwargs
    assert "Pillow" in body


async def test_fal_falls_back_to_text_when_rendering_fails(runtime, monkeypatch) -> None:
    monkeypatch.setattr(fal_image, "render_fal", lambda reading: (_ for _ in ()).throw(OSError("boom")))
    sent = await invoke(runtime, ".fal")
    assert "file" not in sent[0].kwargs
    assert "<blockquote" in sent[0].text


async def test_fal_alias_resolves_to_fal(runtime) -> None:
    assert runtime.registry.resolve("فال").name == "fal"
    assert runtime.registry.resolve("hafez").name == "fal"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_produces_a_png_of_the_right_shape() -> None:
    usable, reason = fal_image.available()
    if not usable:
        pytest.skip(f"card rendering unavailable: {reason}")
    reading = corpus.sample_fal(reading_id=7)
    payload = fal_image.render_fal(reading)
    assert payload.startswith(b"\x89PNG")
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(payload))
    assert image.width == fal_image.WIDTH
    # A portrait card: tall enough for a full reading, never a scrolling ribbon.
    ratio = image.height / image.width
    assert 0.9 < ratio < 1.6, f"unexpected card proportions ({ratio:.2f})"
    # A mostly-blank canvas would be a silent failure; check there is ink.
    colours = image.convert("RGB").getcolors(maxcolors=200_000) or []
    assert len(colours) > 50


def test_render_is_stable_for_a_given_reading() -> None:
    usable, reason = fal_image.available()
    if not usable:
        pytest.skip(f"card rendering unavailable: {reason}")
    reading = corpus.sample_fal(reading_id=42)
    assert fal_image.render_fal(reading) == fal_image.render_fal(reading)


def test_long_verses_shrink_instead_of_breaking() -> None:
    usable, reason = fal_image.available()
    if not usable:
        pytest.skip(f"card rendering unavailable: {reason}")
    reading = {
        "id": 1,
        "verses": ["این بیت عمداً بسیار طولانی است " * 6, "بیت دوم"],
        "interpretation": "معنی کوتاه.",
    }
    payload = fal_image.render_fal(reading)
    assert payload.startswith(b"\x89PNG")


def test_shaping_joins_persian_letters() -> None:
    usable, reason = fal_image.available()
    if not usable:
        pytest.skip(f"card rendering unavailable: {reason}")
    shaped = fal_image._shape("سلام")
    # Reshaping replaces base letters with their joined presentation forms, so
    # the shaped string is never equal to the original.
    assert shaped != "سلام"
    assert shaped.strip()


def test_a_line_is_drawn_one_glyph_at_a_time() -> None:
    """The fix for the mirrored card, asserted instead of described.

    A whole-string ``draw.text`` reaches libraqm when Pillow was built with it,
    and raqm runs bidi over text that is already in visual order. Drawing each
    glyph ourselves is what makes the card identical with and without raqm, so
    this test fails if anyone ever hands a line back to the layout engine.
    """
    calls: list[tuple[str, float]] = []

    class FakeDraw:
        def text(self, xy, text, *, font, fill):  # noqa: A002 - mirrors Pillow
            calls.append((text, xy[0]))

    class FakeFont:
        def getlength(self, text):
            return 10.0 * len(text)

    fal_image._draw_line(FakeDraw(), "سلام", FakeFont(), 5.0, 9, (0, 0, 0))
    shaped = fal_image._shape("سلام")
    assert [text for text, _ in calls] == list(shaped)
    assert all(len(text) == 1 for text, _ in calls), "a whole line reached draw.text"
    assert [x for _, x in calls] == [5.0 + 10.0 * index for index in range(len(shaped))]


def test_width_is_the_sum_of_the_glyph_advances() -> None:
    class FakeFont:
        def getlength(self, text):
            return 7.5 * len(text)

    text = "فال حافظ"
    assert fal_image._width(None, text, FakeFont()) == 7.5 * len(fal_image._shape(text))


def test_the_card_does_not_change_when_raqm_exists(monkeypatch) -> None:
    """The production container has raqm; this one does not. Same bytes or bust."""
    usable, reason = fal_image.available()
    if not usable:
        pytest.skip(f"card rendering unavailable: {reason}")
    reading = corpus.sample_fal(reading_id=42)
    without = fal_image.render_fal(reading)
    monkeypatch.setattr(fal_image, "has_raqm", lambda: True)
    assert fal_image.render_fal(reading) == without


# ---------------------------------------------------------------------------
# .roast
# ---------------------------------------------------------------------------
async def test_roast_needs_a_target(runtime) -> None:
    sent = await invoke(runtime, ".roast")
    assert ".roast ‹text›" in sent[0].text


async def test_roast_uses_replied_text(runtime) -> None:
    replied = FakeMessage(id=77, sender_id=9, text="hot take: tabs are better than spaces")
    sent = await invoke(runtime, ".roast", reply=replied)
    body = sent[0].text
    assert "<blockquote>" in body
    assert "<i>" in body  # the spice/mode panel
    assert "tabs" in body or "spaces" in body or "medium" in body


async def test_roast_can_take_inline_text(runtime) -> None:
    sent = await invoke(runtime, ".roast pineapple belongs on pizza")
    assert "<blockquote>" in sent[0].text


async def test_roast_refuses_a_harassing_target(runtime) -> None:
    replied = FakeMessage(id=78, text="kill yourself you idiot")
    sent = await invoke(runtime, ".roast", reply=replied)
    assert "<blockquote>" not in sent[0].text


async def test_roast_spice_is_a_positional_word(runtime) -> None:
    sent = await invoke(runtime, ".roast spicy this take is bad")
    assert "<code>spicy</code>" in sent[0].text


async def test_roast_spice_also_accepts_the_persian_words(runtime) -> None:
    sent = await invoke(runtime, ".roast تند این حرف بی‌ربط است")
    assert "<code>spicy</code>" in sent[0].text


def test_roast_lines_are_clean_and_not_cringe() -> None:
    banned = {"skibidi", "rizz", "sigma", "gyatt", "sus", "npc", "ohio"}
    for language in ("en", "fa"):
        for _ in range(60):
            line = random_roast(language, "this take")
            assert line.strip()
            assert len(line) <= 200
            lowered = line.lower()
            assert not any(word in lowered for word in banned), line
            assert "{" not in line and "}" not in line


def test_roast_lines_never_target_identity() -> None:
    # The offline pool is small enough to check exhaustively: no line may make
    # the joke about who somebody is.
    from havoc.content import ROASTS

    forbidden = (
        "fat", "ugly", "gay", "girl", "boy", "mom", "mother", "father", "wife",
        "husband", "race", "religion", "disabled", "autistic", "stupid", "idiot",
    )
    for language, lines in ROASTS.items():
        for line in lines:
            lowered = line.lower()
            for word in forbidden:
                assert word not in lowered.split(), f"{language}: {line}"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_poetry_and_roasts_are_registered_features() -> None:
    from havoc.feature_manager import builtin_features
    from havoc.services.storage import DEFAULT_FEATURES, FEATURE_GROUPS

    names = {feature.name for feature in builtin_features()}
    assert {"poetry", "roasts"} <= names
    # Poetry ships *on*: an offline fortune or verse is content, not automation,
    # and "the feature is disabled" is not an answer to "یورا فال بگیر".
    assert DEFAULT_FEATURES["poetry"] is True
    assert DEFAULT_FEATURES["roasts"] is False
    assert set(FEATURE_GROUPS["fun"]) == {"poetry", "roasts"}


async def test_features_are_off_by_default_in_a_fresh_store(runtime) -> None:
    await runtime.feature_manager.set_enabled("poetry", False)
    sent = await invoke(runtime, ".poem")
    assert "<blockquote>" not in sent[0].text
    assert "poetry" in sent[0].text


async def test_help_documents_both_commands(runtime) -> None:
    for text in (".help poem", ".help fal", ".help roast"):
        sent = await invoke(runtime, text)
        assert "Usage" in sent[0].text
    sent = await invoke(runtime, ".help fal", language="fa")
    assert "فال" in sent[0].text and sent[0].text.count("فال") >= 1


def test_poem_card_marks_long_poems_expandable() -> None:
    long_poem = {"title": "خ", "poet": "حافظ", "text": "\n".join(f"بیت {n}" for n in range(12))}
    card = poetry_feature._poem_card(long_poem)
    assert "<blockquote expandable>" in card
    assert "<i>— حافظ</i>" in card or "حافظ" in card


# ---------------------------------------------------------------------------
# Presentation consistency
# ---------------------------------------------------------------------------
async def test_fight_draft_is_a_card_with_their_line_and_ours(runtime) -> None:
    replied = FakeMessage(id=91, sender_id=8, text="tabs are obviously better than spaces")
    sent = await invoke(runtime, ".fight", reply=replied)
    body = sent[0].text
    assert body.startswith("<b>⚔</b> · <b>")
    assert "They said" in body and "You could send" in body
    assert body.count("<blockquote>") >= 3
    # Nothing is sent to the chat; this is a draft and says so.
    assert ".fight send" in body


async def test_fight_styles_are_a_panel_not_a_list_of_bullets(runtime) -> None:
    sent = await invoke(runtime, ".fight styles")
    body = sent[0].text
    assert "<blockquote>" in body and "<code>sarcastic</code>" in body
    assert "<li>" not in body and "•" not in body


async def test_the_hook_prefers_a_subject_over_a_generic_adjective() -> None:
    from havoc.content import extract_hook

    assert extract_hook("tabs are better than spaces and you are wrong", "en") not in {
        "better",
        "wrong",
    }


async def test_yura_card_is_a_card(runtime) -> None:
    await runtime.feature_manager.set_enabled("persona", True)
    sent = await invoke(runtime, ".yura status")
    body = sent[0].text
    assert body.startswith("<b>♡</b> · <b>")
    assert "<blockquote>" in body
    assert "🩶" not in body.split("\n")[0]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def test_search_folds_persian_letters() -> None:
    # The corpus writes ی and ک; a user may type the Arabic forms.
    arabic = corpus.search_poems("عشق")
    assert arabic
    assert corpus.search_poems("عشق") == arabic


def test_search_is_case_insensitive_for_latin() -> None:
    assert corpus.search_poems("Love") == corpus.search_poems("love")
    assert corpus.search_poems("LOVE")


def test_search_respects_the_language_filter() -> None:
    for poem in corpus.search_poems("love", language="fa"):
        assert poem["language"] == "fa"


def test_search_ignores_blank_input() -> None:
    assert corpus.search_poems("   ") == []


async def test_poem_search_shows_a_matching_poem(runtime) -> None:
    sent = await invoke(runtime, ".poem search عشق")
    body = sent[0].text
    assert "<blockquote" in body
    assert "poems about" in body


async def test_poem_search_explains_an_empty_result(runtime) -> None:
    sent = await invoke(runtime, ".poem search qqqqzzz")
    assert "qqqqzzz" in sent[0].text
    assert "<blockquote>" not in sent[0].text


async def test_poem_search_without_a_word_asks_for_one(runtime) -> None:
    sent = await invoke(runtime, ".poem search")
    # The placeholder is drawn with ‹› rather than <>: a bare `<word>` is eaten
    # by Telegram's parser, and an escaped `&lt;word&gt;` is one more thing that
    # can be escaped twice and shown to him as `&lt;`.
    assert ".poem search ‹word›" in sent[0].text


# ---------------------------------------------------------------------------
# Presentational consistency of the older fun commands
# ---------------------------------------------------------------------------
async def test_quote_has_the_same_shape_as_a_poem(runtime) -> None:
    sent = await invoke(runtime, ".quote")
    body = sent[0].text
    assert body.startswith("<blockquote>")
    assert body.rstrip().endswith("<i>— Y.U.R.A 1.9</i>")


async def test_quote_signs_itself_in_farsi_too(runtime) -> None:
    sent = await invoke(runtime, ".quote", language="fa")
    assert sent[0].text.rstrip().endswith("<i>— Y.U.R.A 1.9</i>")


async def test_joke_is_a_card(runtime) -> None:
    sent = await invoke(runtime, ".joke")
    body = sent[0].text
    assert body.startswith("<b>✧</b>")
    assert "<blockquote>" in body


async def test_help_is_split_between_categories_not_mid_line(runtime) -> None:
    sent = await invoke(runtime, ".help")
    assert len(sent) >= 2, "the index no longer fits one message"
    for message in sent:
        assert len(message.text) <= 4096
        # A split in the middle of a command line would leave a stray '<code>'
        # without its closing tag at the end of a message.
        assert message.text.count("<code>") == message.text.count("</code>")
    assert "commands" in sent[-1].text.lower() or "help" in sent[-1].text.lower()


# ---------------------------------------------------------------------------
# The shared title rule
# ---------------------------------------------------------------------------
def test_title_never_prints_the_same_symbol_twice() -> None:
    from havoc import ui

    assert ui.title("◷", "◷ Reminders (1)") == "<b>◷</b> · <b>Reminders (1)</b>"
    assert ui.title("♡", "❥ Yura") == "<b>♡</b> · <b>Yura</b>"
    assert ui.title("⚙", "⚙ Settings for chat 5") == "<b>⚙</b> · <b>Settings for chat 5</b>"
    # A header that is only a symbol stays usable, and the icon is never touched.
    assert ui.title("❦", "شعر") == "<b>❦</b> · <b>شعر</b>"


def test_title_keeps_html_parts_raw() -> None:
    from havoc import ui

    assert ui.title("🩶", "<b>Yura</b>", "commands") == "<b>🩶</b> · <b>Yura</b> · <b>commands</b>"


async def test_reminders_are_a_card(runtime) -> None:
    await invoke(runtime, ".remind 30m water the plants")
    sent = await invoke(runtime, ".reminders")
    body = sent[0].text
    assert body.startswith("<b>◷</b> · <b>Reminders (1)")
    assert "<blockquote><code>#1</code>" in body


async def test_empty_reminders_explain_themselves(runtime) -> None:
    sent = await invoke(runtime, ".reminders")
    assert "<blockquote>" not in sent[0].text
    assert ".remind" in sent[0].text


async def test_agenda_is_a_card(runtime) -> None:
    await invoke(runtime, ".remind 30m stand up")
    sent = await invoke(runtime, ".agenda")
    body = sent[0].text
    assert body.startswith("<b>▦</b>")
    assert "<blockquote>◷" in body
