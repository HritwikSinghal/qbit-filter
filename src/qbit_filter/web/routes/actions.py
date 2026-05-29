"""Torrent action routes: bulk, per-torrent, cleanup-confirm, and viewport.

IMPORTANT: bulk routes (``/torrents/bulk/...``) are declared BEFORE the
``/torrents/{torrent_hash}`` parameterised routes. Starlette matches in
registration order; if the parameterised route came first, DELETE
``/torrents/bulk`` would be captured as ``{torrent_hash="bulk"}`` and the
hashes form body would never be read. Keep them in this file, in this order.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Cookie, Form, Request, Response

from qbit_filter.qbit import actions as qbit_actions
from qbit_filter.state.store import Store
from qbit_filter.state.viewport import Viewport
from qbit_filter.web.routes._shared import (
    get_client,
    get_or_create_subscription,
    parse_bulk,
)

router = APIRouter()


# ---------- Bulk actions (must precede /{torrent_hash}) -------------------

@router.post("/torrents/bulk/pause")
async def bulk_pause(request: Request, hashes: str = Form(...)) -> Response:
    await qbit_actions.pause(get_client(request), parse_bulk(hashes))
    return Response(status_code=204)


@router.post("/torrents/bulk/resume")
async def bulk_resume(request: Request, hashes: str = Form(...)) -> Response:
    await qbit_actions.resume(get_client(request), parse_bulk(hashes))
    return Response(status_code=204)


@router.post("/torrents/bulk/recheck")
async def bulk_recheck(request: Request, hashes: str = Form(...)) -> Response:
    await qbit_actions.recheck(get_client(request), parse_bulk(hashes))
    return Response(status_code=204)


@router.delete("/torrents/bulk")
async def bulk_delete(
    request: Request, hashes: str = Form(...), purge: int = 0
) -> Response:
    store: Store = request.app.state.store
    await qbit_actions.delete(
        get_client(request), parse_bulk(hashes, store=store), purge=bool(purge)
    )
    return Response(status_code=204)


@router.post("/torrents/bulk/cleanup")
async def apply_cleanup(
    request: Request,
    hashes: str = Form(""),
    purge: int = Form(0),
) -> Response:
    """Confirm-step of the cleanup workflow.

    Posted by the selection footer once the user has reviewed a rule's
    candidates and unchecked any mistakes. The reconciler picks up the
    qBit-side removals on its next poll and SSE-pushes the row deletes,
    so we don't need to mutate the store here.

    ``parse_bulk`` enforces hex+length validation and intersects the
    list with ``store.torrents`` so a stray cross-origin form can't
    purge hashes the user has never seen.
    """
    if not hashes:
        return Response(status_code=204)
    store: Store = request.app.state.store
    hash_list = parse_bulk(hashes, store=store)
    await qbit_actions.delete(get_client(request), hash_list, purge=bool(purge))
    return Response(status_code=204)


# ---------- Viewport -----------------------------------------------------

@router.post("/viewport")
async def update_viewport(
    request: Request,
    qf_sid: str | None = Cookie(default=None),
    keys: str = Form(""),
) -> Response:
    """Client reports which group slugs are visible (+overscan). The
    subscription's :class:`Viewport` is what ``Subscription.notify`` reads
    to drop per-row SSE events for off-screen groups, the headline win
    for large stores."""
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    if not keys:
        sub.viewport = Viewport()
        return Response(status_code=204)
    hot = {s for s in keys.split("|") if s}
    sub.viewport = Viewport(hot=hot, updated_at=time.time())
    return Response(status_code=204)


# ---------- Per-torrent actions ------------------------------------------

@router.post("/torrents/{torrent_hash}/pause")
async def pause(request: Request, torrent_hash: str) -> Response:
    await qbit_actions.pause(get_client(request), torrent_hash)
    return Response(status_code=204)


@router.post("/torrents/{torrent_hash}/resume")
async def resume(request: Request, torrent_hash: str) -> Response:
    await qbit_actions.resume(get_client(request), torrent_hash)
    return Response(status_code=204)


@router.post("/torrents/{torrent_hash}/recheck")
async def recheck(request: Request, torrent_hash: str) -> Response:
    await qbit_actions.recheck(get_client(request), torrent_hash)
    return Response(status_code=204)


@router.delete("/torrents/{torrent_hash}")
async def remove(request: Request, torrent_hash: str, purge: int = 0) -> Response:
    await qbit_actions.delete(get_client(request), torrent_hash, purge=bool(purge))
    return Response(status_code=204)
