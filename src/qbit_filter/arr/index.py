"""Pure indexing helpers: ArrSnapshot + qBit torrents -> hash -> ArrMatch.

No I/O. Called by the *arr poller after every snapshot and after every qBit
``store.rid`` bump so new torrents pick up arr metadata on the next tick.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Literal

from qbit_filter.arr.models import (
    ArrMatch,
    ArrMovie,
    ArrSeries,
    ArrSnapshot,
    HistoryMeta,
    QueueRecord,
)
from qbit_filter.domain import Torrent
from qbit_filter.grouping.parser import normalise_title, parse

logger = logging.getLogger(__name__)

_RADARR_TAG_PREFIX = "radarr:"
_SONARR_TAG_PREFIX = "sonarr:"

Via = Literal["tag", "queue", "history", "title"]


def _movies_by_id(snapshot: ArrSnapshot) -> dict[int, ArrMovie]:
    return {m.id: m for m in snapshot.movies}


def _series_by_id(snapshot: ArrSnapshot) -> dict[int, ArrSeries]:
    return {s.id: s for s in snapshot.series}


def _tmdb_index(snapshot: ArrSnapshot) -> dict[int, ArrMovie]:
    return {m.tmdb_id: m for m in snapshot.movies if m.tmdb_id}


def _tvdb_index(snapshot: ArrSnapshot) -> dict[int, ArrSeries]:
    return {s.tvdb_id: s for s in snapshot.series if s.tvdb_id}


def _resolve_tag_labels(
    tag_ids: frozenset[int], labels: dict[int, str]
) -> frozenset[str]:
    """Look up arr tag ids in the snapshot's label map. Drops ids the map
    doesn't know about so a poll that fetched the entity before the tag
    list doesn't strand stale ids in the match."""
    if not tag_ids:
        return frozenset()
    out: set[str] = set()
    for tid in tag_ids:
        label = labels.get(tid)
        if label:
            out.add(label)
    return frozenset(out)


def _movie_match(
    m: ArrMovie,
    via: Via,
    *,
    queue: QueueRecord | None = None,
    history: HistoryMeta | None = None,
    tag_labels: dict[int, str],
) -> ArrMatch:
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
        via=via,
        queue_status_messages=queue.status_messages if queue else (),
        queue_tracked_status=queue.tracked_download_status if queue else "",
        grab_count=history.grab_count if history else 0,
        release_group=history.release_group if history else "",
        indexer=history.indexer if history else "",
        arr_tags=_resolve_tag_labels(m.tags, tag_labels),
    )


def _series_match(
    s: ArrSeries,
    via: Via,
    *,
    queue: QueueRecord | None = None,
    history: HistoryMeta | None = None,
    tag_labels: dict[int, str],
) -> ArrMatch:
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
        via=via,
        queue_status_messages=queue.status_messages if queue else (),
        queue_tracked_status=queue.tracked_download_status if queue else "",
        grab_count=history.grab_count if history else 0,
        release_group=history.release_group if history else "",
        indexer=history.indexer if history else "",
        arr_tags=_resolve_tag_labels(s.tags, tag_labels),
    )


def _orphan_match(source: Literal["radarr", "sonarr"], entity_id: int) -> ArrMatch:
    """Sentinel match for a qBit tag that points at a deleted arr entity.

    The user removed the underlying movie/series from arr but the qBit
    torrent still carries ``radarr:N``/``sonarr:N``. The dangling reference
    is itself the signal the rule layer cares about -- everything else
    (title, monitored, cutoff) defaults to empty/safe values so the orphan
    can't accidentally match other arr rules.
    """
    return ArrMatch(
        source=source,
        entity_id=entity_id,
        tmdb_id=None,
        tvdb_id=None,
        imdb_id="",
        title="",
        year=None,
        monitored=False,
        # Default to True so other rules that gate on "cutoff met" don't
        # falsely flag orphans -- they're handled by the dedicated rule.
        quality_cutoff_met=True,
        poster_url="",
        title_slug="",
        via="tag",
        orphaned=True,
    )


def _match_by_tag(
    t: Torrent,
    movies_by_id: dict[int, ArrMovie],
    series_by_id: dict[int, ArrSeries],
    *,
    radarr_queue: dict[str, QueueRecord],
    sonarr_queue: dict[str, QueueRecord],
    radarr_history: dict[str, HistoryMeta],
    sonarr_history: dict[str, HistoryMeta],
    radarr_tag_labels: dict[int, str],
    sonarr_tag_labels: dict[int, str],
) -> ArrMatch | None:
    h = t.hash.lower()
    for tag in t.tags:
        low = tag.lower().strip()
        if low.startswith(_RADARR_TAG_PREFIX):
            try:
                mid = int(low[len(_RADARR_TAG_PREFIX):])
            except ValueError:
                continue
            m = movies_by_id.get(mid)
            if m is not None:
                return _movie_match(
                    m,
                    "tag",
                    queue=radarr_queue.get(h),
                    history=radarr_history.get(h),
                    tag_labels=radarr_tag_labels,
                )
            # Tag points at a movie id arr doesn't know about -- the user
            # removed it from Radarr while the qBit torrent stuck around.
            return _orphan_match("radarr", mid)
        elif low.startswith(_SONARR_TAG_PREFIX):
            try:
                sid = int(low[len(_SONARR_TAG_PREFIX):])
            except ValueError:
                continue
            s = series_by_id.get(sid)
            if s is not None:
                return _series_match(
                    s,
                    "tag",
                    queue=sonarr_queue.get(h),
                    history=sonarr_history.get(h),
                    tag_labels=sonarr_tag_labels,
                )
            return _orphan_match("sonarr", sid)
    return None


def _match_by_title(
    t: Torrent,
    movies_by_norm: dict[tuple[str, int | None], ArrMovie],
    movies_by_norm_no_year: dict[str, ArrMovie],
    series_by_norm: dict[str, ArrSeries],
    *,
    radarr_queue: dict[str, QueueRecord],
    sonarr_queue: dict[str, QueueRecord],
    radarr_history: dict[str, HistoryMeta],
    sonarr_history: dict[str, HistoryMeta],
    radarr_tag_labels: dict[int, str],
    sonarr_tag_labels: dict[int, str],
) -> ArrMatch | None:
    """Fuzzy title fallback. Routes the raw torrent name through guessit
    (cached in ``grouping.parser``) to recover the clean show/movie title
    and year, then normalises that with :func:`normalise_title` so the
    lookup key matches arr's stored titles. Necessary because TV releases
    rarely carry a year token, so a prefix-cut on the raw name leaves the
    release noise (``S01.1080p.WEB-DL.x265-GRP``) glued to the title and
    nothing in ``series_by_norm`` ever matches.
    """
    parsed = parse(t.name)
    title_norm = normalise_title(parsed.title)
    if not title_norm:
        return None
    h = t.hash.lower()
    year = parsed.year
    if year is not None:
        m = movies_by_norm.get((title_norm, year))
        if m is not None:
            return _movie_match(
                m,
                "title",
                queue=radarr_queue.get(h),
                history=radarr_history.get(h),
                tag_labels=radarr_tag_labels,
            )
    m = movies_by_norm_no_year.get(title_norm)
    if m is not None:
        return _movie_match(
            m,
            "title",
            queue=radarr_queue.get(h),
            history=radarr_history.get(h),
            tag_labels=radarr_tag_labels,
        )
    s = series_by_norm.get(title_norm)
    if s is not None:
        return _series_match(
            s,
            "title",
            queue=sonarr_queue.get(h),
            history=sonarr_history.get(h),
            tag_labels=sonarr_tag_labels,
        )
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

    radarr_queue = snapshot.radarr_queue
    sonarr_queue = snapshot.sonarr_queue
    radarr_history = snapshot.radarr_history
    sonarr_history = snapshot.sonarr_history
    radarr_tags = snapshot.radarr_tag_labels
    sonarr_tags = snapshot.sonarr_tag_labels

    out: dict[str, ArrMatch] = {}
    for t in torrents:
        h = t.hash.lower()

        # 1. Tag-based -- explicit user intent, highest confidence.
        match = _match_by_tag(
            t,
            movies_by_id,
            series_by_id,
            radarr_queue=radarr_queue,
            sonarr_queue=sonarr_queue,
            radarr_history=radarr_history,
            sonarr_history=sonarr_history,
            radarr_tag_labels=radarr_tags,
            sonarr_tag_labels=sonarr_tags,
        )
        if match is not None:
            out[h] = match
            continue

        # 2. Radarr queue (currently-downloading torrents).
        r_queue_rec = radarr_queue.get(h)
        if r_queue_rec is not None:
            movie = movies_by_id.get(r_queue_rec.entity_id)
            if movie is not None:
                out[h] = _movie_match(
                    movie,
                    "queue",
                    queue=r_queue_rec,
                    history=radarr_history.get(h),
                    tag_labels=radarr_tags,
                )
                continue

        # 3. Sonarr queue.
        s_queue_rec = sonarr_queue.get(h)
        if s_queue_rec is not None:
            series = series_by_id.get(s_queue_rec.entity_id)
            if series is not None:
                out[h] = _series_match(
                    series,
                    "queue",
                    queue=s_queue_rec,
                    history=sonarr_history.get(h),
                    tag_labels=sonarr_tags,
                )
                continue

        # 4. Radarr history.
        r_hist = radarr_history.get(h)
        if r_hist is not None:
            movie = movies_by_id.get(r_hist.entity_id)
            if movie is not None:
                out[h] = _movie_match(
                    movie,
                    "history",
                    queue=radarr_queue.get(h),
                    history=r_hist,
                    tag_labels=radarr_tags,
                )
                continue

        # 5. Sonarr history.
        s_hist = sonarr_history.get(h)
        if s_hist is not None:
            series = series_by_id.get(s_hist.entity_id)
            if series is not None:
                out[h] = _series_match(
                    series,
                    "history",
                    queue=sonarr_queue.get(h),
                    history=s_hist,
                    tag_labels=sonarr_tags,
                )
                continue

        # 6. Title fallback (optional).
        if title_fallback:
            match = _match_by_title(
                t,
                movies_by_norm,
                movies_by_norm_no_year,
                series_by_norm,
                radarr_queue=radarr_queue,
                sonarr_queue=sonarr_queue,
                radarr_history=radarr_history,
                sonarr_history=sonarr_history,
                radarr_tag_labels=radarr_tags,
                sonarr_tag_labels=sonarr_tags,
            )
            if match is not None:
                out[h] = match
    return out
