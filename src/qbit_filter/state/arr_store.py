"""Canonical in-memory state for *arr enrichment. Mutated only by the *arr
polling task in ``app.py``; everyone else reads.

Kept separate from :class:`qbit_filter.state.store.Store` because:
- The qBit reconciler rebuilds the snapshot whole on every poll tick (1s);
  the *arr poller runs every 60s. Different cadences want different stores.
- Bolting *arr fields onto the qBit ``Torrent`` snapshot would let the *arr
  poller race the qBit reconciler. Separate store removes the race.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qbit_filter.arr.models import ArrMatch, ArrMovie, ArrSeries, QualityProfile


@dataclass(slots=True)
class ArrStore:
    movies_by_id: dict[int, ArrMovie] = field(default_factory=dict)
    series_by_id: dict[int, ArrSeries] = field(default_factory=dict)
    tmdb_to_movie: dict[int, ArrMovie] = field(default_factory=dict)
    tvdb_to_series: dict[int, ArrSeries] = field(default_factory=dict)
    # qBit infohash (lowercase) -> ArrMatch. Empty entry means "no arr knows
    # about this torrent" (orphan); absence-from-map can be filled in by the
    # next index rebuild.
    hash_to_arr: dict[str, ArrMatch] = field(default_factory=dict)
    quality_profiles: dict[int, QualityProfile] = field(default_factory=dict)
    # Connection state for UI hints: True iff at least one *arr fetch
    # succeeded in the last poll cycle.
    radarr_ok: bool = False
    sonarr_ok: bool = False
    radarr_url: str = ""
    sonarr_url: str = ""
    # Bumped on every snapshot rebuild. Independent from qBit ``Store.rid``.
    rid: int = 0
    # Per-service sync telemetry surfaced by the background activity dialog.
    # Mutated by the arr poller after each fetch cycle. Timestamps are wall
    # clock (``time.time()``) so the browser can render "X ago" against its
    # own clock. ``*_last_err`` is cleared on the next successful fetch.
    # Counts are denormalised from the snapshot so the dialog doesn't have to
    # walk every dict to summarise.
    radarr_last_fetch_at: float = 0.0
    sonarr_last_fetch_at: float = 0.0
    radarr_last_err: str = ""
    sonarr_last_err: str = ""
    radarr_queue_count: int = 0
    sonarr_queue_count: int = 0
    radarr_history_count: int = 0
    sonarr_history_count: int = 0
    arr_fetch_cycles: int = 0
    # Cumulative match counts from the most recent ``build_index`` -- the
    # dialog shows this as the "Linked" number for each service.
    radarr_match_count: int = 0
    sonarr_match_count: int = 0

    @property
    def configured(self) -> bool:
        return bool(self.radarr_url or self.sonarr_url)
