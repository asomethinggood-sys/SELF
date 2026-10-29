"""Render a Hafez reading as a picture.

Yura's ``.fal`` does not send a paragraph of text: it draws a card — parchment,
gold rules, the couplets set large, the interpretation below in a quieter size —
and sends only the image (feature areas 27, 63, 100).

Two constraints shaped this module:

* **Pillow's wheels are not built with libraqm.** Without it, ``draw.text``
  cannot join Arabic letters or reverse the run, so Persian comes out as
  disconnected, left-to-right glyphs. Instead the text is shaped with
  ``arabic_reshaper`` and reordered with ``python-bidi`` before it is drawn:
  pure Python, no system libraries, identical output on every machine.
* **The card must look the same on every render.** All randomness (paper grain,
  ornament placement) is seeded from the reading id, so a given fal is stable.

Everything degrades: if Pillow or a font is missing, :func:`available` reports
it and the command falls back to a text card rather than failing.
"""

from __future__ import annotations

import io
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
REGULAR = ASSET_DIR / "Vazirmatn-Regular.ttf"
BOLD = ASSET_DIR / "Vazirmatn-Bold.ttf"

#: Card geometry, in pixels. 1080 wide survives Telegram's compression without
#: the text turning to mush on a phone.
WIDTH = 1080
PADDING = 68
VERSE_SIZE = 46
NOTE_SIZE = 33
LABEL_SIZE = 29
TITLE_SIZE = 62
SUBTITLE_SIZE = 28
FOOTER_SIZE = 24

#: A whole ghazal is 4000 pixels tall, which nobody can read on a phone. A fal
#: shows an opening — the way the tradition reads it — and the card stays one
#: screen high.
MAX_VERSES = 7

PAPER = (246, 238, 223)
PAPER_SHADE = (236, 224, 202)
INK = (42, 32, 24)
MUTED = (124, 104, 82)
GOLD = (170, 132, 66)
GOLD_SOFT = (206, 176, 118)


def available() -> tuple[bool, str]:
    """Can this machine draw a card? Returns ``(ok, reason)``."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False, "pillow"
    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
    except ImportError:
        return False, "arabic-reshaper"
    if not REGULAR.exists() or not BOLD.exists():
        return False, "font"
    return True, ""


def _shape(text: str) -> str:
    """Join and reorder Persian/Arabic so a plain LTR draw call looks right."""
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(text))


@lru_cache(maxsize=32)
def _font(path: str, size: int) -> Any:
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


def _width(draw: Any, text: str, font: Any) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw: Any, text: str, font: Any, limit: int) -> list[str]:
    """Greedy word wrap measured on the *shaped* text.

    Persian wraps between words like any other script, but the joined glyphs
    are narrower than the sum of their isolated forms, so measuring the raw
    string would wrap too early.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _width(draw, _shape(candidate), font) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _ornament(draw: Any, center: tuple[int, int], size: int, colour: tuple[int, int, int]) -> None:
    x, y = center
    draw.polygon([(x, y - size), (x + size, y), (x, y + size), (x - size, y)], outline=colour)
    draw.polygon(
        [(x, y - size // 2), (x + size // 2, y), (x, y + size // 2), (x - size // 2, y)],
        fill=colour,
    )


def _rule(draw: Any, y: int, *, width: int = WIDTH, inset: int = PADDING + 26) -> None:
    """A gold hairline with a lozenge in the middle."""
    left, right = inset, width - inset
    middle = width // 2
    draw.line([(left, y), (middle - 34, y)], fill=GOLD_SOFT, width=2)
    draw.line([(middle + 34, y), (right, y)], fill=GOLD_SOFT, width=2)
    _ornament(draw, (middle, y), 13, GOLD)


def render_fal(reading: dict[str, Any], *, width: int = WIDTH) -> bytes:
    """Draw one reading and return PNG bytes."""
    from PIL import Image, ImageDraw

    all_verses = [str(verse).strip() for verse in reading.get("verses", []) if str(verse).strip()]
    verses = all_verses[:MAX_VERSES]
    interpretation = " ".join(str(reading.get("interpretation", "")).split())
    reading_id = int(reading.get("id") or 0)

    # One bayt should read as one line. Pick the largest type size at which
    # every selected verse fits, instead of letting a long verse break in two.
    verse_size = VERSE_SIZE
    for candidate in (VERSE_SIZE, 42, 38, 34, 30):
        verse_size = candidate
        if all(
            _width(
                ImageDraw.Draw(Image.new("RGB", (10, 10))),
                _shape(verse),
                _font(str(BOLD), candidate),
            )
            <= width - 2 * PADDING
            for verse in verses
        ):
            break

    bold = _font(str(BOLD), verse_size)
    regular = _font(str(REGULAR), NOTE_SIZE)
    label = _font(str(BOLD), LABEL_SIZE)
    title = _font(str(BOLD), TITLE_SIZE)
    subtitle = _font(str(REGULAR), SUBTITLE_SIZE)
    footer = _font(str(REGULAR), FOOTER_SIZE)

    # Measure on a scratch canvas, then size the real one to fit.
    scratch = Image.new("RGB", (width, 10))
    probe = ImageDraw.Draw(scratch)
    inner = width - 2 * PADDING

    verse_lines: list[str] = []
    for verse in verses:
        verse_lines.extend(_wrap(probe, verse, bold, inner))
    truncated = len(all_verses) > MAX_VERSES
    note_lines = _wrap(probe, interpretation, regular, inner)

    verse_step = int(verse_size * 1.86)
    note_step = int(NOTE_SIZE * 1.86)
    height = (
        PADDING
        + 62  # top ornament
        + 84  # title
        + 54  # subtitle
        + 64  # first rule
        + (len(verse_lines) + (1 if truncated else 0)) * verse_step
        + 58  # second rule
        + 46  # label
        + len(note_lines) * note_step
        + 72  # footer block
        + PADDING
    )
    height = max(height, 720)

    canvas = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(canvas)

    # Paper: a soft vertical wash plus a deterministic grain of faint dots.
    for row in range(height):
        ratio = row / max(1, height - 1)
        blend = 0.55 * abs(ratio - 0.5) * 2
        colour = tuple(
            int(PAPER[i] * (1 - blend) + PAPER_SHADE[i] * blend) for i in range(3)
        )
        draw.line([(0, row), (width, row)], fill=colour)
    grain = secrets.SystemRandom()  # noqa: F841  (kept for clarity of intent)
    import random as _random

    rng = _random.Random(reading_id or 7)
    for _ in range(1400):
        x = rng.randrange(width)
        y = rng.randrange(height)
        shade = rng.choice(((210, 197, 172), (250, 244, 233)))
        draw.point((x, y), fill=shade)

    # Border: two rules and corner lozenges.
    margin = 26
    draw.rectangle([margin, margin, width - margin, height - margin], outline=GOLD, width=3)
    draw.rectangle(
        [margin + 10, margin + 10, width - margin - 10, height - margin - 10],
        outline=GOLD_SOFT,
        width=1,
    )
    for corner in (
        (margin, margin),
        (width - margin, margin),
        (margin, height - margin),
        (width - margin, height - margin),
    ):
        _ornament(draw, corner, 11, GOLD)

    y = PADDING + 10
    _ornament(draw, (width // 2, y + 8), 15, GOLD)
    y += 62
    _centered(draw, _shape("فال حافظ"), title, y, width, INK)
    y += 84
    _centered(draw, _shape("تفأل به دیوان حافظ"), subtitle, y, width, MUTED)
    y += 54
    _rule(draw, y, width=width)
    y += 58

    for line in verse_lines:
        _centered(draw, _shape(line), bold, y, width, INK)
        y += verse_step
    if truncated:
        _centered(draw, _shape("…"), label, y + 6, width, MUTED)
        y += verse_step

    y += 30
    _rule(draw, y, width=width)
    y += 40
    _centered(draw, _shape("معنی"), label, y, width, GOLD)
    y += 46
    for line in note_lines:
        _centered(draw, _shape(line), regular, y, width, INK)
        y += note_step

    y = height - PADDING - 26
    _centered(draw, _shape("یورا · فال حافظ"), footer, y, width, MUTED)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _centered(draw: Any, text: str, font: Any, y: int, width: int, colour: tuple[int, int, int]) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) / 2 - box[0], y), text, font=font, fill=colour)


def save_png(payload: bytes, path: Path) -> Path:
    path.write_bytes(payload)
    return path
