from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jean.gateway.dispatch import Attachment

logger = logging.getLogger(__name__)

# Nothing the agent can usefully read is bigger than this, and the pod's disk is
# shared with every thread's transcript, so an oversize upload is reported rather
# than fetched. Slack itself allows 1GB.
MAX_BYTES = 20 * 1024 * 1024

# Everything outside this becomes `_`: the name is whatever the uploader called
# the file, so it decides a path on our disk and is untrusted for that reason.
# `/` and `..` are the ones that matter; quotes and angle brackets go too,
# because the name is also rendered into the `<attachment .../>` envelope.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class InboundFile:
    """A file Slack says is attached to a message.

    `url` is `url_private`, which is not public: fetching it needs the bot token
    and the `files:read` scope, so only the Slack adapter can resolve it."""

    id: str
    name: str
    url: str
    size: int = 0


class Downloader(Protocol):
    async def download(self, url: str, dest: str) -> None: ...


def safe_name(name: str) -> str:
    """A filename safe to join onto a directory we own. Keeps the extension,
    because that is what tells a reader (and the model) what the file is."""
    cleaned = _UNSAFE.sub("_", name).lstrip(".")
    return cleaned[:100] or "file"


async def fetch_attachments(
    downloader: Downloader, files: Sequence[InboundFile], *, dest_dir: Path
) -> list[Attachment]:
    """Pull each attached file onto local disk and describe it for the turn.

    Every file comes back one way or another. A skipped or failed download is
    returned as an Attachment carrying `error` instead of a path, because
    dropping it silently is the worst outcome available: the human sees an image
    in the thread and the agent answers as though the message had none.
    """
    attachments: list[Attachment] = []
    for file in files:
        name = safe_name(file.name)
        if not file.url:
            attachments.append(Attachment(name=name, path="", error="slack sent no url for it"))
            continue
        if file.size > MAX_BYTES:
            attachments.append(
                Attachment(name=name, path="", error=f"too large ({file.size} bytes)")
            )
            continue
        # The id prefix keeps two files with the same name in one thread apart.
        dest = dest_dir / f"{file.id}-{name}"
        try:
            await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)
            await downloader.download(file.url, str(dest))
        except Exception as exc:
            logger.warning("attachment %s (%s) download failed: %s", file.id, name, exc)
            attachments.append(Attachment(name=name, path="", error=f"download failed: {exc}"))
            continue
        attachments.append(Attachment(name=name, path=str(dest)))
    return attachments
