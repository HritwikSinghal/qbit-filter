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

    @property
    def configured(self) -> bool:
        return bool(self.radarr_url or self.sonarr_url)
