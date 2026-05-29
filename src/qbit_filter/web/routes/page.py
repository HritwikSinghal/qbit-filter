"""Initial page render (streamed) plus health and dev-livereload routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from qbit_filter.domain import Group
from qbit_filter.state.store import Store
from qbit_filter.state.views import (
    apply_filters,
    count_by_facet,
    seasons_of,
    torrents_for_group,
)
from qbit_filter.web import filter_parse
from qbit_filter.web.routes._shared import _RENDER_POOL, get_or_create_subscription

router = APIRouter()

# Sentinel string inserted into ``index.html`` while in stream mode. Split
# on this boundary to yield the page chrome before the group cards and the
# closing tags after.
_STREAM_MARKER = "<!--QF_STREAM_INSERT-->"

# Flush threshold for the initial-page stream. Each rendered group is queued
# and grouped into chunks of this many bytes so the browser doesn't pay one
# TCP/HTTP framing cost per group while still flushing fast enough to start
# painting within tens of milliseconds.
_STREAM_FLUSH_BYTES = 16 * 1024


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    qf_sid: str | None = Cookie(default=None),
) -> Response:
    store: Store = request.app.state.store
    templates: Jinja2Templates = request.app.state.templates
    cookie_carrier = Response()
    sub, _sid = get_or_create_subscription(request, cookie_carrier, qf_sid)
    visible = apply_filters(store, sub.filter_state)
    counts = count_by_facet(store)

    # Render the page chrome once with a placeholder where the group
    # cards will go. Split on the placeholder to get the bytes that
    # come before / after the group list. The browser parses HTML
    # incrementally, so flushing the head + app-bar + sidebar first
    # gives the user a usable shell within tens of milliseconds while
    # the (CPU-bound) group rendering catches up in the thread pool.
    arr_configured = store.arr is not None and store.arr.configured
    chrome = templates.get_template("index.html").render(
        request=request,
        filter_state=sub.filter_state,
        visible_groups=[],
        # ``visible_count`` is the true number for the active-filter
        # strip's "Showing N of M". The chrome runs with an empty
        # ``visible_groups`` placeholder so the rest of the page can
        # stream into the marker, but the strip needs the real count
        # right away.
        visible_count=len(visible),
        total_torrents=len(store.torrents),
        total_groups=len(store.groups),
        counts=counts,
        active_count=filter_parse.active_count(sub.filter_state),
        stream_mode=True,
        store_arr_configured=arr_configured,
    )
    before, after = chrome.split(_STREAM_MARKER, 1)

    group_tpl = templates.get_template("_group.html")
    empty_tpl = templates.get_template("_empty.html")

    fs = sub.filter_state

    def _render_group(group: Group) -> str:
        torrents = torrents_for_group(store, group.key, fs)
        arr_meta = None
        arr_matches: dict[str, Any] = {}
        if store.arr is not None and store.arr.hash_to_arr:
            for t in torrents:
                m = store.arr.hash_to_arr.get(t.hash.lower())
                if m is not None:
                    arr_matches[t.hash] = m
                    if arr_meta is None:
                        arr_meta = m
        return group_tpl.render(
            request=request,
            group=group,
            torrents=torrents,
            seasons=seasons_of(group, store),
            arr_meta=arr_meta,
            arr_matches=arr_matches,
        )

    async def body() -> AsyncIterator[bytes]:
        yield before.encode("utf-8")
        if not visible:
            # Distinguish "store still warming up (no torrents yet)" from
            # "filter excludes everything". The former hands the screen to
            # the header activity widget (the chrome ships it in the
            # "Contacting qBittorrent" state, and applyBatchStaging fills
            # #groups as RESYNC_PARTIAL chunks land); the latter shows a
            # guidance message with a Clear-all button.
            if (
                not store.torrents
                and filter_parse.active_count(sub.filter_state) == 0
            ):
                pass
            else:
                yield empty_tpl.render(
                    request=request,
                    filter_state=sub.filter_state,
                    total_torrents=len(store.torrents),
                ).encode("utf-8")
        else:
            loop = asyncio.get_running_loop()
            # Submit every render up front. The pool runs up to
            # ``max_workers`` concurrently and queues the rest, so as we
            # await futures in input order the lookahead workers have
            # already started on later groups. Output order matches the
            # sorted ``visible`` list because we await sequentially; the
            # parallelism is hidden in the pool, not in the await order.
            futures = [
                loop.run_in_executor(_RENDER_POOL, _render_group, g)
                for g in visible
            ]
            buf: list[str] = []
            buf_size = 0
            for fut in futures:
                piece = await fut
                buf.append(piece)
                buf_size += len(piece)
                if buf_size >= _STREAM_FLUSH_BYTES:
                    yield "".join(buf).encode("utf-8")
                    buf.clear()
                    buf_size = 0
            if buf:
                yield "".join(buf).encode("utf-8")
        yield after.encode("utf-8")

    headers = {"Cache-Control": "no-store"}
    for k, v in cookie_carrier.headers.items():
        if k.lower() == "set-cookie":
            headers["Set-Cookie"] = v
    return StreamingResponse(
        body(), media_type="text/html; charset=utf-8", headers=headers
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/dev/version", response_class=Response)
async def dev_version(request: Request) -> Response:
    # Browser livereload poll target. Returns the per-process boot id so
    # clients reload after a uvicorn `--reload` restart. 404 in prod so the
    # poll script becomes a no-op.
    settings = request.app.state.settings
    if not settings.dev_mode:
        raise HTTPException(status_code=404)
    return Response(
        content=request.app.state.boot_id,
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )
