"""Pure indexing helpers: ArrSnapshot + qBit torrents -> hash -> ArrMatch.

No I/O. Called by the *arr poller after every snapshot and after every qBit
``store.rid`` bump so new torrents pick up arr metadata on the next tick.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from qbit_filter.arr.models import ArrMatch, ArrMovie, ArrSeries, ArrSnapshot
from qbit_filter.domain import Torrent
from qbit_filter.grouping.parser import normalise_title

logger = logging.getLogger(__name__)

_RADARR_TAG_PREFIX = "radarr:"
_SONARR_TAG_PREFIX = "sonarr:"


def _movies_by_id(snapshot: ArrSnapshot) -> dict[int, ArrMovie]:
    return {m.id: m for m in snapshot.movies}


def _series_by_id(snapshot: ArrSnapshot) -> dict[int, ArrSeries]:
    return {s.id: s for s in snapshot.series}


def _tmdb_index(snapshot: ArrSnapshot) -> dict[int, ArrMovie]:
    return {m.tmdb_id: m for m in snapshot.movies if m.tmdb_id}


def _tvdb_index(snapshot: ArrSnapshot) -> dict[int, ArrSeries]:
    return {s.tvdb_id: s for s in snapshot.series if s.tvdb_id}


def _movie_match(m: ArrMovie, via: str) -> ArrMatch:
    return ArrMatch(
        source="radarr",
        entity_id=m.id,
        tmdb_id=m.tmdb_id,
        tvdb_id=None,
        imdb_id=m.imdb_id,
        title=m.title,
        year=m.year,
        monitored=m.monitored,
        # Radarr exposes the inverse: qualityCutoffNotMet=True means "still
        # searching for an upgrade". Flip to a positive predicate.
        quality_cutoff_met=not m.quality_cutoff_not_met,
        poster_url=m.poster_url,
        title_slug=m.title_slug,
        via=via,  # type: ignore[arg-type]
    )


def _series_match(s: ArrSeries, via: str) -> ArrMatch:
    # Sonarr doesn't expose a per-series cutoff-met flag; the closest proxy is
    # "every monitored episode has a file AND series is no longer monitored
    # for new items". The richer per-episode signal lands when the indexer
    # learns to walk /episode -- v1 uses the heuristic.
    cutoff_met = (
        s.episode_file_count >= s.total_episode_count and s.total_episode_count > 0
    )
    return ArrMatch(
        source="sonarr",
        entity_id=s.id,
        tmdb_id=s.tmdb_id,
        tvdb_id=s.tvdb_id,
        imdb_id=s.imdb_id,
        title=s.title,
        year=s.year,
        monitored=s.monitored,
        quality_cutoff_met=cutoff_met,
        poster_url=s.poster_url,
        title_slug=s.title_slug,
        via=via,  # type: ignore[arg-type]
    )


def _match_by_tag(
    t: Torrent,
    movies_by_id: dict[int, ArrMovie],
    series_by_id: dict[int, ArrSeries],
) -> ArrMatch | None:
    for tag in t.tags:
        low = tag.lower().strip()
        if low.startswith(_RADARR_TAG_PREFIX):
            try:
                mid = int(low[len(_RADARR_TAG_PREFIX):])
            except ValueError:
                continue
            m = movies_by_id.get(mid)
            if m is not None:
                return _movie_match(m, "tag")
        elif low.startswith(_SONARR_TAG_PREFIX):
            try:
                sid = int(low[len(_SONARR_TAG_PREFIX):])
            except ValueError:
                continue
            s = series_by_id.get(sid)
            if s is not None:
                return _series_match(s, "tag")
    return None


def _match_by_title(
    t: Torrent,
    movies_by_norm: dict[tuple[str, int | None], ArrMovie],
    movies_by_norm_no_year: dict[str, ArrMovie],
    series_by_norm: dict[str, ArrSeries],
) -> ArrMatch | None:
    """Fuzzy title fallback. Uses :func:`normalise_title` so qBit and arr
    titles collapse the same way (drops punctuation, case-folds, removes
    common release noise). Year-aware for movies; series match on title only.
    """
    # guessit is expensive; cheap normalisation against the raw torrent name is
    # already done elsewhere via grouping/parser.py. We re-use the cheap form
    # here: normalise the torrent name, then prefix-trim numeric tails.
    name_norm = normalise_title(t.name)
    if not name_norm:
        return None
    # Try movies with year first (most precise). Series indexed by title only.
    # Some series share titles across years (rare); v1 ignores that.
    # Year extraction: grab the first 4-digit token between 1900-2099.
    year: int | None = None
    for token in name_norm.split():
        if token.isdigit() and len(token) == 4:
            candidate = int(token)
            if 1900 <= candidate <= 2099:
                year = candidate
                break
    # The title we look up is the prefix before the year token, when present.
    title_only = name_norm
    if year is not None:
        cut = name_norm.find(str(year))
        if cut > 0:
            title_only = name_norm[:cut].strip()
    if year is not None:
        m = movies_by_norm.get((title_only, year))
        if m is not None:
            return _movie_match(m, "title")
    m = movies_by_norm_no_year.get(title_only)
    if m is not None:
        return _movie_match(m, "title")
    s = series_by_norm.get(title_only)
    if s is not None:
        return _series_match(s, "title")
    return None


def build_index(
    snapshot: ArrSnapshot,
    torrents: Iterable[Torrent],
    *,
    title_fallback: bool = True,
) -> dict[str, ArrMatch]:
    """Resolve every qBit infohash to its *arr entity (when known).

    Precedence: tag -> Radarr queue -> Sonarr queue -> Radarr history ->
    Sonarr history -> title fallback (Radarr movies, then Sonarr series).
    """
    movies_by_id = _movies_by_id(snapshot)
    series_by_id = _series_by_id(snapshot)

    movies_by_norm: dict[tuple[str, int | None], ArrMovie] = {}
    movies_by_norm_no_year: dict[str, ArrMovie] = {}
    for m in snapshot.movies:
        n = normalise_title(m.title)
        if not n:
            continue
        movies_by_norm[(n, m.year)] = m
        # Year-less fallback: only insert if we haven't seen the title yet
        # (prefers the first one). Realistically this is rare.
        movies_by_norm_no_year.setdefault(n, m)

    series_by_norm: dict[str, ArrSeries] = {}
    for s in snapshot.series:
        n = normalise_title(s.title)
        if n:
            series_by_norm.setdefault(n, s)

    out: dict[str, ArrMatch] = {}
    for t in torrents:
        h = t.hash.lower()

        # 1. Tag-based -- explicit user intent, highest confidence.
        match = _match_by_tag(t, movies_by_id, series_by_id)
        if match is not None:
            out[h] = match
            continue

        # 2. Radarr queue (currently-downloading torrents).
        movie_id = snapshot.radarr_queue.get(h)
        if movie_id is not None:
            movie = movies_by_id.get(movie_id)
            if movie is not None:
                out[h] = _movie_match(movie, "queue")
                continue

        # 3. Sonarr queue.
        series_id = snapshot.sonarr_queue.get(h)
        if series_id is not None:
            series = series_by_id.get(series_id)
            if series is not None:
                out[h] = _series_match(series, "queue")
                continue

        # 4. Radarr history.
        movie_id = snapshot.radarr_history.get(h)
        if movie_id is not None:
            movie = movies_by_id.get(movie_id)
            if movie is not None:
                out[h] = _movie_match(movie, "history")
                continue

        # 5. Sonarr history.
        series_id = snapshot.sonarr_history.get(h)
        if series_id is not None:
            series = series_by_id.get(series_id)
            if series is not None:
                out[h] = _series_match(series, "history")
                continue

        # 6. Title fallback (optional).
        if title_fallback:
            match = _match_by_title(
                t, movies_by_norm, movies_by_norm_no_year, series_by_norm
            )
            if match is not None:
                out[h] = match
    return out
