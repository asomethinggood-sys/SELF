"""Folders: Telegram sends titles as ``TextWithEntities``, and the card must not.

The owner's report was a folder list showing
``TextWithEntities(text='manhwa◇lol◇', entities=[])`` — the repr of the object
instead of the folder's name.
"""

from __future__ import annotations

from conftest import invoke
from telethon.tl.types import TextWithEntities


class Folder:
    def __init__(self, folder_id: int, title) -> None:
        self.id = folder_id
        self.title = title
        self.include_peers = [1, 2]


async def test_a_folder_title_is_its_text_not_its_repr(runtime) -> None:
    class Rows:
        filters = [
            Folder(2, TextWithEntities(text="manhwa◇lol◇", entities=[])),
            Folder(3, "Private"),
        ]

    runtime.client.responses["GetDialogFiltersRequest"] = Rows()
    sent = await invoke(runtime, ".folder")
    text = "\n".join(m.text for m in sent)
    assert "manhwa◇lol◇" in text
    assert "TextWithEntities" not in text
    assert "Private" in text


async def test_a_folder_with_no_title_is_a_dash_not_a_crash(runtime) -> None:
    class Rows:
        filters = [Folder(2, TextWithEntities(text="", entities=[]))]

    runtime.client.responses["GetDialogFiltersRequest"] = Rows()
    sent = await invoke(runtime, ".folder")
    assert "—" in "\n".join(m.text for m in sent)
