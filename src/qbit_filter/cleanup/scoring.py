"""Shared scoring / selection helpers for cleanup rules.

Pure functions over a :class:`Store` snapshot that more than one rule needs:
age math, arr-current keeper selection, and per-season partitioning. Kept
separate from the per-rule modules so adding a rule never means copy-pasting
this logic, and separate from :mod:`qbit_filter.cleanup.factors` (which only
formats :class:`ReasonFactor` pills) because these encode selection logic,
not presentation.
"""

from __future__ import annotations

import time

from qbit_filter.domain import Group, GroupKind, Torrent
from qbit_filter.grouping.parser import quick_season
from qbit_filter.state.store import Store

# Window during which deleting a freeleech torrent typically incurs a
# tracker-side penalty. Used by ``DuplicateSameQualityRule`` to mark
# candidates whose pair is *both* freshly added, since dropping the newer
# one before this window can cost upload credit even when the older copy
# still seeds. Conservative default; per-tracker freeleech awareness is a
# followup (see plan: out-of-scope).
FREELEECH_PENALTY_WINDOW_DAYS = 10


def age_days(ts: int, now: int | None = None) -> int:
    base = now if now is not None else int(time.time())
    return max(0, (base - ts) // 86_400)


def pick_arr_current_keeper(bucket: list[Torrent], store: Store) -> Torrent | None:
    """Return the torrent in ``bucket`` that arr currently considers the
    live import (``ArrMatch.arr_current``), or ``None`` if none qualify.

    arr-current is set by the indexer when the hash is the source of the
    most-recent ``downloadFolderImported`` event for the owning entity --
    the file arr has on disk *right now*. After an upgrade, arr drops the
    older grab from that head, so absence is a direct "leftover" signal.

    When multiple torrents in the bucket are arr-current (rare; usually
    only happens for Sonarr when a single bucket spans episodes that arr
    imported from different torrents), the most-recently-added wins so
    the keeper picks up release-group / indexer metadata from the freshest
    grab. Bucket order is assumed newest-first by the caller.
    """
    if store.arr is None or not store.arr.hash_to_arr:
        return None
    for t in bucket:
        match = store.arr.hash_to_arr.get(t.hash.lower())
        if match is not None and match.arr_current:
            return t
    return None


def partition_by_season(group: Group, store: Store) -> list[list[Torrent]]:
    """Partition a group's torrents into per-season buckets for TV groups.

    TV shows can hold multiple seasons under one group; running a "best tier
    in the group" comparison across seasons wrongly flags e.g. an S02 1080p
    when an unrelated S01 2160p sits alongside. For TV groups we bucket by
    :func:`quick_season` (cheap regex), keeping torrents with no detectable
    season -- typically full-series packs -- in their own bucket so they
    only compare against other no-season torrents.

    Movie and OTHER groups return a single all-in bucket; season scoping is
    meaningless there.
    """
    torrents = [
        store.torrents[h] for h in group.torrent_hashes if h in store.torrents
    ]
    if group.kind is not GroupKind.TV:
        return [torrents]
    buckets: dict[int | None, list[Torrent]] = {}
    for t in torrents:
        buckets.setdefault(quick_season(t.name), []).append(t)
    return list(buckets.values())
