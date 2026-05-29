"""arr history dialog route -- a one-shot snapshot of Radarr/Sonarr history for
a single movie / series, rendered into the history modal."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from qbit_filter.state.store import Store
from qbit_filter.web import render

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/arr/history/{source}/{entity_id}", response_class=HTMLResponse)
async def arr_history(
    request: Request, source: str, entity_id: int
) -> HTMLResponse:
    """Return the rendered history dialog body for one movie / series.

    Used by the inline history badge on torrent rows -- clicking it
    ``hx-get``s this endpoint into ``#arr-history-dialog``. The endpoint
    is intentionally synchronous-style (no SSE): the dialog is a
    snapshot, not a live view, so a 200ms one-shot is fine.

    404 when ``source`` is unknown or arr isn't configured. Empty
    history (no events) still returns 200 with an "empty" body so the
    dialog opens with context instead of failing silently.
    """
    from qbit_filter.arr import client as arr_client

    if source not in ("radarr", "sonarr"):
        raise HTTPException(status_code=404, detail="unknown arr source")
    settings = request.app.state.settings
    if source == "radarr":
        url, api_key = settings.radarr_url, settings.radarr_api_key
    else:
        url, api_key = settings.sonarr_url, settings.sonarr_api_key
    if not (url and api_key):
        raise HTTPException(
            status_code=404, detail=f"{source} is not configured"
        )
    # Try to resolve a human-readable title from the live arr store.
    store: Store = request.app.state.store
    entity_title = ""
    if store.arr is not None:
        if source == "radarr":
            movie = store.arr.movies_by_id.get(entity_id)
            if movie is not None:
                entity_title = (
                    f"{movie.title}" + (f" ({movie.year})" if movie.year else "")
                )
        else:
            series = store.arr.series_by_id.get(entity_id)
            if series is not None:
                entity_title = series.title
    async with arr_client.make_client() as client:
        try:
            events = await arr_client.fetch_history_for_entity(
                client,
                url,
                api_key,
                source=source,
                entity_id=entity_id,
            )
        except arr_client.ArrUnavailable as exc:
            logger.warning("arr history dialog fetch failed: %s", exc)
            events = []
    return HTMLResponse(
        render.render_arr_history_dialog(
            request,
            source=source,
            entity_id=entity_id,
            entity_title=entity_title,
            events=events,
        )
    )
