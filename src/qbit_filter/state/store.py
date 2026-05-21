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
    # Cold-boot streaming state. The reconciler's chunked path stamps the
    # qBit total here when a first full_update lands so the SSE progress UI
    # has a stable denominator across partials (otherwise the bar moves
    # backwards as later chunks reveal more groups than the first one knew
    # about). ``cold_boot_done`` flips once the final chunk publishes its
    # RESYNC; the SSE renderer uses it to drop the progress block.
    # ``cold_boot_log`` is the shared activity log shown under the bar --
    # appended to by the reconciler (per chunk) and the arr poller (first
    # successful index) so the user can see what's happening between
    # chunks.
    cold_boot_total: int = 0
    cold_boot_processed: int = 0
    cold_boot_done: bool = False
    cold_boot_log: list[str] = field(default_factory=list)
    # qBit connection telemetry surfaced by the activity dialog. Mutated by
    # ``app._poller`` on connect/disconnect and by ``Reconciler.apply`` on
    # every successful poll cycle. ``qbit_last_poll_at`` is a wall-clock
    # timestamp (``time.time()``) so the browser can render it as
    # "N seconds ago" relative to its own clock; monotonic time would be
    # meaningless across the process / client boundary. ``qbit_last_error``
    # is cleared on the next successful poll.
    qbit_connected: bool = False
    qbit_last_poll_at: float = 0.0
    qbit_poll_count: int = 0
    qbit_last_error: str = ""
    qbit_host: str = ""

    def snapshot_groups(self) -> list[Group]:
        return list(self.groups.values())

    def torrents_in(self, key: GroupKey) -> list[Torrent]:
        group = self.groups.get(key)
        if not group:
            return []
        return [self.torrents[h] for h in group.torrent_hashes if h in self.torrents]
