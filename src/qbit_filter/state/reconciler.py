"""Reconciler -- sole mutator of :class:`Store`. Consumes deltas, emits events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from qbit_filter.config import Settings
from qbit_filter.domain import (
    DomainEvent,
    EventKind,
    Group,
    GroupKey,
    MainDataDelta,
    Torrent,
    TorrentStatus,
)
from qbit_filter.grouping.grouper import assign
from qbit_filter.grouping.parser import ParsedName, parse
from qbit_filter.grouping.quality import parse_quality
from qbit_filter.state.events import EventBus
from qbit_filter.state.store import Store

logger = logging.getLogger(__name__)


_QBIT_STATE_MAP: dict[str, TorrentStatus] = {
    "error": TorrentStatus.ERRORED,
    "missingFiles": TorrentStatus.ERRORED,
    "uploading": TorrentStatus.SEEDING,
    "pausedUP": TorrentStatus.PAUSED,
    "queuedUP": TorrentStatus.QUEUED,
    "stalledUP": TorrentStatus.SEEDING,
    "checkingUP": TorrentStatus.CHECKING,
    "forcedUP": TorrentStatus.SEEDING,
    "allocating": TorrentStatus.DOWNLOADING,
    "downloading": TorrentStatus.DOWNLOADING,
    "metaDL": TorrentStatus.DOWNLOADING,
    "pausedDL": TorrentStatus.PAUSED,
    "queuedDL": TorrentStatus.QUEUED,
    "stalledDL": TorrentStatus.STALLED,
    "checkingDL": TorrentStatus.CHECKING,
    "forcedDL": TorrentStatus.DOWNLOADING,
    "checkingResumeData": TorrentStatus.CHECKING,
    "moving": TorrentStatus.CHECKING,
    "unknown": TorrentStatus.ERRORED,
    # qBit 5.x: paused* renamed to stopped*.
    "stoppedUP": TorrentStatus.PAUSED,
    "stoppedDL": TorrentStatus.PAUSED,
}


def _parse_tags(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    if isinstance(raw, list | tuple | set | frozenset):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    return ()


def _torrent_from_raw(h: str, raw: dict[str, Any]) -> Torrent:
    state_raw = str(raw.get("state") or "unknown")
    state = _QBIT_STATE_MAP.get(state_raw, TorrentStatus.DOWNLOADING)
    tracker = str(raw.get("tracker") or "")
    name = str(raw.get("name") or "")
    return Torrent(
        hash=h,
        name=name,
        size=int(raw.get("size") or 0),
        progress=float(raw.get("progress") or 0.0),
        state=state,
        category=str(raw.get("category") or ""),
        tags=_parse_tags(raw.get("tags")),
        trackers=(tracker,) if tracker else (),
        dlspeed=int(raw.get("dlspeed") or 0),
        upspeed=int(raw.get("upspeed") or 0),
        eta=int(raw.get("eta") or -1),
        added_on=int(raw.get("added_on") or 0),
        last_activity=int(raw.get("last_activity") or 0),
        ratio=float(raw.get("ratio") or 0.0),
        raw_state=state_raw,
        quality=parse_quality(name),
    )


def _warm_parse_cache(names: list[str]) -> None:
    """Populate :func:`qbit_filter.grouping.parser.parse`'s ``lru_cache`` for
    a batch of torrent names off the event loop. Subsequent ``parse(name)``
    calls in the reconciler become cache lookups (~1us each).

    Also warms :func:`parse_quality` for the same names so the quality
    lru_cache is hot when the reconciler builds Torrent objects.
    """
    for name in names:
        if name:
            parse(name)
            parse_quality(name)


def _patch_torrent(t: Torrent, raw: dict[str, Any]) -> Torrent:
    """Return a new :class:`Torrent` with ``raw``'s changed fields merged in."""
    state_raw = str(raw.get("state", t.raw_state)) if "state" in raw else t.raw_state
    state = _QBIT_STATE_MAP.get(state_raw, t.state)
    tags: tuple[str, ...] = _parse_tags(raw.get("tags")) if "tags" in raw else t.tags
    trackers: tuple[str, ...]
    if "tracker" in raw:
        tracker = str(raw.get("tracker") or "")
        trackers = (tracker,) if tracker else ()
    else:
        trackers = t.trackers
    new_name = str(raw.get("name", t.name))
    return Torrent(
        hash=t.hash,
        name=new_name,
        size=int(raw.get("size", t.size)),
        progress=float(raw.get("progress", t.progress)),
        state=state,
        category=str(raw.get("category", t.category)),
        tags=tags,
        trackers=trackers,
        dlspeed=int(raw.get("dlspeed", t.dlspeed)),
        upspeed=int(raw.get("upspeed", t.upspeed)),
        eta=int(raw.get("eta", t.eta)),
        added_on=int(raw.get("added_on", t.added_on)),
        last_activity=int(raw.get("last_activity", t.last_activity)),
        ratio=float(raw.get("ratio", t.ratio)),
        raw_state=state_raw,
        quality=parse_quality(new_name),
    )


@dataclass
class Reconciler:
    store: Store
    bus: EventBus
    settings: Settings

    async def apply(self, delta: MainDataDelta) -> None:
        # Names that will need a guessit parse this tick. Pre-warming the
        # lru_cache off-thread keeps the event loop responsive during a
        # cold-start full_update (~1310 names x 1-3ms = several seconds
        # of blocking on first poll without this).
        names: list[str] = []
        if delta.full_update:
            names.extend(
                str(raw.get("name") or "") for raw in delta.added.values()
            )
        else:
            names.extend(
                str(raw.get("name") or "") for raw in delta.added.values()
            )
            names.extend(
                str(raw.get("name") or "") for raw in delta.changed.values()
                if "name" in raw or "category" in raw or "tags" in raw
            )
        if names:
            await asyncio.to_thread(_warm_parse_cache, names)

        self.store.categories |= delta.categories_added
        self.store.categories -= delta.categories_removed
        self.store.tags |= delta.tags_added
        self.store.tags -= delta.tags_removed
        self.store.trackers |= delta.trackers_added
        self.store.trackers -= delta.trackers_removed
        # Invalidate any memoised facet counts before we mutate. Otherwise a
        # concurrent ``count_by_facet`` reader can pin a cache entry to the
        # in-progress rid and see stale counts until the next poll. Setting
        # store.rid happens AFTER mutation for the same reason.
        self.store.facet_cache = None

        if delta.full_update:
            self._rebuild(delta)
            self.store.rid = delta.rid
            return

        for h, raw in delta.added.items():
            self._add(h, dict(raw))
        for h, raw in delta.changed.items():
            self._update(h, dict(raw))
        for h in delta.removed:
            self._remove(h)
        self.store.rid = delta.rid

    def _rebuild(self, delta: MainDataDelta) -> None:
        self.store.torrents.clear()
        self.store.groups.clear()
        self.store.hash_to_key.clear()
        for h, raw in delta.added.items():
            t = _torrent_from_raw(h, dict(raw))
            self.store.torrents[h] = t
            key, parsed = self._classify(t)
            self._attach(h, t, key, parsed)
        self.bus.publish(DomainEvent(kind=EventKind.RESYNC))

    def _classify(self, t: Torrent) -> tuple[GroupKey, ParsedName]:
        """Single ``parse(t.name)`` pass shared between key assignment and
        the new-group title that ``_attach`` may need."""
        parsed = parse(t.name)
        key = assign(
            t,
            parsed,
            movie_categories=self.settings.movie_categories,
            tv_categories=self.settings.tv_categories,
        )
        return key, parsed

    def _attach(
        self, h: str, t: Torrent, key: GroupKey, parsed: ParsedName
    ) -> None:
        """Place ``h`` under ``key``. Emits exactly one of GROUP_ADDED
        (new card) or TORRENT_ADDED (row insert + count bump on existing
        card). Whole-card re-renders are reserved for genuinely new
        groups."""
        self.store.hash_to_key[h] = key
        group = self.store.groups.get(key)
        if group is None:
            group = Group(
                key=key,
                title=parsed.title or t.name,
                year=key.year,
                kind=key.kind,
            )
            group.torrent_hashes.append(h)
            self.store.groups[key] = group
            self.bus.publish(DomainEvent(kind=EventKind.GROUP_ADDED, group_key=key))
            return
        if h in group.torrent_hashes:
            return
        group.torrent_hashes.append(h)
        self.bus.publish(
            DomainEvent(kind=EventKind.TORRENT_ADDED, group_key=key, torrent_hash=h)
        )

    def _detach(self, h: str, key: GroupKey) -> None:
        """Remove ``h`` from group ``key``. Emits GROUP_REMOVED if the group
        is now empty, otherwise TORRENT_REMOVED (row delete + count bump)."""
        group = self.store.groups.get(key)
        if group is None:
            return
        if h in group.torrent_hashes:
            group.torrent_hashes.remove(h)
        if not group.torrent_hashes:
            del self.store.groups[key]
            self.bus.publish(DomainEvent(kind=EventKind.GROUP_REMOVED, group_key=key))
            return
        self.bus.publish(
            DomainEvent(kind=EventKind.TORRENT_REMOVED, group_key=key, torrent_hash=h)
        )

    def _add(self, h: str, raw: dict[str, Any]) -> None:
        t = _torrent_from_raw(h, raw)
        self.store.torrents[h] = t
        key, parsed = self._classify(t)
        self._attach(h, t, key, parsed)

    def _update(self, h: str, raw: dict[str, Any]) -> None:
        existing = self.store.torrents.get(h)
        if existing is None:
            self._add(h, raw)
            return
        patched = _patch_torrent(existing, raw)
        self.store.torrents[h] = patched
        old_key = self.store.hash_to_key[h]
        # The only delta fields that can change a group key are name,
        # category, and tags -- skip the reparse for everything else
        # (progress, dlspeed, upspeed, eta, ...). parse() is also lru_cached
        # so an unchanged-name reparse is a hash lookup, but we still avoid
        # invoking classify() when it can't change anything.
        if "name" in raw or "category" in raw or "tags" in raw:
            new_key, new_parsed = self._classify(patched)
            if new_key != old_key:
                # Membership shift between two groups. _detach + _attach
                # emit the surgical TORRENT_REMOVED / TORRENT_ADDED pair
                # (or GROUP_REMOVED / GROUP_ADDED for boundary cases) --
                # no whole-card re-render needed for either side.
                self._detach(h, old_key)
                self._attach(h, patched, new_key, new_parsed)
                return
        # Same-group update: only the torrent row changes. The group card
        # shows title / year / kind / count -- none of those drift on
        # speed / progress updates, so this stays a per-row swap.
        self.bus.publish(
            DomainEvent(kind=EventKind.TORRENT_CHANGED, group_key=old_key, torrent_hash=h)
        )

    def _remove(self, h: str) -> None:
        key = self.store.hash_to_key.pop(h, None)
        self.store.torrents.pop(h, None)
        if key is not None:
            self._detach(h, key)
