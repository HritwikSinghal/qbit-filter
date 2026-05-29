"""Filter-mutation routes (facet toggle, search, clear) and the OOB payload
they return."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from qbit_filter.state.store import Store
from qbit_filter.state.subscribers import Subscription
from qbit_filter.state.views import apply_filters
from qbit_filter.web import filter_parse, render
from qbit_filter.web.routes._shared import get_or_create_subscription
from qbit_filter.web.routes.rules_preview import render_rule_bar

router = APIRouter()


def _oob_payload(request: Request, store: Store, sub: Subscription) -> str:
    """Compose the multi-swap HTML returned by filter-mutating endpoints.

    Main swap target is `#groups` (inner HTML). OOB swaps refresh the
    active-filter strip, the facet chips, and the rule bar so their
    counts/active state update without a full reload. The rule-bar counts
    depend on the active filter, so a filter change must re-render it.
    """
    # One apply_filters pass per request, shared with both render helpers.
    visible = apply_filters(store, sub.filter_state)
    groups_html = render.render_groups_payload(
        request, store, sub.filter_state, visible=visible
    )
    active_html = render.render_active_filters(
        request, store, sub.filter_state, visible=visible
    )
    facets_html = render.render_filter_facets(request, store, sub.filter_state)
    rule_bar_html = render_rule_bar(request, store, sub.filter_state)
    return (
        groups_html
        + f'<div id="active-filters" hx-swap-oob="outerHTML" aria-live="polite">{active_html}</div>'
        + f'<div id="filter-facets" hx-swap-oob="outerHTML">{facets_html}</div>'
        + f'<div id="rule-bar-slot" hx-swap-oob="innerHTML">{rule_bar_html}</div>'
    )


@router.post("/filters", response_class=HTMLResponse)
async def post_filters(
    request: Request,
    facet: str = Form(...),
    value: str = Form(""),
    qf_sid: str | None = Cookie(default=None),
) -> HTMLResponse:
    if not filter_parse.is_facet(facet):
        raise HTTPException(status_code=400, detail=f"unknown facet: {facet!r}")
    store: Store = request.app.state.store
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    sub.set_filter(filter_parse.toggle(sub.filter_state, facet, value))
    return HTMLResponse(_oob_payload(request, store, sub))


@router.post("/filters/search", response_class=HTMLResponse)
async def post_search(
    request: Request,
    search: str = Form(""),
    qf_sid: str | None = Cookie(default=None),
) -> HTMLResponse:
    store: Store = request.app.state.store
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    sub.set_filter(filter_parse.set_search(sub.filter_state, search))
    return HTMLResponse(_oob_payload(request, store, sub))


@router.post("/filters/clear", response_class=HTMLResponse)
async def post_clear(
    request: Request,
    qf_sid: str | None = Cookie(default=None),
) -> HTMLResponse:
    store: Store = request.app.state.store
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    sub.set_filter(filter_parse.clear())
    return HTMLResponse(_oob_payload(request, store, sub))
