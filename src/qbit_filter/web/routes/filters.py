"""Filter-mutation routes (facet toggle, search, clear).

These mutate the subscription's :class:`FilterState` and enqueue a
``RESYNC_FILTER`` event to that one client's SSE queue, then return 204. The
heavy re-render (groups + chrome OOBs) is delivered over SSE by
``sse._emit_resync_batches`` -- the same machinery a poller RESYNC uses --
instead of being rebuilt synchronously per click. ``RESYNC_FILTER`` is
coalesce-exempt so a click never gets swallowed by the poller's 1 s window.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Form, HTTPException, Request, Response

from qbit_filter.domain import DomainEvent, EventKind
from qbit_filter.state.subscribers import Subscription
from qbit_filter.web import filter_parse
from qbit_filter.web.routes._shared import get_or_create_subscription

router = APIRouter()


def _enqueue_filter_resync(sub: Subscription) -> None:
    """Wake this subscription's SSE stream to re-render against the just-changed
    filter. Per-client (not ``bus.publish``) so other tabs are unaffected."""
    sub.notify(DomainEvent(kind=EventKind.RESYNC_FILTER))


@router.post("/filters")
async def post_filters(
    request: Request,
    facet: str = Form(...),
    value: str = Form(""),
    qf_sid: str | None = Cookie(default=None),
) -> Response:
    if not filter_parse.is_facet(facet):
        raise HTTPException(status_code=400, detail=f"unknown facet: {facet!r}")
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    sub.set_filter(filter_parse.toggle(sub.filter_state, facet, value))
    _enqueue_filter_resync(sub)
    return Response(status_code=204)


@router.post("/filters/search")
async def post_search(
    request: Request,
    search: str = Form(""),
    qf_sid: str | None = Cookie(default=None),
) -> Response:
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    sub.set_filter(filter_parse.set_search(sub.filter_state, search))
    _enqueue_filter_resync(sub)
    return Response(status_code=204)


@router.post("/filters/clear")
async def post_clear(
    request: Request,
    qf_sid: str | None = Cookie(default=None),
) -> Response:
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    sub.set_filter(filter_parse.clear())
    _enqueue_filter_resync(sub)
    return Response(status_code=204)
