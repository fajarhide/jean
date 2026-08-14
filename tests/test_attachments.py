from __future__ import annotations

from pathlib import Path

from jean.db.memory import MemoryStore
from jean.gateway.app import Gateway
from jean.gateway.attachments import MAX_BYTES, InboundFile, fetch_attachments, safe_name
from jean.persona.model import Identity, Manager, SoulData


class FakeDownloader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def download(self, url: str, dest: str) -> None:
        self.calls.append((url, dest))
        if self.error is not None:
            raise self.error
        Path(dest).write_bytes(b"png-bytes")


def _file(**kwargs) -> InboundFile:
    defaults = dict(id="F1", name="screenshot.png", url="https://files.example/F1", size=12)
    defaults.update(kwargs)
    return InboundFile(**defaults)


async def test_downloads_the_file_and_hands_back_its_local_path(tmp_path):
    downloader = FakeDownloader()

    attachments = await fetch_attachments(downloader, [_file()], dest_dir=tmp_path / "111.0")

    [attachment] = attachments
    assert attachment.error is None
    assert Path(attachment.path).read_bytes() == b"png-bytes"
    assert Path(attachment.path).parent == tmp_path / "111.0"


async def test_the_uploader_cannot_name_a_file_out_of_the_directory(tmp_path):
    """The name is whatever the uploader typed, so it is untrusted input."""
    downloader = FakeDownloader()

    [attachment] = await fetch_attachments(
        downloader, [_file(name="../../../etc/passwd")], dest_dir=tmp_path / "111.0"
    )

    # Resolved, not just joined: `..` in the name would climb out at open() time.
    assert Path(attachment.path).resolve().parent == (tmp_path / "111.0").resolve()


def test_safe_name_keeps_the_extension_a_reader_needs():
    assert safe_name("my shot.png") == "my_shot.png"


async def test_two_files_sharing_a_name_do_not_overwrite_each_other(tmp_path):
    downloader = FakeDownloader()

    first, second = await fetch_attachments(
        downloader,
        [_file(id="F1", name="shot.png"), _file(id="F2", name="shot.png")],
        dest_dir=tmp_path / "111.0",
    )

    assert first.path != second.path


async def test_oversize_file_is_reported_rather_than_downloaded(tmp_path):
    downloader = FakeDownloader()

    [attachment] = await fetch_attachments(
        downloader, [_file(size=MAX_BYTES + 1)], dest_dir=tmp_path / "111.0"
    )

    assert downloader.calls == []
    assert attachment.path == ""
    assert "too large" in (attachment.error or "")


async def test_a_failed_download_is_told_to_the_agent_not_swallowed(tmp_path):
    """Silently dropping it is the worst outcome: the human sees an image in the
    thread and the agent answers as if nothing was attached. `files:read` missing
    from the Slack app lands here, and this is what makes it say so."""
    downloader = FakeDownloader(error=RuntimeError("missing_scope"))

    [attachment] = await fetch_attachments(downloader, [_file()], dest_dir=tmp_path / "111.0")

    assert attachment.path == ""
    assert "missing_scope" in (attachment.error or "")


async def test_a_file_slack_gave_no_url_for_is_reported(tmp_path):
    downloader = FakeDownloader()

    [attachment] = await fetch_attachments(downloader, [_file(url="")], dest_dir=tmp_path / "111.0")

    assert downloader.calls == []
    assert attachment.error


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def handle(self, channel: str, thread_ts: str, text: str) -> None:
        self.calls.append((channel, thread_ts, text))


class FakeGate:
    async def handle_action(self, action_id: str, user_id: str) -> str:
        return "approved"


def _gateway(tmp_path, downloader):
    manager = FakeManager()
    gw = Gateway(
        store=MemoryStore(),
        manager=manager,
        gate=FakeGate(),
        bot_id="UBOT",
        soul_provider=lambda: SoulData(
            identity=Identity(name="jean"), manager=Manager(user_id="U00001")
        ),
        chat=downloader,
        attachments_dir=tmp_path,
    )
    return gw, manager


async def test_a_mention_with_a_file_reaches_the_agent_as_a_local_path(tmp_path):
    downloader = FakeDownloader()
    gw, manager = _gateway(tmp_path, downloader)

    await gw.on_mention(
        channel="C1",
        thread_ts="111.0",
        text="<@UBOT> what is wrong here",
        author_id="U11111",
        files=[_file()],
    )

    [(_channel, _thread, turn)] = manager.calls
    assert '<attachment name="screenshot.png" path="' in turn
    assert len(downloader.calls) == 1


async def test_a_bystanders_upload_is_never_downloaded(tmp_path):
    """Engagement decides first. A file posted by someone the agent is not
    talking to must cost nothing: no fetch, no disk, no turn."""
    downloader = FakeDownloader()
    gw, manager = _gateway(tmp_path, downloader)

    await gw.on_message("C1", "111.0", "here you go", "U22222", False, files=[_file()])

    assert manager.calls == []
    assert downloader.calls == []
