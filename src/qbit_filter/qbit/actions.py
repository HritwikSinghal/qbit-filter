"""Async wrappers around qbittorrent-api action calls.

All functions take a logged-in client (from :func:`qbit.client.connect`).
The library is sync; we hop to a worker thread so the FastAPI event loop
stays responsive. qBit's REST API is idempotent for these actions (pausing
an already-paused torrent is a no-op), so no state-check is needed here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import qbittorrentapi

logger = logging.getLogger(__name__)


def _hashes_arg(hashes: str | list[str] | tuple[str, ...]) -> str:
    """qBittorrent's WebUI accepts hashes as ``|``-separated string. Normalise
    anything iterable down to that form so bulk and single call-sites share
    one wire format."""
    if isinstance(hashes, str):
        return hashes
    return "|".join(h for h in hashes if h)


async def pause(
    client: qbittorrentapi.Client, torrent_hashes: str | list[str] | tuple[str, ...]
) -> None:
    """Pause one or many torrents. qBit 5.x renamed pause to stop; the library
    exposes both -- prefer ``torrents_stop`` when present and fall back to
    ``torrents_pause`` on older instances."""
    arg = _hashes_arg(torrent_hashes)

    def _do() -> None:
        fn = getattr(client, "torrents_stop", None)
        if fn is None:
            fn = client.torrents_pause
        fn(torrent_hashes=arg)

    await asyncio.to_thread(_do)


async def resume(
    client: qbittorrentapi.Client, torrent_hashes: str | list[str] | tuple[str, ...]
) -> None:
    arg = _hashes_arg(torrent_hashes)

    def _do() -> None:
        fn = getattr(client, "torrents_start", None)
        if fn is None:
            fn = client.torrents_resume
        fn(torrent_hashes=arg)

    await asyncio.to_thread(_do)


async def recheck(
    client: qbittorrentapi.Client, torrent_hashes: str | list[str] | tuple[str, ...]
) -> None:
    arg = _hashes_arg(torrent_hashes)
    await asyncio.to_thread(client.torrents_recheck, torrent_hashes=arg)


async def delete(
    client: qbittorrentapi.Client,
    torrent_hashes: str | list[str] | tuple[str, ...],
    *,
    purge: bool = False,
) -> None:
    """Remove one or many torrents. ``purge=True`` also deletes files on disk."""
    arg = _hashes_arg(torrent_hashes)
    await asyncio.to_thread(
        client.torrents_delete,
        delete_files=purge,
        torrent_hashes=arg,
    )
