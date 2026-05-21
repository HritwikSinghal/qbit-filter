"""Trimmed dataclasses for *arr resources. Only fields we actually consume."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class QualityProfile:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class QueueRecord:
    """One queue entry as exposed by ``/api/v3/queue``.

    Drops everything except the diagnostic surface qbit-filter needs:
    ``entity_id`` resolves the hash to the owning movie/series, and the
    two status fields drive the ``ArrImportBrokenRule`` plus the inline
    "arr says: ..." pill on torrent rows.
    """

    entity_id: int  # movieId for Radarr, seriesId for Sonarr
    tracked_download_status: str  # "ok" | "warning" | "error"
    status_messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HistoryMeta:
    """Per-hash aggregate from ``/api/v3/history`` grab events.

    ``entity_id`` is the latest grab's owning movie/series id (replaces the
    bare-int mapping the old fetcher returned). ``grab_count`` is the total
    number of ``grabbed`` events arr has for this hash -- 1 is the common
    case; higher counts mark torrents arr has had to retry repeatedly and
    drives the click-through history dialog. ``release_group`` and
    ``indexer`` come from the most-recent grab so the UI shows what's
    actually on disk now.
    """

    entity_id: int
    grab_count: int
    release_group: str
    indexer: str


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
    # Arr's own tag IDs (separate namespace from qBit's tag strings). The
    # snapshot's ``radarr_tag_labels`` resolves these to labels, which the
    # indexer then attaches to each ``ArrMatch.arr_tags`` for filter use.
    tags: frozenset[int] = frozenset()


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
    tags: frozenset[int] = frozenset()


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

    ``orphaned`` flags the "user removed this title in arr but the qBit
    torrent still carries a ``radarr:N``/``sonarr:N`` tag" case. When True,
    ``title``/``year`` are empty placeholders and the only useful fields
    are ``source`` + ``entity_id`` -- it exists purely so the indexer can
    surface dangling references to the rule layer.

    ``queue_status_messages`` / ``queue_tracked_status`` carry arr's own
    diagnostic output when this hash is currently in the queue with an
    import problem. Empty when arr's queue is happy or doesn't have this
    hash. ``grab_count`` / ``release_group`` / ``indexer`` come from arr's
    history (most-recent grab event). ``arr_tags`` are the user-defined
    tag *labels* (resolved from arr's tag id namespace) applied to the
    underlying movie/series -- the user's own retention labels are kept
    here for filter / exclusion use.
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
    # arr-derived context. All optional; default to empty/zero so existing
    # call-sites that don't supply these still get valid matches.
    queue_status_messages: tuple[str, ...] = ()
    queue_tracked_status: str = ""
    grab_count: int = 0
    release_group: str = ""
    indexer: str = ""
    arr_tags: frozenset[str] = frozenset()
    orphaned: bool = False


@dataclass(slots=True)
class ArrSnapshot:
    """One tick's worth of pulled state from both *arr instances.

    ``radarr_queue`` / ``sonarr_queue`` map qBit hash (lowercased) ->
    :class:`QueueRecord` -- the indexer reads ``record.entity_id`` to
    resolve the owning movie/series and the diagnostic fields to flag
    broken imports. ``radarr_history`` / ``sonarr_history`` mirror the
    same shape with :class:`HistoryMeta` aggregating grab counts +
    most-recent release group + indexer per hash.

    ``radarr_tag_labels`` / ``sonarr_tag_labels`` are arr's own
    ``tag id -> label`` maps fetched from ``/api/v3/tag``. The indexer
    uses these to resolve each entity's ``tags`` (which arr exposes as
    integer ids) to user-facing labels on the ``ArrMatch``.
    """

    movies: list[ArrMovie] = field(default_factory=list)
    series: list[ArrSeries] = field(default_factory=list)
    radarr_queue: dict[str, QueueRecord] = field(default_factory=dict)
    sonarr_queue: dict[str, QueueRecord] = field(default_factory=dict)
    radarr_history: dict[str, HistoryMeta] = field(default_factory=dict)
    sonarr_history: dict[str, HistoryMeta] = field(default_factory=dict)
    quality_profiles_radarr: dict[int, QualityProfile] = field(default_factory=dict)
    quality_profiles_sonarr: dict[int, QualityProfile] = field(default_factory=dict)
    radarr_tag_labels: dict[int, str] = field(default_factory=dict)
    sonarr_tag_labels: dict[int, str] = field(default_factory=dict)
    # Empty when both arr URLs are unset.
    ok: bool = False
