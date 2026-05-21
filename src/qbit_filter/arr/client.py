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
    QualityProfile,
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
            rel = "/api/v3" + rel
        # Strip query (arr appends a cache-buster); we re-attach the api key.
        if "?" in rel:
            rel = rel.split("?", 1)[0]
        return f"{base_url}{rel}?apikey={api_key}"
    return ""


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
            )
        )
    return out


async def _fetch_queue(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    entity_field: str,
) -> dict[str, int]:
    """Return a downloadId(lowercased) -> entity_id map for queue items.

    ``entity_field`` is ``"movieId"`` for Radarr and ``"seriesId"`` for Sonarr.
    Radarr's queue uses ``includeUnknownMovieItems`` to surface items that
    haven't yet matched a movie record; we still skip those (entity_id == 0)
    because we need a usable id to look up the rest of the metadata.
    """
    base = _base(url)
    out: dict[str, int] = {}
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
        if download_id and entity_id:
            out[download_id] = entity_id
    return out


async def fetch_radarr_queue(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, int]:
    return await _fetch_queue(client, url, api_key, entity_field="movieId")


async def fetch_sonarr_queue(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, int]:
    return await _fetch_queue(client, url, api_key, entity_field="seriesId")


async def _fetch_history(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    *,
    entity_field: str,
    pages: int = 4,
    page_size: int = 250,
) -> dict[str, int]:
    """Walk recent grabbed history and return downloadId -> entity_id.

    Sonarr / Radarr history paginates; we pull the most recent ``pages`` pages
    of ``eventType=grabbed`` records. That covers the typical "everything
    grabbed in the last few months" window without hammering the API.

    Most-recent record wins on collision (later grabs of the same hash should
    point at the latest entity id).
    """
    base = _base(url)
    out: dict[str, int] = {}
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
            if download_id and entity_id and download_id not in out:
                # First occurrence on a descending-by-date list IS the most
                # recent, so don't overwrite later (older) collisions.
                out[download_id] = entity_id
    return out


async def fetch_radarr_history(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, int]:
    return await _fetch_history(client, url, api_key, entity_field="movieId")


async def fetch_sonarr_history(
    client: httpx.AsyncClient, url: str, api_key: str
) -> dict[str, int]:
    return await _fetch_history(client, url, api_key, entity_field="seriesId")


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
