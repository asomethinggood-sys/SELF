"""The model catalogue: what can answer, how much it costs, and its mark.

Two providers are configured, and they could not be more different in what they
give away for free. Google's free tier is generous but per model — 500 requests a
day on the Lite flashes, 14,400 on Gemma — while OpenRouter's free plan is 50 a
day across the whole account. The numbers below are transcribed from Google's own
AI Studio rate-limit page for this project (November 2026), because a limit
nobody wrote down is a limit nobody can plan around.

Icons are typographic, never colour emoji: the cards are read in a monospace chat
bubble and a row of tiny photographs of fruit would look worse than twelve
characters that mean something.

* ``◈`` — the model she is speaking with right now
* ``✦`` — 500 requests a day or better: the everyday voice
* ``✧`` — 20 a day: fine for a second opinion, gone by lunchtime
* ``❖`` — 14,400 a day, small context: the backup that never runs out
* ``◇`` — discovered on OpenRouter, 50 a day for the whole account
* ``✕`` — listed by the provider, not available on this plan
"""

from __future__ import annotations

from dataclasses import dataclass

#: Where each provider's OpenAI-shaped endpoint lives. Google exposes one, which
#: is why the same client speaks to both (verified against both keys).
PROVIDER_ENDPOINTS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}

PROVIDER_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter",
    "google": "Google AI Studio",
}

PROVIDER_ICONS: dict[str, str] = {
    "openrouter": "◇",
    "google": "✦",
}

#: A free OpenRouter account: 50 model requests a day, reset at 00:00 UTC.
OPENROUTER_DAILY_REQUESTS = 50
OPENROUTER_MODEL = "auto"


@dataclass(frozen=True)
class ModelSpec:
    """One selectable model and its published free-tier limits."""

    provider: str
    api_id: str
    label: str
    icon: str
    rpm: int = 0  # requests per minute, 0 = unlimited/unknown
    tpm: int = 0  # tokens per minute, 0 = unlimited/unknown
    rpd: int = 0  # requests per day, 0 = unlimited/unknown
    available: bool = True
    note: str = ""
    #: Can the model see a picture? Google's Gemini family can; the gateway's
    #: "auto" routes to whatever is free, so it is only a maybe.
    vision: bool = True

    @property
    def key(self) -> str:
        """Stable identifier used in settings and in the usage table."""
        return f"{self.provider}:{self.api_id}"

    def budget(self, used: int) -> int:
        """Requests left today, or a large number when the limit is unknown."""
        if not self.rpd:
            return 1_000_000
        return max(0, self.rpd - used)


def _google(api_id: str, label: str, icon: str, rpm: int, tpm: int, rpd: int, note: str = "") -> ModelSpec:
    return ModelSpec("google", api_id, label, icon, rpm, tpm, rpd, True, note)


#: Google's free-tier table, best daily budget first. Transcribed from
#: AI Studio → Gemini API Rate Limit (28-day window), November 2026.
GEMINI_MODELS: tuple[ModelSpec, ...] = (
    _google("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite", "✦", 15, 250_000, 500, "best all-round free model"),
    _google("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite", "✦", 15, 250_000, 500, "same budget, older family"),
    _google("gemma-4-31b-it", "Gemma 4 31B", "❖", 30, 16_000, 14_400, "huge daily budget, small context"),
    _google("gemma-4-26b-a4b-it", "Gemma 4 26B A4B", "❖", 30, 16_000, 14_400, "MoE sibling of the 31B"),
    _google("gemini-3.8-flash", "Gemini 3.8 Flash", "✧", 5, 250_000, 20, "better answers, 20 a day"),
    _google("gemini-3.6-flash", "Gemini 3.6 Flash", "✧", 5, 250_000, 20),
    _google("gemini-3-flash-preview", "Gemini 3 Flash", "✧", 5, 250_000, 20, "preview slug"),
    _google("gemini-2.5-flash", "Gemini 2.5 Flash", "✧", 5, 250_000, 20),
    _google("gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite", "✧", 10, 250_000, 20),
    # Listed by the API, refused on the free plan (0 RPM / 0 RPD in the table).
    ModelSpec("google", "gemini-3.1-pro-preview", "Gemini 3.1 Pro", "✕", 0, 0, 0, False, "paid tier only"),
    ModelSpec("google", "gemini-2.5-pro", "Gemini 2.5 Pro", "✕", 0, 0, 0, False, "paid tier only"),
    ModelSpec("google", "gemini-4-argon", "Gemini 4 Argon", "✕", 0, 0, 0, False, "paid tier only"),
)

#: Discovered on OpenRouter at runtime; limits are per account, not per model.
OPENROUTER_SPEC = ModelSpec(
    "openrouter",
    OPENROUTER_MODEL,
    "OpenRouter auto",
    "◇",
    20,
    0,
    OPENROUTER_DAILY_REQUESTS,
    True,
    "asks the gateway which free model exists",
)

#: Ordering used when the owner has not chosen: cheap-to-burn first.
AUTO_ORDER: tuple[str, ...] = (
    "google:gemini-3.5-flash-lite",
    "google:gemini-3.1-flash-lite",
    "google:gemma-4-31b-it",
    "google:gemini-3.8-flash",
    "openrouter:auto",
)


def catalogue() -> tuple[ModelSpec, ...]:
    """Every model this project knows how to talk to, available ones first."""
    return (*GEMINI_MODELS, OPENROUTER_SPEC)


def find(reference: str) -> ModelSpec | None:
    """Look a model up by ``provider:api_id``, bare ``api_id``, or label."""
    needle = reference.strip().lower()
    if not needle:
        return None
    for spec in catalogue():
        if needle in {spec.key.lower(), spec.api_id.lower(), spec.label.lower()}:
            return spec
    for spec in catalogue():
        if needle in spec.api_id.lower() or needle in spec.label.lower():
            return spec
    return None


def available_models(provider: str | None = None) -> tuple[ModelSpec, ...]:
    return tuple(
        spec for spec in catalogue() if spec.available and (provider is None or spec.provider == provider)
    )


def vision_models() -> tuple[ModelSpec, ...]:
    """Models that can look at a picture, best first."""
    return tuple(spec for spec in auto_plan() if spec.vision)


def auto_plan() -> tuple[ModelSpec, ...]:
    """The default attempt order: the curated head, then the biggest budgets.

    OpenRouter always sits at the end. Its free allowance is fifty requests for
    the whole account, so it is the last resort by design — a gateway call must
    never spend the budget that the *next* message needs.
    """
    by_key = {spec.key: spec for spec in available_models()}
    curated = [by_key[key] for key in AUTO_ORDER if key in by_key]
    plan = [spec for spec in curated if spec.provider != "openrouter"]
    rest = [spec for spec in available_models() if spec not in plan and spec.provider != "openrouter"]
    rest.sort(key=lambda spec: -spec.rpd)
    gateway = [spec for spec in available_models() if spec.provider == "openrouter"]
    return tuple(plan + rest + gateway)


def format_limits(spec: ModelSpec) -> str:
    """``15/min · 500/day`` — the two numbers that decide whether it is usable."""
    parts: list[str] = []
    if spec.rpm:
        parts.append(f"{spec.rpm}/min")
    if spec.rpd:
        parts.append(f"{spec.rpd:,}/day")
    return " · ".join(parts) if parts else "no published limit"
