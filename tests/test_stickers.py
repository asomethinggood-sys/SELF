"""The sticker hand: a word for a feeling becomes a sticker on the wire."""

from __future__ import annotations

from conftest import FakeClient, FakeMessage, invoke
from telethon.tl.types import Document, DocumentAttributeSticker, StickerSet

from havoc.features.stickers import _wanted


def _document(doc_id: int, alt: str) -> Document:
    return Document(
        id=doc_id,
        access_hash=doc_id * 7,
        file_reference=b"",
        date=None,
        mime_type="application/x-tgsticker",
        size=1024,
        dc_id=2,
        attributes=[DocumentAttributeSticker(alt=alt, stickerset=None)],
    )


class StickerClient(FakeClient):
    """The usual fake, plus the two pack requests Telethon would make."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[str] = []
        self.sent_files: list = []

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        if name == "GetAllStickersRequest":
            class _All:
                sets = [StickerSet(
                    id=11,
                    access_hash=22,
                    title="hearts",
                    short_name="hearts",
                    count=2,
                    hash=0,
                    official=False,
                )]
            return _All()
        if name == "GetStickerSetRequest":
            class _Pack:
                documents = [_document(1, "❤️"), _document(2, "❤️"), _document(3, "😂")]
                packs = []
                set = None
            return _Pack()
        return None

    async def send_file(self, chat_id, file, **kwargs):
        self.sent_files.append((chat_id, file, kwargs))
        return FakeMessage(id=9001, chat_id=int(chat_id), text="", out=True)


def test_a_word_becomes_the_emoji_the_packs_are_indexed_by() -> None:
    assert _wanted("love") == "❤️"
    assert _wanted("❤️") == "❤️"
    assert _wanted("خنده") == "😂"
    assert _wanted("nothing-like-this") == ""


async def test_a_sticker_reaches_telegram(runtime) -> None:
    await runtime.feature_manager.set_enabled("media", True)
    client = StickerClient()
    runtime.client = client
    runtime.sender.client = client
    sent = await invoke(runtime, ".sticker ❤️", chat_id=-1001234567890)
    assert client.requests[:2] == ["GetAllStickersRequest", "GetStickerSetRequest"]
    assert client.sent_files, "no sticker was sent"
    chat_id, document, _kwargs = client.sent_files[0]
    assert int(chat_id) == -1001234567890
    assert isinstance(document, Document)
    assert document.id in {1, 2}
    assert "س" not in sent[0].text and "sticker" in sent[0].text.lower()


async def test_a_pack_without_the_emoji_is_not_sent(runtime) -> None:
    await runtime.feature_manager.set_enabled("media", True)
    client = StickerClient()
    runtime.client = client
    runtime.sender.client = client
    await invoke(runtime, ".sticker 🚀")
    assert client.sent_files == []


async def test_a_word_i_do_not_know_is_refused_with_its_name(runtime) -> None:
    await runtime.feature_manager.set_enabled("media", True)
    client = StickerClient()
    runtime.client = client
    runtime.sender.client = client
    sent = await invoke(runtime, ".sticker qwzzx")
    assert client.requests == []
    assert "qwzzx" in sent[0].text
