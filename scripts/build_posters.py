"""Render verse posters: a poem, set on a card, as a PNG she can send.

Why a browser and not Pillow: Persian needs real shaping and a real bidi
algorithm, and Telegram needs a raster image rather than an SVG. Chromium does
both correctly — it is the only renderer in this project that gets ``حافظ``
right without a fight — and a GitHub runner has it.

The output is deliberately small and stable:

``havoc/data/posters/<slug>.png``   the cards
``havoc/data/posters/index.json``   what is in them, for `.yura poster`

Nothing here runs in the bot: the bot only sends files that already exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "havoc" / "data" / "posters"
#: A card is 1080x1350 — a phone photograph, which Telegram shows at full width
#: without upscaling anything.
WIDTH, HEIGHT = 1080, 1350

PALETTE = (
    ("#0f1117", "#e8e3d9", "#8b7d6b"),
    ("#12100e", "#f2e9dc", "#c9a227"),
    ("#0b1412", "#d8e8e0", "#5f9e8f"),
    ("#140d16", "#f0e0f5", "#a06bb8"),
    ("#101018", "#dfe6ff", "#5f7bd9"),
)


def slug(text: str) -> str:
    cleaned = unicodedata.normalize("NFKD", text)
    cleaned = re.sub(r"[^\w\u0600-\u06ff]+", "-", cleaned, flags=re.UNICODE).strip("-")
    return (cleaned or "poster")[:48].lower()


def html_for(poem: dict, palette: tuple[str, str, str], index: int) -> str:
    background, ink, accent = palette
    body = "<br>".join(line for line in str(poem.get("text", "")).splitlines() if line.strip())
    title = str(poem.get("title", "")).strip()
    poet = str(poem.get("poet", "")).strip()
    rtl = str(poem.get("language", "")) == "fa"
    return f"""<!doctype html>
<html lang="{'fa' if rtl else 'en'}" dir="{'rtl' if rtl else 'ltr'}">
<head><meta charset="utf-8"><style>
  @page {{ margin: 0; }}
  html, body {{ margin: 0; padding: 0; background: {background}; }}
  .card {{
    width: {WIDTH}px; height: {HEIGHT}px; box-sizing: border-box;
    padding: 96px 88px; display: flex; flex-direction: column; justify-content: space-between;
    background: radial-gradient(120% 90% at 50% 0%, {background} 40%, #000 140%);
    color: {ink}; font-family: 'Vazirmatn', 'Noto Naskh Arabic', 'Noto Serif', Georgia, serif;
  }}
  .mark {{ font-size: 46px; letter-spacing: 0.3em; color: {accent}; }}
  .verse {{ font-size: 74px; line-height: 1.85; text-align: center; }}
  .rule {{ height: 1px; background: {accent}; opacity: 0.55; margin: 56px auto; width: 42%; }}
  .foot {{ font-size: 44px; text-align: center; color: {accent}; }}
  .foot small {{ display: block; font-size: 30px; opacity: 0.7; margin-top: 14px; letter-spacing: 0.16em; }}
</style></head>
<body><div class="card">
  <div class="mark">❦</div>
  <div class="verse">{body}</div>
  <div>
    <div class="rule"></div>
    <div class="foot">{poet}<small>{title}</small></div>
  </div>
</div></body></html>"""


async def render(items: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        for index, poem in enumerate(items):
            name = f"{index:03d}-{slug(str(poem.get('poet', 'poem')))}-{slug(str(poem.get('title', '')))}.png"
            html = html_for(poem, PALETTE[index % len(PALETTE)], index)
            await page.set_content(html, wait_until="load")
            await page.screenshot(path=str(OUT / name), clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
            written.append(
                {
                    "file": name,
                    "poet": poem.get("poet", ""),
                    "title": poem.get("title", ""),
                    "language": poem.get("language", ""),
                }
            )
            print(f"rendered {name}")
        await browser.close()
    return written


def pick(count: int) -> list[dict]:
    """A spread of poets, one poem each, short enough to fit a card."""
    from havoc import corpus

    chosen: list[dict] = []
    used: set[str] = set()
    for name in corpus.poets():
        if len(chosen) >= count:
            break
        try:
            poems = corpus.search_poems(name, limit=200)
            poems = [poem for poem in poems if poem.get("poet") == name]
        except Exception:  # noqa: BLE001 - an empty poet is not a failure
            poems = []
        for poem in poems:
            text = str(poem.get("text", "")).strip()
            if not text or len(text) > 220 or name in used:
                continue
            chosen.append(poem)
            used.add(name)
            break
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="render verse posters")
    parser.add_argument("--count", type=int, default=12)
    args = parser.parse_args()
    items = pick(args.count)
    if not items:
        print("no poems could be selected; is the corpus installed?", file=sys.stderr)
        return 1
    written = asyncio.run(render(items))
    (OUT / "index.json").write_text(
        json.dumps(written, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(written)} poster(s) and index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
