"""The catalogue: what is listed, what is free, and in what order she tries it.

These are the numbers the `.ai` card prints, so they are worth pinning: a budget
that is wrong on the card is worse than no card, because the owner plans around it.
"""

from __future__ import annotations

from havoc import ai_models


def test_the_catalogue_has_both_providers_and_unique_keys() -> None:
    catalogue = ai_models.catalogue()
    assert {spec.provider for spec in catalogue} == {"google", "openrouter"}
    keys = [spec.key for spec in catalogue]
    assert len(keys) == len(set(keys))


def test_every_available_model_shows_its_limits_and_unavailable_ones_say_why() -> None:
    for spec in ai_models.catalogue():
        if spec.available:
            assert spec.rpm and spec.rpd, spec.key
            assert "no published limit" not in ai_models.format_limits(spec)
        else:
            assert spec.icon == "✕"
            assert spec.note, f"{spec.key} must say why it cannot be used"


def test_find_accepts_a_full_key_a_bare_id_and_a_label() -> None:
    assert ai_models.find("google:gemini-3.5-flash-lite") is not None
    assert ai_models.find("gemini-3.5-flash-lite").api_id == "gemini-3.5-flash-lite"
    assert ai_models.find("Gemini 3.5 Flash Lite").provider == "google"
    assert ai_models.find("  AUTO ") .key == "openrouter:auto"
    assert ai_models.find("no-such-model") is None
    assert ai_models.find("") is None


def test_find_is_forgiving_about_a_partial_name() -> None:
    spec = ai_models.find("gemma")
    assert spec is not None and spec.provider == "google"


def test_available_models_excludes_the_paid_tier_ones() -> None:
    available = ai_models.available_models()
    assert all(spec.available for spec in available)
    assert all(spec.available for spec in ai_models.available_models("google"))
    assert "gemini-2.5-pro" not in {spec.api_id for spec in available}


def test_auto_plan_starts_with_the_best_daily_budget_and_uses_every_model_once() -> None:
    plan = ai_models.auto_plan()
    assert plan[0].api_id == "gemini-3.5-flash-lite"
    assert plan[-1].provider == "openrouter"
    assert len(plan) == len(ai_models.available_models())
    assert len({spec.key for spec in plan}) == len(plan)
    # Everything in AUTO_ORDER that is available appears, and everything else follows.
    for key in ai_models.AUTO_ORDER:
        assert key in {spec.key for spec in plan}


def test_budget_counts_down_and_never_goes_negative() -> None:
    spec = ai_models.find("gemini-3.5-flash-lite")
    assert spec.rpd == 500
    assert spec.budget(0) == 500
    assert spec.budget(499) == 1
    assert spec.budget(500) == 0
    assert spec.budget(900) == 0
    unlimited = ai_models.ModelSpec("google", "x", "X", "✦", 1, 1, 0)
    assert unlimited.budget(10_000) > 1000


def test_format_limits_reads_like_a_sentence() -> None:
    assert ai_models.format_limits(ai_models.find("gemma-4-31b-it")) == "30/min · 14,400/day"
    assert ai_models.format_limits(ai_models.ModelSpec("google", "x", "X", "✦", 0, 0, 0)) == "no published limit"


def test_provider_icons_are_typographic_not_emoji() -> None:
    for icon in (*ai_models.PROVIDER_ICONS.values(), *(spec.icon for spec in ai_models.catalogue())):
        assert len(icon) == 1
        assert ord(icon) < 0x1F000, f"{icon!r} is a colour emoji, not a glyph"
