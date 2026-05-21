"""qBit ``sync/maindata`` polling + delta normalisation.

``poll()`` is an async generator that yields :class:`MainDataDelta` once per
``poll_interval_seconds``. ``normalise()`` is the pure mapping from a raw qBit
payload to a delta and is the unit-tested seam.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import qbittorrentapi

from qbit_filter.config import Settings
from qbit_filter.domain import MainDataDelta

logger = logging.getLogger(__name__)


def normalise(
    raw: dict[str, Any],
    known_hashes: set[str] | None = None,
) -> MainDataDelta:
    """Convert a raw ``sync/maindata`` payload to a :class:`MainDataDelta`.

    qBit semantics:
    - ``full_update=True`` means everything in ``torrents`` is canonical state.
    - Without ``full_update``, ``torrents`` contains only changed fields per hash.
    - ``torrents_removed`` lists hashes that disappeared since last RID.
    """
    full = bool(raw.get("full_update"))
    rid = int(raw.get("rid", 0))
    torrents: dict[str, dict[str, Any]] = raw.get("torrents") or {}
    removed: set[str] = set(raw.get("torrents_removed") or [])

    categories = raw.get("categories") or {}
    tags = raw.get("tags") or []
    trackers = raw.get("trackers") or {}

    if full:
        return MainDataDelta(
            full_update=True,
            rid=rid,
            added=dict(torrents),
            changed={},
            removed=set(),
            categories_added=set(categories.keys()),
            tags_added=set(tags),
            trackers_added=set(trackers.keys()),
        )

    known = known_hashes or set()
    added: dict[str, dict[str, Any]] = {}
    changed: dict[str, dict[str, Any]] = {}
    for h, fields in torrents.items():
        if h in known:
            changed[h] = fields
        else:
            added[h] = fields

    return MainDataDelta(
        full_update=False,
        rid=rid,
        added=added,
        changed=changed,
        removed=removed,
        categories_added=set(categories.keys()),
        categories_removed=set(raw.get("categories_removed") or []),
        tags_added=set(tags),
        tags_removed=set(raw.get("tags_removed") or []),
        trackers_added=set(trackers.keys()),
        trackers_removed=set(raw.get("trackers_removed") or []),
    )


async def poll(
    client: qbittorrentapi.Client,
    settings: Settings,
) -> AsyncIterator[MainDataDelta]:
    """Indefinitely yield ``MainDataDelta``s every ``poll_interval_seconds``.

    Survives transient qBit errors by logging and re-polling on the next tick.
    A full-update tick rebuilds the local ``known`` hash set; incremental
    ticks fold ``added`` in and ``removed`` out so the next call's
    added-vs-changed partitioning stays correct.
    """
    rid = 0
    known: set[str] = set()
    interval = settings.poll_interval_seconds

    while True:
        try:
            raw = dict(await asyncio.to_thread(client.sync_maindata, rid=rid))
            delta = normalise(raw, known_hashes=known)
            rid = delta.rid

            if delta.full_update:
                known = set(delta.added.keys())
            else:
                known |= set(delta.added.keys())
                known -= delta.removed

            yield delta
        except Exception as exc:
            logger.warning("sync/maindata poll failed: %s", exc)

        await asyncio.sleep(interval)
