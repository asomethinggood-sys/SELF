"""The ornament box: a thousand text glyphs she can design a message with.

Why a module of its own
-----------------------
Emoji are pictures; these are *characters*. A row of ``✦`` or a thin ``⊹`` rule
between two paragraphs is typography — it survives every font, copies as text,
and never looks like a sticker dropped into a sentence. The owner asked for a
thousand of them and for her to stop using the same cute face every time, so the
list is grouped by what a glyph is *for* (a star to open with, a rule to separate
two thoughts, a corner to close with) instead of being one flat puddle.

What is excluded, and why
-------------------------
* everything Telegram renders as a colour emoji — the design rule of this project
  is symbols, not pictures, and a green checkmark next to Persian text looks like
  a phone notification;
* everything the markdown renderer treats as syntax (``* _ ~ | > ` # < &``) — a
  glyph that silently becomes a tag is not decoration, it is a bug;
* invisible characters (zero-width spaces, bidi controls, variation selectors) —
  they make a message look broken in a different way than intended.

Two APIs matter to the rest of the code:

* :func:`suggest` — a small set of *fresh* glyphs for one reply, grouped by role;
* :func:`restyle` — the safety net. If she reaches for the same ornament twice in
  a row, the repeat is swapped for an unused one of the same kind, unless it sits
  inside a kaomoji like ``(¬‿¬)`` where a swap would break the face.
"""

from __future__ import annotations

import random
import unicodedata

#: Curated glyphs, by the job they do. These are the pretty ones — chosen by hand
#: so the pool is not just "every dingbat Unicode has ever shipped".
CURATED: dict[str, tuple[str, ...]] = {
    "star": (
        "✦", "✧", "⋆", "✩", "✪", "✫", "✬", "✭", "✮", "✯", "✰", "✷", "✶", "✵",
        "✴", "✳", "✲", "✱", "✺", "✹", "✸", "❋", "❊", "❉", "❈", "⭒", "⋆", "⁂",
        "⁎", "∗", "▸✦", "✩", "❃", "❊",
    ),
    "spark": ("✧", "❇", "✳", "⁕", "⁂", "※", "⋰", "⋱", "⁘", "⁙", "⁚", "⁛", "⁜"),
    "heart": ("♡", "♥", "❥", "❣", "❤", "ღ", "♡", "❦", "❧", "☙"),
    "floral": (
        "❀", "✿", "❁", "✾", "❃", "❋", "❊", "❉", "❈", "⚘", "☘", "❦", "❧",
        "☙", "⚜", "❃", "⁕",
    ),
    "leaf": ("❦", "❧", "☙", "⚘", "☘", "⸙", "⁂", "❋"),
    "moon": ("☾", "☽", "☼", "✵", "✶", "⋆", "☪", "⯑", "☄", "◐", "◑", "◒", "◓"),
    "wave": ("〜", "〰", "∿", "⁓", "⌇", "︴", "∼", "≈", "≋", "⌒", "⌓", "⌢", "⌣"),
    "rule": (
        "─", "━", "│", "┃", "═", "╌", "╍", "┄", "┅", "┈", "┉", "▁", "▔", "⎯",
        "⊹", "⁘", "⁙", "⋮", "⋯", "⋰", "⋱", "⌁",
        "⏤", "⸺", "⸻", "﹘", "⌁", "⟡", "⟢", "⟣",
    ),
    "dot": (
        "·", "•", "‣", "⁃", "‧", "∙", "⋅", "⸱", "⸳", "・", "･", "◦", "∘", "○",
        "⦁", "⦂", "⁘", "⁙", "⋄", "▫", "▪",
    ),
    "arrow": (
        "→", "⇒", "➜", "➛", "➝", "➞", "➟", "➠", "➢", "➣", "➤", "➥", "➦", "➧",
        "➨", "➩", "➪", "➫", "➬", "➭", "➮", "➯", "➱", "➲", "➳", "➵", "➶", "➷",
        "➸", "➹", "➺", "➻", "➼", "➽", "➾", "⟶", "⟹", "⟾", "⟿", "⤳", "⤴", "⤵",
        "↵", "↩", "↪", "↬", "↭", "↮", "⇢", "⇥", "⇢", "⇝",
    ),
    "open": (
        "꒰", "「", "『", "❨", "❪", "❬", "❮", "❰", "【", "〖", "⦅", "⦇", "⟨", "⟪",
        "⸨", "⁅", "⸢", "⸤", "〔", "〘", "⌜", "⌈", "⌊", "❮", "«",
    ),
    "close": (
        "꒱", "」", "』", "❩", "❫", "❭", "❯", "❱", "】", "〗", "⦆", "⦈", "⟩", "⟫",
        "⸩", "⁆", "⸣", "⸥", "〕", "〙", "⌟", "⌉", "⌋", "❯", "»",
    ),
    "note": ("♪", "♫", "♬", "♩", "♭", "♮", "♯", "𝄞", "𝄢", "𝄡"),
    "tech": ("⌘", "⌬", "⌂", "⌗", "⌖", "⌑", "⍟", "⍣", "⎔", "⏣", "⌗", "⌙", "⌟"),
    "cross": (
        "✕", "✖", "✗", "✘", "⨯", "⨉", "✚", "✛", "✜", "✝", "✞", "✟", "☩", "☨",
        "☦", "⳨", "☓", "❌",
    ),
    "check": ("✓", "✔", "☑", "☒", "✅", "☐", "◻", "◼", "❏", "❐", "❑", "❒"),
    "circle": (
        "◌", "◍", "◎", "●", "○", "◉", "◈", "◇", "◆", "◊", "❂", "❍", "⍟", "⊚",
        "⊛", "⊜", "⊝", "⊕", "⊖", "⊗", "⊘", "⊙", "⊛", "⟐", "⦾", "⦿",
    ),
    "square": (
        "▪", "▫", "▢", "▣", "▤", "▥", "▦", "▧", "▨", "▩", "■", "□", "◰", "◱",
        "◲", "◳", "◴", "◵", "◶", "◷",
    ),
    "triangle": (
        "▲", "△", "▴", "▵", "▶", "▷", "▸", "▹", "►", "▻", "▼", "▽", "◀", "◁",
        "◂", "◃", "◢", "◣", "◤", "◥",
    ),
    "diamond": ("◇", "◆", "◈", "◊", "⟐", "❖", "❖", "⟡", "⌑", "▱", "▰", "▱"),
    "corner": (
        "┌", "┍", "┎", "┏", "└", "┕", "┖", "┗", "├", "┝", "┞", "┟", "┠", "┤",
        "┥", "┦", "┧", "┬", "┴", "┼", "╭", "╮", "╯", "╰", "╱", "╲", "╳",
    ),
    "ornament": (
        "❦", "❧", "☙", "⁂", "⁕", "※", "✤", "✥", "✦", "୨", "୧", "ᨓ", "ᨒ", "⌘",
        "⊹", "⋆", "❖", "⁘", "⸜", "⸝",
        "❈", "❋", "⌁", "⍟", "⟡", "⸎", "⸙", "⸛", "⸜", "⸝",
    ),
    "bracket": ("⟦", "⟧", "〚", "〛", "⦃", "⦄", "⦉", "⦊", "⟬", "⟭", "⸤", "⸥"),
    "misc": (
        "§", "¶", "†", "‡", "‡", "℘", "℮", "№", "℀", "℁", "℅", "℆", "⌅", "⌆",
        "⁎", "⁑", "⁂", "⸮", "⸘", "⸰", "⸳",
    ),
}

#: Ranges that are *typography* first: they exist to make a page look composed.
#: The bulk of the thousand comes from here — mostly arrows, rules and shapes.
BULK_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x2190, 0x21FF, "arrow"),
    (0x27F0, 0x27FF, "arrow"),
    (0x2900, 0x297F, "arrow"),
    (0x2B00, 0x2B4F, "arrow"),
    (0x25A0, 0x25FF, "square"),
    (0x2500, 0x257F, "corner"),
    (0x2580, 0x259F, "square"),
    (0x27C0, 0x27EF, "misc"),
    (0x2E00, 0x2E7F, "misc"),
    (0x2300, 0x23FF, "tech"),
)

#: Rendered as colour emoji by default on most phones, so they never enter the box.
EMOJI_PRESENTATION = frozenset(
    "⌚ ⌛ ⏩ ⏪ ⏫ ⏬ ⏭ ⏮ ⏯ ⏰ ⏱ ⏲ ⏳ ☀ ☁ ☂ ☃ ☄ ☎ ☑ ☕ ☘ ☝ ☠ ☢ ☣ ☦ ☪ ☮ ☯ ☸ "
    "☹ ☺ ♀ ♂ ♈ ♉ ♊ ♋ ♌ ♍ ♎ ♏ ♐ ♑ ♒ ♓ ♟ ♠ ♥ ♦ ♣ ♨ ♻ ♾ ♿ ⚒ ⚓ ⚔ ⚕ ⚖ ⚗ ⚙ ⚚ ⚛ "
    "⚜ ⚠ ⚡ ⚧ ⚰ ⚱ ⚽ ⚾ ⛅ ⛎ ⛏ ⛑ ⛓ ⛔ ⛩ ⛪ ⛰ ⛱ ⛲ ⛳ ⛴ ⛵ ⛷ ⛸ ⛹ ⛺ ⛽ ✂ ✅ ✈ ✉ ✊ "
    "✋ ✌ ✍ ✏ ✒ ✖ ✝ ✡ ✨ ✳ ✴ ❄ ❇ ❌ ❎ ❓ ❔ ❕ ❗ ➕ ➖ ➗ ➡ ➰ ➿ ⭐ ⭕ ⬛ ⬜ "
    "⭐ 🅰 🅱 Ⓜ 🌀 🌁 🌂 🌃 🌄 🌅 🌆 🌇 🌈 🌉 🌊 🌋 🌌 🌍 🌎 🌏 🌐 🌑 🌒 🌓 🌔 🌕 🌖 "
    "🌗 🌘 🌙 🌚 🌛 🌜 🌝 🌞 🌟 🌠 🌡 🌢 🌤 🌥 🌦 🌧 🌨 🌩 🌪 🌫 🌬 🌭 🌮 🌯 🌰 🌱 🌲 "
    "🌳 🌴 🌵 🌶 🌷 🌸 🌹 🌺 🌻 🌼 🌽 🌾 🌿 🍀 🍁 🍂 🍃".split()
)

#: Markdown the renderer would eat, plus anything that would break escaping.
UNSAFE = frozenset("*_~|>`#<&\\[](){}" + '"' + "'" + "`")


def _usable(char: str) -> bool:
    """True when ``char`` can sit in a sentence without becoming syntax or a picture."""
    if len(char) != 1:
        return False
    if char in UNSAFE or char in EMOJI_PRESENTATION:
        return False
    if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Mn", "Me", "Zl", "Zp"}:
        return False
    return not char.isspace()


def _build() -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    # A curated entry is one character; anything longer is a typo, not a glyph.
    categories: dict[str, list[str]] = {
        name: [g for g in items if len(g) == 1 and _usable(g)]
        for name, items in CURATED.items()
    }
    for start, end, name in BULK_RANGES:
        bucket = categories.setdefault(name, [])
        for code in range(start, end + 1):
            char = chr(code)
            if _usable(char):
                bucket.append(char)
    # Deduplicate inside a category, keep the curated order (the pretty ones first).
    cleaned = {name: tuple(dict.fromkeys(items)) for name, items in categories.items()}
    flat: list[str] = []
    for items in cleaned.values():
        flat.extend(items)
    return cleaned, tuple(dict.fromkeys(flat))


GLYPHS, ALL = _build()

#: Which category a glyph belongs to, for the "swap a repeat for a fresh one" path.
_OWNER: dict[str, str] = {}
for _name, _items in GLYPHS.items():
    for _glyph in _items:
        _OWNER.setdefault(_glyph, _name)
del _name, _items, _glyph

#: Roles a reply actually uses, so the suggestion list is not noise.
ROLES: dict[str, tuple[str, ...]] = {
    "open": ("star", "ornament", "moon", "floral", "spark", "diamond"),
    "separate": ("rule", "dot", "wave", "spark", "star"),
    "close": ("ornament", "floral", "heart", "star", "moon", "spark"),
    "point": ("arrow", "triangle", "diamond", "check", "circle"),
    "wrap": ("bracket", "corner", "square"),
}


#: Kaomoji, kept apart from the ornaments because a face is *hers*, not syntax.
#: Hand-picked so a message can wear a different one every time instead of the
#: same three: ``ᵕ̈`` and ``Ⱉ`` and ``₍ᐢ. .ᐢ₎`` are three different moods.
FACES: tuple[str, ...] = (
    "ᵕ̈", "ᐧ ᴗ ᐧ", "꒰ᵕ ᵕ꒱", "₍ᐢ. .ᐢ₎", "₍ᐢ..ᐢ₎", "(¬‿¬)", ">_<", "Ⱉ", "ᐟ", "ᐠᐧᐟ",
    "(˶ᵔ ᵕ ᵔ˶)", "ᵔ◡ᵔ", "( ˘ ³˘)♡", "ᶘ ᵒᴥᵒᶅ", "(๑˃ᴗ˂)ﻭ", "٩(ˊᗜˋ*)و", "⸜(｡˃ ᵕ ˂ )⸝",
    "⋆˙⟡♡", "(≧▽≦)", "(；一_一)", "ᕕ( ᐛ )ᕗ", "ʕ•ᴥ•ʔ", "(๑•̀ㅂ•́)و", "⸜(๑’ᵕ’๑)⸝",
    "(ᵕ—ᴗ—)", "૮₍ ˃ ⤙ ˂ ₎ა", "૮ ˶ᵔ ᵕ ᵔ˶ ა", "⟡ ݁₊ ⊹", "₊˚⊹ ᰔ", "ᯓᡣ𐭩", "ᡣ𐭩",
    "૮꒰˶ᵔ ᵕ ᵔ˶꒱ა", "꒰ᵕ༚ᵕ꒱˖♡", "( ´ ▽ ` )", "(๑>◡<๑)", "(*´▽`*)", "(¬‿¬ )",
    "✧(＾ω＾)", "^._.^", "-ᴗ-", "ᵕ̈♡", "(˶ᵔᗜᵔ˶)", "૮₍˶ •. • ⑅₎ა", "ᐟᐟ", "ʚɞ",
    "ᐧᵕᐧ", "( ˶ˆ ᗜ ˆ˵ )", "ヽ(・∀・)ﾉ", "(｡•́︿•̀｡)", "⸜(ᵕᵕ)⸝", "₊ ⊹ ᵕ̈",
    "ᰔᩚ", "ᡣ𐭩♡", "(๑'ᵕ'๑)", "ᵕ̈ ✧", "ᶻ 𝗓 𐰁", "ᐢ. .ᐢ", "꒰ ∗ ᵕ ᵕ ∗ ꒱",
)

#: Faces already used, so a message can wear a different one. Kept whole (a face
#: is several characters) and matched as a substring.


def collect_faces(text: str) -> set[str]:
    """Every kaomoji from :data:`FACES` that appears in ``text``."""
    body = str(text or "")
    return {face for face in FACES if face in body}


def fresh_faces(*, avoid: set[str] | None = None, count: int = 3) -> list[str]:
    taken = set(avoid or ())
    pool = [face for face in FACES if face not in taken] or list(FACES)
    return random.sample(pool, k=min(count, len(pool)))


def signature(*, avoid: set[str] | None = None) -> str:
    """One fresh ornament for this message, and it is hers to open with."""
    return fresh(avoid=avoid, role="open", count=1)[0]


def restyle_faces(text: str, used: set[str] | None = None) -> str:
    """Swap a kaomoji she already wore in a recent answer for a fresh one."""
    body = str(text or "")
    if not body or not used:
        return body
    taken = collect_faces(body) | set(used)
    for face in used:
        if face not in body:
            continue
        replacement = fresh_faces(avoid=taken, count=1)[0]
        body = body.replace(face, replacement)
        taken.add(replacement)
        taken.discard(face)
    return body


def count() -> int:
    """How many glyphs the box holds."""
    return len(ALL)


def category_of(glyph: str) -> str:
    return _OWNER.get(glyph, "")


def collect(text: str) -> set[str]:
    """Every glyph from the box that appears in ``text``."""
    return {char for char in str(text or "") if char in _OWNER}


def fresh(*, avoid: set[str] | None = None, role: str = "", count: int = 1) -> list[str]:
    """``count`` glyphs from the box that are not in ``avoid``. Never raises."""
    taken = set(avoid or ())
    pool: list[str] = []
    names = ROLES.get(role) or tuple(GLYPHS)
    for name in names:
        pool.extend(g for g in GLYPHS.get(name, ()) if g not in taken)
    if len(pool) < count:  # everything is used up: allow repeats rather than nothing
        pool = [g for g in ALL if g not in taken] or list(ALL)
    return random.sample(pool, k=min(count, len(pool)))


def suggest(
    avoid: set[str] | None = None,
    *,
    faces: set[str] | None = None,
    per_role: int = 5,
) -> dict[str, list[str]]:
    """A fresh handful for one message, grouped by the job each does."""
    taken = set(avoid or ())
    out: dict[str, list[str]] = {}
    for role in ("open", "separate", "close", "point", "wrap"):
        picked = fresh(avoid=taken, role=role, count=per_role)
        taken.update(picked)
        out[role] = picked
    out["face"] = fresh_faces(avoid=faces, count=4)
    out["signature"] = [signature(avoid=taken)]
    return out


def _standalone(text: str, index: int) -> bool:
    """True when the glyph at ``index`` is decoration, not part of a kaomoji.

    ``╰(*´︶`*)╯`` must survive untouched: its glyphs sit against letters and
    brackets with no spaces, while a designed ornament stands on its own.
    """
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    def _edge(char: str) -> bool:
        return not char or char.isspace() or char in ".,!?;:—-–…\n\t"
    return _edge(before) and _edge(after)


def restyle(text: str, used: set[str] | None = None) -> str:
    """Swap ornaments she repeats from an earlier answer for fresh ones.

    Only *standalone* glyphs are touched, and only ones that already appeared in
    a recent answer; everything else — kaomoji, a quote, code — is left exactly as
    written. If the box has nothing fresh left, the text is returned unchanged.
    """
    body = str(text or "")
    if not body or not used:
        return body
    taken = collect(body) | set(used)
    chars = list(body)
    for index, char in enumerate(chars):
        if char not in used or char not in _OWNER:
            continue
        if not _standalone(body, index):
            continue
        candidates = fresh(avoid=taken, role=_role_for(_OWNER[char]), count=1)
        if not candidates:
            continue
        chars[index] = candidates[0]
        taken.add(candidates[0])
        taken.discard(char)
    return "".join(chars)


def _role_for(category: str) -> str:
    for role, names in ROLES.items():
        if category in names:
            return role
    return ""


def describe(language: str = "en") -> str:
    """One line for a card or a log: how big the box is."""
    return f"{count()} glyphs in {len(GLYPHS)} sets" if language == "en" else f"{count()} نماد در {len(GLYPHS)} دسته"
