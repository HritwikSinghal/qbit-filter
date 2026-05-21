"""Canonical in-memory state. Mutated only by ``reconciler.py``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qbit_filter.domain import Group, GroupKey, Torrent

if TYPE_CHECKING:
    from qbit_filter.state.arr_store import ArrStore


@dataclass(slots=True)
class Store:
    torrents: dict[str, Torrent] = field(default_factory=dict)
    groups: dict[GroupKey, Group] = field(default_factory=dict)
    hash_to_key: dict[str, GroupKey] = field(default_factory=dict)
    categories: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    trackers: set[str] = field(default_factory=set)
    rid: int = 0
    # Memoisation slot for ``views.count_by_facet`` keyed by ``rid``.
    # Lives on the Store so the cache doesn't outlive its store (avoids the
    # ``id()``-reuse hazard a module-level dict would have).
    facet_cache: tuple[int, dict[str, Any]] | None = None
    # Pointer to the *arr enrichment store. Owned by the *arr poller task
    # in ``app.py``; this is a read-only handle from the qBit store's POV.
    # None when no *arr instance is configured.
    arr: ArrStore | None = None

    def snapshot_groups(self) -> list[Group]:
        return list(self.groups.values())

    def torrents_in(self, key: GroupKey) -> list[Torrent]:
        group = self.groups.get(key)
        if not group:
            return []
        return [self.torrents[h] for h in group.torrent_hashes if h in self.torrents]
