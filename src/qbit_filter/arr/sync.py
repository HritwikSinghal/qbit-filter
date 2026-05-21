"""Async polling loop -- one tick yields one :class:`ArrSnapshot`.

Polls Radarr + Sonarr concurrently via ``asyncio.gather`` so a slow Sonarr
doesn't block a fast Radarr (and vice versa). Skips the call entirely when
the URL is unset. Returns an empty-but-ok snapshot when both URLs are unset
so the caller can treat "configured but down" the same as "configured but
empty library".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from qbit_filter.arr import client as arr_client
from qbit_filter.arr.models import ArrMovie, ArrSeries, ArrSnapshot, QualityProfile
from qbit_filter.config import Settings

logger = logging.getLogger(__name__)


async def _fetch_radarr_all(
    client: httpx.AsyncClient, url: str, api_key: str
) -> tuple[list[ArrMovie], dict[str, int], dict[str, int], dict[int, QualityProfile]]:
    movies = await arr_client.fetch_movies(client, url, api_key)
    queue = await arr_client.fetch_radarr_queue(client, url, api_key)
    history = await arr_client.fetch_radarr_history(client, url, api_key)
    profiles = await arr_client.fetch_quality_profiles(client, url, api_key)
    return movies, queue, history, profiles


async def _fetch_sonarr_all(
    client: httpx.AsyncClient, url: str, api_key: str
) -> tuple[list[ArrSeries], dict[str, int], dict[str, int], dict[int, QualityProfile]]:
    series = await arr_client.fetch_series(client, url, api_key)
    queue = await arr_client.fetch_sonarr_queue(client, url, api_key)
    history = await arr_client.fetch_sonarr_history(client, url, api_key)
    profiles = await arr_client.fetch_quality_profiles(client, url, api_key)
    return series, queue, history, profiles


async def fetch_once(settings: Settings) -> ArrSnapshot:
    """Pull one snapshot from whichever *arr instances are configured.

    Returns ``ArrSnapshot(ok=False)`` only when no instance is configured;
    a configured-but-unreachable instance produces an ``ok=True`` snapshot
    with empty lists so downstream filters still update (e.g. "Library"
    sidebar still renders, just empty until the next successful poll).
    """
    radarr_on = bool(settings.radarr_url and settings.radarr_api_key)
    sonarr_on = bool(settings.sonarr_url and settings.sonarr_api_key)
    if not radarr_on and not sonarr_on:
        return ArrSnapshot(ok=False)

    snap = ArrSnapshot(ok=True)
    async with arr_client.make_client() as client:
        radarr_task: asyncio.Task[
            tuple[list[ArrMovie], dict[str, int], dict[str, int], dict[int, QualityProfile]]
        ] | None = None
        sonarr_task: asyncio.Task[
            tuple[list[ArrSeries], dict[str, int], dict[str, int], dict[int, QualityProfile]]
        ] | None = None
        if radarr_on:
            radarr_task = asyncio.create_task(
                _fetch_radarr_all(client, settings.radarr_url, settings.radarr_api_key)
            )
        if sonarr_on:
            sonarr_task = asyncio.create_task(
                _fetch_sonarr_all(client, settings.sonarr_url, settings.sonarr_api_key)
            )
        if radarr_task is not None:
            try:
                movies, r_queue, r_history, r_profiles = await radarr_task
                snap.movies = list(movies)
                snap.radarr_queue = dict(r_queue)
                snap.radarr_history = dict(r_history)
                snap.quality_profiles_radarr = dict(r_profiles)
            except arr_client.ArrUnavailable as exc:
                logger.warning("arr fetch failed for radarr: %s", exc)
            except Exception:
                logger.exception("arr fetch raised unexpectedly for radarr")
        if sonarr_task is not None:
            try:
                series, s_queue, s_history, s_profiles = await sonarr_task
                snap.series = list(series)
                snap.sonarr_queue = dict(s_queue)
                snap.sonarr_history = dict(s_history)
                snap.quality_profiles_sonarr = dict(s_profiles)
            except arr_client.ArrUnavailable as exc:
                logger.warning("arr fetch failed for sonarr: %s", exc)
            except Exception:
                logger.exception("arr fetch raised unexpectedly for sonarr")
    return snap


async def poll_arr(settings: Settings) -> AsyncIterator[ArrSnapshot]:
    """Async generator. First yield happens immediately; subsequent yields are
    spaced by ``settings.arr_poll_interval_seconds``. Cancellation closes the
    loop cleanly.
    """
    interval = max(5.0, float(settings.arr_poll_interval_seconds))
    while True:
        snap = await fetch_once(settings)
        yield snap
        if not snap.ok:
            # Nothing configured -- terminate the iteration so the caller can
            # exit its task without burning CPU.
            return
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
