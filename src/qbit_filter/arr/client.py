"""HTTP wrappers around Radarr/Sonarr's `/api/v3` endpoints.

Only this module imports httpx. Callers receive parsed lists/dicts; failures
raise :class:`ArrUnavailable` so the polling layer can swallow + log + retry
without taking down the rest of the app.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from qbit_filter.arr.models import (
    ArrMovie,
    ArrSeries,
    HistoryMeta,
    QualityProfile,
    QueueRecord,
)

logger = logging.getLogger(__name__)

# Reasonable per-request ceiling: Radarr/Sonarr can return multi-thousand-item
# movie/series lists on large libraries. 30s gives slow self-hosted instances
# room without holding the poll loop forever.
_DEFAULT_TIMEOUT = 30.0


class ArrUnavailable(Exception):
    """Connect / HTTP / decode failure talking to an *arr instance."""


def make_client() -> httpx.AsyncClient:
    """Shared async client. One per poll loop; reused across radarr + sonarr."""
    return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, follow_redirects=True)


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Accept": "application/json"}


def _base(url: str) -> str:
    """Strip trailing slash so endpoint paths join cleanly."""
    return url.rstrip("/")


async def _get_json(
    client: httpx.AsyncClient, url: str, api_key: str
) -> Any:
    try:
        resp = await client.get(url, headers=_headers(api_key))
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ArrUnavailable(f"GET {url} failed: {exc}") from exc


def _pick_poster_url(images: list[dict[str, Any]] | None, base_url: str, api_key: str) -> str:
    """Pick the poster image from arr's ``images`` array.

    arr returns relative API paths (``/api/v3/MediaCover/{id}/poster.jpg``);
    we rewrite to an absolute URL with the API key as a query parameter so
    the browser can hot-link it without a custom header.
    """
    if not images:
        return ""
    for img in images:
        if (img.get("coverType") or "").lower() != "poster":
            continue
        path = img.get("url") or img.get("remoteUrl") or ""
        if not path:
            continue
        if path.startswith("http://") or path.startswith("https://"):
            return str(path)
        # arr returns paths like "/MediaCover/123/poster.jpg" (no api/v3 prefix).
        # The full path lives at "{base}/api/v3/MediaCover/123/poster.jpg".
        # Some versions strip the leading slash; both shapes are supported.
        rel = path if path.startswith("/") else "/" + path
        if "/MediaCover/" in rel and not rel.startswith("/api/"):
            rel = "/api/v3" + rel[rel.index("/MediaCover/"):]
        # Strip query (arr appends a cache-buster); we re-attach the api key.
        if "?" in rel:
            rel = rel.split("?", 1)[0]
        return f"{base_url}{rel}?apikey={api_key}"
    return ""


def _coerce_tag_ids(raw: Any) -> frozenset[int]:
    """Coerce arr's ``tags`` JSON field (list[int]) into a frozenset[int].

    Returns the empty set when the field is missing, malformed, or contains
    non-numeric entries -- arr always emits ints, but defensiveness keeps a
    misbehaving instance from crashing the poller.
    """
    if not isinstance(raw, list):
        return frozenset()
    ids: set[int] = set()
    for x in raw:
        try:
            ids.add(int(x))
        except (TypeError, ValueError):
            continue
    return frozenset(ids)


async def fetch_movies(
    client: httpx.AsyncClient, url: str, api_key: str
) -> list[ArrMovie]:
    base = _base(url)
    raw = await _get_json(client, f"{base}/api/v3/movie", api_key)
    if not isinstance(raw, list):
        raise ArrUnavailable(f"radarr /movie returned {type(raw).__name__}")
    out: list[ArrMovie] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        movie_file = m.get("movieFile") if isinstance(m.get("movieFile"), dict) else {}
        out.append(
            ArrMovie(
                id=int(m.get("id") or 0),
                tmdb_id=int(m["tmdbId"]) if m.get("tmdbId") else None,
                imdb_id=str(m.get("imdbId") or ""),
                title=str(m.get("title") or ""),
                year=int(m["year"]) if m.get("year") else None,
                monitored=bool(m.get("monitored")),
                has_file=bool(m.get("hasFile")),
                quality_cutoff_not_met=bool(m.get("qualityCutoffNotMet")),
                quality_profile_id=int(m.get("qualityProfileId") or 0),
                size_on_disk=int(
                    m.get("sizeOnDisk")
                    or (movie_file.get("size") if movie_file else 0)
                    or 0
                ),
                poster_url=_pick_poster_url(m.get("images"), base, api_key),
                status=str(m.get("status") or ""),
                title_slug=str(m.get("titleSlug") or ""),
                tags=_coerce_tag_ids(m.get("tags")),
            )
        )
    return out


async def fetch_series(
    client: httpx.AsyncClient, url: str, api_key: str
) -> list[ArrSeries]:
    base = _base(url)
    raw = await _get_json(client, f"{base}/api/v3/series", api_key)
    if not isinstance(raw, list):
        raise ArrUnavailable(f"sonarr /series returned {type(raw).__name__}")
    out: list[ArrSeries] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        raw_seasons = s.get("seasons")
        seasons: list[Any] = list(raw_seasons) if isinstance(raw_seasons, list) else []
        season_monitored: dict[int, bool] = {}
        for sn in seasons:
            if isinstance(sn, dict) and "seasonNumber" in sn:
                season_monitored[int(sn["seasonNumber"])] = bool(sn.get("monitored"))
        raw_stats = s.get("statistics")
        stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
        ep_file_count = int(stats.get("episodeFileCount") or 0)
        total_eps = int(stats.get("totalEpisodeCount") or 0)
        percent = float(stats.get("percentOfEpisodes") or 0.0)
        out.append(
            ArrSeries(
                id=int(s.get("id") or 0),
                tvdb_id=int(s["tvdbId"]) if s.get("tvdbId") else None,
                imdb_id=str(s.get("imdbId") or ""),
                tmdb_id=int(s["tmdbId"]) if s.get("tmdbId") else None,
                title=str(s.get("title") or ""),
                year=int(s["year"]) if s.get("year") else None,
                monitored=bool(s.get("monitored")),
                status=str(s.get("status") or ""),
                quality_profile_id=int(s.get("qualityProfileId") or 0),
                poster_url=_pick_poster_url(s.get("images"), base, api_key),
                title_slug=str(s.get("titleSlug") or ""),
                season_monitored=season_monitored,
                episode_file_count=ep_file_count,
                total_episode_count=total_eps,
                percent_of_episodes=percent,
                tags=_coerce_tag_ids(s.get("tags")),
            )
        )
    return out


def _coerce_status_messages(raw: Any) -> tuple[str, ...]:
    """Flatten arr's ``statusMessages`` array of ``{title, messages}`` dicts
    into a tuple of distinct human-readable strings.

    Each item is shaped ``{"title": str, "messages": list[str]}``. The
    ``title`` is often a release name (useful context); the ``messages``
    array carries the actual diagnostic. Returns an empty tuple when arr
    has nothing to say. Preserves order so the first message stays first
    (UI renders only the leading one).
    """
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        msgs = entry.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                s = str(m).strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        # Some arr versions emit a flat ``title`` with no ``messages`` list
        # (typically when the entry IS the diagnostic). Fall through to it.
        title = entry.get("title")
        if isinstance(title, str):
            s = title.strip()
            if s and s not in seen and not msgs:
                seen.add(s)
                out.append(s)
    return tuple(out)


async def _fetch_queue(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    entity_field: str,
) -> dict[str, QueueRecord]:
    """Return a downloadId(lowercased) -> :class:`QueueRecord` map.

    ``entity_field`` is ``"movieId"`` for Radarr and ``"seriesId"`` for Sonarr.
    Radarr's queue uses ``includeUnknownMovieItems`` to surface items that
    haven't yet matched a movie record; we still skip those (entity_id == 0)
    because we need a usable id to look up the rest of the metadata.

    Captures ``trackedDownloadStatus`` + flattened ``statusMessages`` so the
    indexer can decorate the resulting ``ArrMatch`` with arr's own
    diagnostic output -- those drive the ``ArrImportBrokenRule`` and the
    inline error pill on torrent rows.
    """
    base = _base(url)
    out: dict[str, QueueRecord] = {}
    # Sonarr paginates; ask for one big page and rely on totalRecords. Radarr
    # behaves the same. 1000 covers the vast majority of real libraries.
    raw = await _get_json(
        client,
        f"{base}/api/v3/queue?page=1&pageSize=1000",
        api_key,
    )
    records: list[Any]
    if isinstance(raw, dict) and isinstance(raw.get("records"), list):
        records = list(raw["records"])
    elif isinstance(raw, list):
        records = raw
    else:
        return out
    for item in records:
        if not isinstance(item, dict):
            continue
        download_id = str(item.get("downloadId") or "").strip().lower()
        entity_id = int(item.get(entity_field) or 0)
        if not (download_id and entity_id):
            continue
        out[download_id] = QueueRecord(
            entity_id=entity_id,
            tracked_download_status=str(item.get("trackedDownloadStatus") or ""),
            status_messages=_coerce_status_messages(item.get("statusMessages")),
        )
    return out


async def fetch_radarr_queue(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, QueueRecord]:
    return await _fetch_queue(client, url, api_key, entity_field="movieId")


async def fetch_sonarr_queue(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, QueueRecord]:
    return await _fetch_queue(client, url, api_key, entity_field="seriesId")


async def _fetch_history(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    entity_field: str,
    pages: int = 4,
    page_size: int = 250,
) -> dict[str, HistoryMeta]:
    """Walk recent grabbed history and return downloadId -> HistoryMeta.

    Sonarr / Radarr history paginates; we pull the most recent ``pages`` pages
    of ``eventType=grabbed`` records. That covers the typical "everything
    grabbed in the last few months" window without hammering the API.

    Accumulates *all* grab events per hash to count retries (``grab_count``
    in the returned record). The most-recent grab (first occurrence in a
    descending-by-date scan) wins for ``entity_id`` / ``release_group`` /
    ``indexer`` -- so the metadata reflects what's actually on disk now.
    """
    base = _base(url)
    counts: dict[str, int] = {}
    first_seen: dict[str, dict[str, Any]] = {}
    for page in range(1, pages + 1):
        params = (
            f"page={page}&pageSize={page_size}"
            f"&sortKey=date&sortDirection=descending&eventType=1"
        )
        try:
            raw = await _get_json(
                client, f"{base}/api/v3/history?{params}", api_key
            )
        except ArrUnavailable:
            break
        records: list[Any]
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            records = list(raw["records"])
        elif isinstance(raw, list):
            records = raw
        else:
            break
        if not records:
            break
        for item in records:
            if not isinstance(item, dict):
                continue
            download_id = str(item.get("downloadId") or "").strip().lower()
            entity_id = int(item.get(entity_field) or 0)
            if not (download_id and entity_id):
                continue
            counts[download_id] = counts.get(download_id, 0) + 1
            if download_id not in first_seen:
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                # arr nests release group + indexer inside ``data`` on grab
                # events. Top-level ``releaseGroup`` / ``indexer`` are
                # fallbacks for older arr versions that flatten them.
                first_seen[download_id] = {
                    "entity_id": entity_id,
                    "release_group": str(
                        (data.get("releaseGroup") if data else "")
                        or item.get("releaseGroup")
                        or ""
                    ),
                    "indexer": str(
                        (data.get("indexer") if data else "")
                        or item.get("indexer")
                        or ""
                    ),
                }
    out: dict[str, HistoryMeta] = {}
    for download_id, info in first_seen.items():
        out[download_id] = HistoryMeta(
            entity_id=int(info["entity_id"]),
            grab_count=counts.get(download_id, 1),
            release_group=str(info["release_group"]),
            indexer=str(info["indexer"]),
        )
    return out


async def fetch_radarr_history(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, HistoryMeta]:
    return await _fetch_history(client, url, api_key, entity_field="movieId")


async def fetch_sonarr_history(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, HistoryMeta]:
    return await _fetch_history(client, url, api_key, entity_field="seriesId")


async def _fetch_current_download_ids(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    entity_field: str,
    pages: int = 4,
    page_size: int = 250,
) -> frozenset[str]:
    """Walk recent ``downloadFolderImported`` (eventType=3) events and
    return the union of the most-recent imported downloadId per entity.

    ``entity_field`` is ``"movieId"`` for Radarr (one file per movie) and
    ``"episodeId"`` for Sonarr (per-episode granularity, since a series
    typically pulls files from many torrents -- one per season is the
    common shape). A descending-by-date scan means the first occurrence
    we see for a given entity is the current import; we record its
    downloadId and ignore any earlier entries for the same entity.

    The returned set is the universe of hashes that arr still considers
    "live" on disk. Any hash *outside* the set is either older media arr
    has since upgraded or an unimported grab -- the cleanup engine reads
    this to flag superseded copies even when the cross-tier rule can't
    (e.g. two 2160p REMUX copies of the same movie, only one still backing
    arr's file).
    """
    base = _base(url)
    first_seen: set[int] = set()
    out: set[str] = set()
    for page in range(1, pages + 1):
        params = (
            f"page={page}&pageSize={page_size}"
            f"&sortKey=date&sortDirection=descending&eventType=3"
        )
        try:
            raw = await _get_json(
                client, f"{base}/api/v3/history?{params}", api_key
            )
        except ArrUnavailable:
            break
        records: list[Any]
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            records = list(raw["records"])
        elif isinstance(raw, list):
            records = raw
        else:
            break
        if not records:
            break
        for item in records:
            if not isinstance(item, dict):
                continue
            entity_id_raw = item.get(entity_field)
            if not isinstance(entity_id_raw, int) or entity_id_raw == 0:
                continue
            if entity_id_raw in first_seen:
                continue
            first_seen.add(entity_id_raw)
            download_id = str(item.get("downloadId") or "").strip().lower()
            if download_id:
                out.add(download_id)
    return frozenset(out)


async def fetch_radarr_current_download_ids(
    client: httpx.AsyncClient, url: str, api_key: str
) -> frozenset[str]:
    return await _fetch_current_download_ids(
        client, url, api_key, entity_field="movieId"
    )


async def fetch_sonarr_current_download_ids(
    client: httpx.AsyncClient, url: str, api_key: str
) -> frozenset[str]:
    return await _fetch_current_download_ids(
        client, url, api_key, entity_field="episodeId"
    )


async def fetch_tags(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[int, str]:
    """Return arr's ``tag id -> label`` map from ``/api/v3/tag``.

    Empty dict when arr is unreachable, has no tags, or the endpoint
    returns an unexpected shape. arr's tag namespace is separate from
    qBit's; the qbit-filter UI exposes labels (not ids) so this resolution
    happens once per poll tick and is cached on the snapshot.
    """
    base = _base(url)
    try:
        raw = await _get_json(client, f"{base}/api/v3/tag", api_key)
    except ArrUnavailable:
        return {}
    out: dict[int, str] = {}
    if not isinstance(raw, list):
        return out
    for t in raw:
        if not isinstance(t, dict):
            continue
        tid_raw = t.get("id")
        label = t.get("label")
        if tid_raw is None or not isinstance(label, str):
            continue
        try:
            tid = int(tid_raw)
        except (TypeError, ValueError):
            continue
        out[tid] = label.strip()
    return out


async def fetch_history_for_entity(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    source: str,
    entity_id: int,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """Pull the recent history for one movie or series for the dialog view.

    ``source`` is ``"radarr"`` or ``"sonarr"``. Calls
    ``/api/v3/history/movie?movieId=<id>`` for Radarr; Sonarr lacks a
    series-scoped history endpoint, so the fallback hits
    ``/api/v3/history?seriesId=<id>``. Each record is normalised down to
    the small set of fields the dialog renders -- the full arr payload is
    large and most of it isn't useful in this context. Newest record
    first (matches arr's default ordering).
    """
    base = _base(url)
    params: str
    if source == "radarr":
        params = f"movieId={entity_id}"
        path = f"{base}/api/v3/history/movie?{params}"
    else:
        params = (
            f"seriesId={entity_id}&page=1&pageSize={page_size}"
            f"&sortKey=date&sortDirection=descending"
        )
        path = f"{base}/api/v3/history?{params}"
    try:
        raw = await _get_json(client, path, api_key)
    except ArrUnavailable:
        return []
    records: list[Any]
    if isinstance(raw, dict) and isinstance(raw.get("records"), list):
        records = list(raw["records"])
    elif isinstance(raw, list):
        records = raw
    else:
        return []
    out: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
        qual_inner = (
            quality.get("quality") if isinstance(quality.get("quality"), dict) else {}
        )
        out.append(
            {
                "date": str(item.get("date") or ""),
                "event_type": str(item.get("eventType") or ""),
                "source_title": str(item.get("sourceTitle") or ""),
                "release_group": str(
                    (data.get("releaseGroup") if data else "")
                    or item.get("releaseGroup")
                    or ""
                ),
                "indexer": str(
                    (data.get("indexer") if data else "")
                    or item.get("indexer")
                    or ""
                ),
                "quality": str(qual_inner.get("name") or ""),
            }
        )
    return out


async def fetch_quality_profiles(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[int, QualityProfile]:
    base = _base(url)
    try:
        raw = await _get_json(client, f"{base}/api/v3/qualityprofile", api_key)
    except ArrUnavailable:
        return {}
    out: dict[int, QualityProfile] = {}
    if not isinstance(raw, list):
        return out
    for p in raw:
        if not isinstance(p, dict):
            continue
        pid = int(p.get("id") or 0)
        name = str(p.get("name") or "")
        if pid and name:
            out[pid] = QualityProfile(id=pid, name=name)
    return out
