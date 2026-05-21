"""Reconciler -- sole mutator of :class:`Store`. Consumes deltas, emits events."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field
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
from qbit_filter.grouping import parser
from qbit_filter.grouping.grouper import assign
from qbit_filter.grouping.parser import ParsedName, parse
from qbit_filter.grouping.quality import parse_quality
from qbit_filter.state.events import EventBus
from qbit_filter.state.store import Store

logger = logging.getLogger(__name__)

# Above this many uncached names per warm call, spread the work across a
# ``ProcessPoolExecutor``. Below it, the IPC + pickling overhead outweighs
# the CPU parallelism: the in-thread loop wins for the steady-state case
# where almost every name is already cached. The cold-boot full-update
# is the only realistic scenario where this kicks in -- exactly where
# the multi-second blocking would otherwise hurt most.
_POOL_THRESHOLD = 64

# Fields that change the group-key, the status badge, or any other row
# property the user reads when deciding what to act on. A delta touching
# any of these fires TORRENT_CHANGED immediately.
_STRUCTURAL_FIELDS = frozenset(
    {"name", "category", "tags", "state", "tracker"}
)

# Per-torrent transient updates (dlspeed/upspeed/progress/eta/ratio/
# last_activity/size/added_on) are coalesced to one render every
# TRANSIENT_COALESCE_SECONDS. qBit polls at 1 s; without this, a busy
# seedbox emits hundreds of full-row HTML re-renders per second over SSE.
# The store is still patched on every poll so reads see fresh values --
# only the event emission is throttled.
TRANSIENT_COALESCE_SECONDS = 5.0


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


def _pool_parse_one(name: str) -> ParsedName:
    """Subprocess entry point. The parent sends only the raw name string
    and receives a ``ParsedName`` frozen dataclass back."""
    return parser.parse_uncached(name)


def _warm_parse_cache(names: list[str]) -> None:
    """Populate the parser cache for a batch of torrent names off the event
    loop. Subsequent ``parse(name)`` calls become dict lookups (~100 ns).

    Names already in the cache (typical for warm boots, where the
    persistent disk cache hydrated them) cost nothing. Of the rest, when
    the batch is big enough to amortise IPC overhead, fan out to a
    ``ProcessPoolExecutor`` so multiple CPU cores share the guessit
    work; the GIL would otherwise serialise everything on one thread.

    Also warms :func:`parse_quality` for the same names so the quality
    lru_cache is hot when the reconciler builds Torrent objects.
    """
    uncached = [n for n in names if n and not parser.is_cached(n)]
    if uncached and len(uncached) >= _POOL_THRESHOLD:
        # Up to N-1 cores -- leave one for the event loop + reconciler.
        workers = max(1, min((os.cpu_count() or 2) - 1, 8))
        try:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers
            ) as pool:
                # ``map`` preserves input order, so zip by index is safe.
                results = list(
                    pool.map(_pool_parse_one, uncached, chunksize=32)
                )
            for name, parsed_name in zip(uncached, results, strict=True):
                parser.prime(name, parsed_name)
        except Exception:
            # Pool fan-out is a best-effort speedup; fall through to the
            # in-thread path so a broken multiprocessing environment
            # just costs latency, not correctness.
            logger.exception(
                "parse pool fan-out failed; falling back to in-thread"
            )
            for name in uncached:
                parse(name)
    else:
        for name in uncached:
            parse(name)
    for name in names:
        if name:
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
    # Monotonic timestamp of the last TORRENT_CHANGED emitted for each
    # hash, used to coalesce transient-only updates (see _update).
    _transient_emit_at: dict[str, float] = field(default_factory=dict)

    async def apply(self, delta: MainDataDelta) -> None:
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
            # Cold-boot (rid==0) goes through the chunked path so the first
            # ~200 torrents reach the browser within a few hundred ms instead
            # of waiting for the full ~1310-torrent rebuild. Reconnect-time
            # full_updates (rid bumps but store is already populated) use the
            # one-shot path because they're rarer and a streamed mid-session
            # rebuild would visibly flash the page.
            chunk_size = self.settings.qbit_cold_boot_chunk_size
            is_cold_boot = self.store.rid == 0
            should_chunk = (
                is_cold_boot
                and chunk_size > 0
                and len(delta.added) > chunk_size
            )
            if should_chunk:
                await self._rebuild_chunked(delta, chunk_size)
            else:
                names = [
                    str(raw.get("name") or "") for raw in delta.added.values()
                ]
                if names:
                    await asyncio.to_thread(_warm_parse_cache, names)
                self._rebuild(delta)
                if is_cold_boot:
                    # One-shot cold boot (small library, didn't trip the
                    # chunk threshold). Mark done so the SSE renderer drops
                    # the progress block instead of leaving the placeholder.
                    self.store.cold_boot_total = len(self.store.torrents)
                    self.store.cold_boot_processed = len(self.store.torrents)
                    self.store.cold_boot_done = True
            self.store.rid = delta.rid
            return

        # Delta path: warm parse cache for changed names before mutating.
        names = [str(raw.get("name") or "") for raw in delta.added.values()]
        names.extend(
            str(raw.get("name") or "") for raw in delta.changed.values()
            if "name" in raw or "category" in raw or "tags" in raw
        )
        if names:
            await asyncio.to_thread(_warm_parse_cache, names)

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
        self._transient_emit_at.clear()
        for h, raw in delta.added.items():
            t = _torrent_from_raw(h, dict(raw))
            self.store.torrents[h] = t
            key, parsed = self._classify(t)
            self._attach(h, t, key, parsed)
        self.bus.publish(DomainEvent(kind=EventKind.RESYNC))

    async def _rebuild_chunked(
        self, delta: MainDataDelta, chunk_size: int
    ) -> None:
        """Cold-boot path: parse + group in chunks, publishing a partial
        RESYNC after each. The SSE handler treats RESYNC_PARTIAL like RESYNC
        but bypasses the coalesce window so each chunk reaches the browser
        as soon as it lands.

        Skips the GROUP_ADDED / TORRENT_ADDED publishes that ``_attach``
        would normally do -- the per-chunk RESYNC_PARTIAL already covers
        every group that exists in the store at that point.
        """
        self.store.torrents.clear()
        self.store.groups.clear()
        self.store.hash_to_key.clear()
        self._transient_emit_at.clear()
        items = list(delta.added.items())
        total = len(items)
        # Stamp the qBit total up-front so the SSE progress UI has a stable
        # denominator. The bar then climbs monotonically across partials
        # instead of resetting per chunk. Cleared on the final chunk so a
        # subsequent reconnect-time RESYNC doesn't re-paint the progress
        # block.
        self.store.cold_boot_total = total
        self.store.cold_boot_processed = 0
        self.store.cold_boot_done = False
        # Append rather than replace -- the qBit poller has already logged
        # "Connecting to..." / "Connected" lines that the user needs to see
        # alongside the chunked-parse progress.
        self.store.cold_boot_log.append(
            f"qBittorrent sync received -- {total} torrents incoming"
        )
        idx = 0
        while idx < total:
            end = min(idx + chunk_size, total)
            chunk = items[idx:end]
            # Warm guessit lru_cache for this chunk's names off-loop. With
            # the chunk size at 200 the per-chunk warm completes in ~50-150 ms
            # and the event loop yields between chunks so SSE renders can fan
            # out the previous chunk while this one is parsing.
            names = [str(raw.get("name") or "") for _, raw in chunk]
            if names:
                await asyncio.to_thread(_warm_parse_cache, names)
            for h, raw in chunk:
                t = _torrent_from_raw(h, dict(raw))
                self.store.torrents[h] = t
                key, parsed = self._classify(t)
                # Inline _attach without bus.publish -- the RESYNC_PARTIAL
                # at the end of the chunk covers it.
                self.store.hash_to_key[h] = key
                group = self.store.groups.get(key)
                if group is None:
                    self.store.groups[key] = Group(
                        key=key,
                        title=parsed.title or t.name,
                        year=key.year,
                        kind=key.kind,
                    )
                self.store.groups[key].torrent_hashes.append(h)
            # Bump rid per chunk so any concurrent ``count_by_facet`` reader
            # invalidates and recomputes against the partial-but-consistent
            # snapshot rather than serving stale counts.
            self.store.rid += 1
            self.store.facet_cache = None
            is_final = end == total
            self.store.cold_boot_processed = end
            group_count = len(self.store.groups)
            # Trim the log so the box stays compact (CSS max-height also
            # caps it, but a short list keeps the SSE payload small).
            self.store.cold_boot_log.append(
                f"Parsed {end} of {total} torrents -> {group_count} groups"
            )
            if len(self.store.cold_boot_log) > 16:
                del self.store.cold_boot_log[: len(self.store.cold_boot_log) - 16]
            if is_final:
                self.store.cold_boot_done = True
                self.store.cold_boot_log.append(
                    f"Cold boot complete -- {group_count} groups ready"
                )
            kind = EventKind.RESYNC if is_final else EventKind.RESYNC_PARTIAL
            logger.debug(
                "cold-boot chunk: %d/%d torrents, %d groups, kind=%s",
                end,
                total,
                group_count,
                kind.name,
            )
            self.bus.publish(DomainEvent(kind=kind))
            idx = end

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
        is_structural = not _STRUCTURAL_FIELDS.isdisjoint(raw)
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
                self._transient_emit_at.pop(h, None)
                return
        # Same-group update: only the torrent row changes. Structural
        # changes (state badge, category chip, name) fire immediately;
        # transient-only ticks (speed/progress/eta/ratio) are coalesced
        # to one emission per TRANSIENT_COALESCE_SECONDS so a busy
        # seedbox doesn't flood SSE with row re-renders the user can't
        # act on. The store mutation above is unchanged, so any
        # non-event reader sees fresh values either way.
        now = time.monotonic()
        if not is_structural:
            last = self._transient_emit_at.get(h, 0.0)
            if now - last < TRANSIENT_COALESCE_SECONDS:
                return
        self._transient_emit_at[h] = now
        self.bus.publish(
            DomainEvent(kind=EventKind.TORRENT_CHANGED, group_key=old_key, torrent_hash=h)
        )

    def _remove(self, h: str) -> None:
        key = self.store.hash_to_key.pop(h, None)
        self.store.torrents.pop(h, None)
        self._transient_emit_at.pop(h, None)
        if key is not None:
            self._detach(h, key)
