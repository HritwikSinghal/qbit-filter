"""Trimmed dataclasses for *arr resources. Only fields we actually consume."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class QualityProfile:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ArrMovie:
    id: int
    tmdb_id: int | None
    imdb_id: str  # "tt0468569" or ""
    title: str
    year: int | None
    monitored: bool
    has_file: bool
    quality_cutoff_not_met: bool
    quality_profile_id: int
    size_on_disk: int
    poster_url: str  # absolute URL with API key baked in, or ""
    status: str  # "released" / "announced" / "inCinemas" / ...
    title_slug: str


@dataclass(frozen=True, slots=True)
class ArrSeries:
    id: int
    tvdb_id: int | None
    imdb_id: str
    tmdb_id: int | None
    title: str
    year: int | None
    monitored: bool
    status: str  # "continuing" / "ended" / ...
    quality_profile_id: int
    poster_url: str
    title_slug: str
    season_monitored: dict[int, bool] = field(default_factory=dict)
    episode_file_count: int = 0
    total_episode_count: int = 0
    percent_of_episodes: float = 0.0


@dataclass(frozen=True, slots=True)
class ArrMatch:
    """Link from a qBit infohash to the *arr entity that owns it.

    ``quality_cutoff_met`` mirrors the inverse of arr's ``qualityCutoffNotMet``
    so the rule layer can read it directly without flipping the boolean. For
    Sonarr (series-level), this is True iff the *series* cutoff is met across
    its monitored episodes (best-effort: arr exposes this per episode, not
    per series, so v1 uses the series-level signal from the queue/history
    record where available, else True when the series has every episode and
    monitoring is off).
    """

    source: Literal["radarr", "sonarr"]
    entity_id: int  # arr's internal id (NOT tmdb/tvdb)
    tmdb_id: int | None
    tvdb_id: int | None
    imdb_id: str
    title: str
    year: int | None
    monitored: bool
    quality_cutoff_met: bool
    poster_url: str
    title_slug: str
    # How the match was established: "tag" / "queue" / "history" / "title".
    # Surfaced for debugging and so a future strict mode can exclude the
    # weaker matches.
    via: Literal["tag", "queue", "history", "title"]


@dataclass(slots=True)
class ArrSnapshot:
    """One tick's worth of pulled state from both *arr instances.

    ``radarr_queue`` / ``radarr_history`` map qBit hash (lowercased) -> arr
    movie id, so the indexer can resolve hash -> ArrMovie via two lookups.
    Sonarr equivalents use series id.
    """

    movies: list[ArrMovie] = field(default_factory=list)
    series: list[ArrSeries] = field(default_factory=list)
    radarr_queue: dict[str, int] = field(default_factory=dict)
    sonarr_queue: dict[str, int] = field(default_factory=dict)
    radarr_history: dict[str, int] = field(default_factory=dict)
    sonarr_history: dict[str, int] = field(default_factory=dict)
    quality_profiles_radarr: dict[int, QualityProfile] = field(default_factory=dict)
    quality_profiles_sonarr: dict[int, QualityProfile] = field(default_factory=dict)
    # Empty when both arr URLs are unset.
    ok: bool = False
