"""Sending pictures: what is allowed, what is refused, and why.

Most of these are security tests. A userbot that will send any path it is given
can be talked into sending its own session file; one that fetches any URL can
be pointed at the machine it runs on. Both are covered here.
"""

from __future__ import annotations

import pytest
from conftest import invoke, last_text

from havoc.services import media as media_service
from havoc.services.media import MediaError


def _file(tmp_path, name: str = "sunset.jpg", size: int = 128):
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff" + b"x" * max(0, size - 3))
    return path


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_a_photo_is_recognized(tmp_path) -> None:
    path = _file(tmp_path)
    item = media_service.resolve(str(path))
    assert item.kind == "photo"
    assert item.name == "sunset.jpg"
    assert item.size == 128
    assert item.is_photo_limit is True


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("clip.mp4", "video"),
        ("song.mp3", "audio"),
        ("anim.gif", "animation"),
        ("report.pdf", "document"),
        ("data.json", "document"),
    ],
)
def test_types_are_classified(tmp_path, name: str, kind: str) -> None:
    assert media_service.resolve(str(_file(tmp_path, name))).kind == kind


def test_quotes_and_tilde_are_handled(tmp_path, monkeypatch) -> None:
    """Shell habits must not turn into "no such file"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "a picture.png").write_bytes(b"png")
    (tmp_path / "b.png").write_bytes(b"png")
    assert media_service.resolve("'~/a picture.png'").name == "a picture.png"
    assert media_service.resolve(f'"{tmp_path / "b.png"}"').name == "b.png"


@pytest.mark.parametrize("name", ["havoc.session", "havoc.db", "key.pem", "id_rsa.key", ".env"])
def test_secrets_are_never_sendable(tmp_path, name: str) -> None:
    """Even when the owner asks: a userbot must not exfiltrate its own keys."""
    path = tmp_path / name
    path.write_bytes(b"secret")
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve(str(path))
    assert excinfo.value.key == "media.blocked_type"


def test_missing_files_are_reported_without_a_traceback(tmp_path) -> None:
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve(str(tmp_path / "nope.jpg"))
    assert excinfo.value.key == "media.not_found"


def test_directories_are_refused(tmp_path) -> None:
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve(str(tmp_path))
    assert excinfo.value.key in {"media.is_directory", "media.unsupported_type"}


def test_empty_files_are_refused(tmp_path) -> None:
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve(str(path))
    assert excinfo.value.key == "media.empty_file"


def test_unknown_extensions_are_refused(tmp_path) -> None:
    path = tmp_path / "thing.xyz"
    path.write_bytes(b"data")
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve(str(path))
    assert excinfo.value.key == "media.unsupported_type"


def test_oversized_files_are_refused(tmp_path) -> None:
    path = _file(tmp_path, "big.mp4", 2048)
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve(str(path), max_bytes=1024)
    assert excinfo.value.key == "media.too_large"


def test_a_null_byte_cannot_smuggle_a_path(tmp_path) -> None:
    with pytest.raises(MediaError):
        media_service.resolve(str(tmp_path / "x.jpg") + "\x00.png")


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/a.png",
        "http://127.0.0.1/a.png",
        "http://10.0.0.5/a.png",
        "http://192.168.1.1/a.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/a.png",
        "http://0.0.0.0/a.png",
        "http://printer.local/a.png",
        "http://metadata.google.internal/x",
        "ftp://example.com/a.png",
        "file:///etc/passwd",
        "https://user:pass@example.com/a.png",
    ],
)
def test_internal_and_unsafe_urls_are_refused(url: str) -> None:
    with pytest.raises(MediaError):
        media_service.resolve(url)


@pytest.mark.parametrize("url", ["https://example.com/a.png", "http://cdn.example.org/pic.jpg?x=1"])
def test_public_urls_are_accepted(url: str) -> None:
    item = media_service.resolve(url)
    assert item.is_url is True and item.kind == "url"


def test_urls_can_be_switched_off() -> None:
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve("https://example.com/a.png", allow_urls=False)
    assert excinfo.value.key == "media.urls_disabled"


def test_absurd_urls_are_refused() -> None:
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve("https://example.com/" + "a" * 3000)
    assert excinfo.value.key == "media.url_too_long"


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
def test_an_album_is_validated_as_a_whole(tmp_path) -> None:
    good = _file(tmp_path, "a.png")
    with pytest.raises(MediaError):
        media_service.resolve_many([str(good), str(tmp_path / "missing.png")])


def test_an_album_has_a_ceiling(tmp_path) -> None:
    paths = [str(_file(tmp_path, f"p{i}.png")) for i in range(media_service.MAX_SOURCES + 1)]
    with pytest.raises(MediaError) as excinfo:
        media_service.resolve_many(paths)
    assert excinfo.value.key == "media.too_many"


def test_one_item_is_sent_alone_and_several_as_an_album(tmp_path) -> None:
    one = media_service.resolve(str(_file(tmp_path, "a.png")))
    two = media_service.resolve_many([str(_file(tmp_path, "b.png")), str(_file(tmp_path, "c.png"))])
    assert isinstance(media_service.as_telegram_input([one]), str)
    assert isinstance(media_service.as_telegram_input(two), list)


def test_summary_is_readable(tmp_path) -> None:
    item = media_service.resolve(str(_file(tmp_path)))
    text = media_service.summary([item])
    assert "photo" in text and "sunset.jpg" in text


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
async def test_pic_explains_itself_without_arguments(runtime) -> None:
    await invoke(runtime, ".pic")
    text = last_text(runtime)
    assert ".pic" in text
    assert "MB" in text
    assert "session" in text  # it says outright what it will not send


async def test_pic_refuses_while_the_feature_is_off(runtime) -> None:
    path = _file(runtime.config.data_dir)
    await invoke(runtime, f".pic {path}")
    assert "disabled" in last_text(runtime).lower()
    assert not [m for m in runtime.client.sent if "file" in m.kwargs]


async def test_pic_sends_a_real_file_when_enabled(runtime) -> None:
    await invoke(runtime, ".feature media on")
    path = _file(runtime.config.data_dir)
    await invoke(runtime, f'.pic {path} --caption "look at this"')

    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert len(files) == 1
    assert files[0].kwargs["file"] == str(path)
    assert files[0].text == "look at this"
    assert "Sent 1 item" in last_text(runtime)


async def test_pic_sends_an_album(runtime) -> None:
    await invoke(runtime, ".feature media on")
    first = _file(runtime.config.data_dir, "one.png")
    second = _file(runtime.config.data_dir, "two.png")
    await invoke(runtime, f".pic {first} {second}")

    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert len(files) == 1
    assert files[0].kwargs["file"] == [str(first), str(second)]


async def test_pic_never_sends_the_session_file(runtime) -> None:
    await invoke(runtime, ".feature media on")
    secret = runtime.config.session_file
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"session-bytes")
    await invoke(runtime, f".pic {secret}")
    assert "Refusing" in last_text(runtime)
    assert not [m for m in runtime.client.sent if "file" in m.kwargs]


async def test_pic_reports_a_bad_url(runtime) -> None:
    await invoke(runtime, ".feature media on")
    await invoke(runtime, ".pic http://127.0.0.1/secret.png")
    assert "not allowed" in last_text(runtime)
    assert not [m for m in runtime.client.sent if "file" in m.kwargs]


async def test_pic_sends_to_another_chat(runtime) -> None:
    await invoke(runtime, ".feature media on")
    path = _file(runtime.config.data_dir)
    await invoke(runtime, f".pic {path} --to 555")
    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert files and files[0].chat_id == 555


async def test_pic_is_dry_run_aware(runtime) -> None:
    await invoke(runtime, ".feature media on")
    await runtime.store.set_setting("runtime.dry_run", True)
    path = _file(runtime.config.data_dir)
    await invoke(runtime, f".pic {path}")
    assert "Dry-run" in last_text(runtime)
    assert not [m for m in runtime.client.sent if "file" in m.kwargs]
