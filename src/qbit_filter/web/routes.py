"""FastAPI routes."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from qbit_filter.cleanup.registry import BY_SLUG, RULES, is_implemented
from qbit_filter.domain import DomainEvent, EventKind, FilterState, Group, GroupKey
from qbit_filter.qbit import actions as qbit_actions
from qbit_filter.state.store import Store
from qbit_filter.state.subscribers import Subscription
from qbit_filter.state.viewport import Viewport
from qbit_filter.state.views import (
    apply_filters,
    count_by_facet,
    group_matches,
    seasons_of,
    torrent_matches,
)
from qbit_filter.web import filter_parse, render

logger = logging.getLogger(__name__)


SID_COOKIE = "qf_sid"
CACHE_COOKIE = "qf_has_cache"
# Bump when the rendered group/row HTML schema changes in a way the
# SSE-driven row updater can't gracefully handle on a stale cached payload
# (e.g. new field on every row, new wrapping element, renamed selector).
# Exposed to the client as ``window.QF_CACHE_VERSION`` so cached HTML from
# a previous version is discarded on next page load.
CACHE_VERSION = 1
SSE_PING_INTERVAL = 15.0  # seconds between keep-alive comments
# Minimum gap between RESYNC payloads to one client. Each RESYNC re-renders
# every visible group (~900KB blob), so back-to-back RESYNCs visibly stutter
# the UI. Coalescing drops subsequent RESYNCs within this window and lets
# the next non-coalesced one resync state.
RESYNC_COALESCE_INTERVAL = 1.0

# Sentinel string inserted into ``index.html`` while in stream mode. Split
# on this boundary to yield the page chrome before the group cards and the
# closing tags after.
_STREAM_MARKER = "<!--QF_STREAM_INSERT-->"

# Worker pool for streaming the initial page. Jinja2's renderer is a thin
# wrapper over C-extension code which releases the GIL during template
# bytecode execution, so a small pool gives a measurable wall-clock win for
# 100+ groups while keeping memory bounded. Module-level so it's shared
# across requests rather than re-created per request.
_RENDER_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qf-render")
# Flush threshold for the initial-page stream. Each rendered group is queued
# and grouped into chunks of this many bytes so the browser doesn't pay one
# TCP/HTTP framing cost per group while still flushing fast enough to start
# painting within tens of milliseconds.
_STREAM_FLUSH_BYTES = 16 * 1024


def _get_or_create_subscription(
    request: Request, response: Response | None, sid: str | None
) -> tuple[Subscription, str]:
    """Return (subscription, sid). If a new sid is minted and ``response`` is
    given, set the cookie on it.

    The Subscription is **not** added to the EventBus here. The SSE handler
    owns bus membership and refcounts active streams so multi-tab works and a
    disconnected client stops accumulating events.
    """
    subs: dict[str, Subscription] = request.app.state.subscriptions
    if sid and sid in subs:
        return subs[sid], sid
    new_sid = sid or secrets.token_urlsafe(16)
    sub = Subscription(filter_state=FilterState())
    subs[new_sid] = sub
    if response is not None:
        response.set_cookie(
            SID_COOKIE,
            new_sid,
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
    return sub, new_sid


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
    rule_bar_html = _render_rule_bar(request, store, sub.filter_state)
    return (
        groups_html
        + f'<div id="active-filters" hx-swap-oob="outerHTML" aria-live="polite">{active_html}</div>'
        + f'<div id="filter-facets" hx-swap-oob="outerHTML">{facets_html}</div>'
        + f'<div id="rule-bar-slot" hx-swap-oob="innerHTML">{rule_bar_html}</div>'
    )


def _render_rule_bar(request: Request, store: Store, fs: FilterState) -> str:
    """Render the rule selector chips, intersecting each rule's candidates
    with the active filter so the count reflects what clicking the chip
    will actually surface."""
    templates: Jinja2Templates = request.app.state.templates
    rows: list[dict[str, Any]] = []
    for r in RULES:
        impl = is_implemented(r)
        count = 0
        if impl:
            for c in r.candidates(store):
                t = store.torrents.get(c.torrent_hash)
                if t is None or c.group_key not in store.groups:
                    continue
                if not group_matches(store, fs, c.group_key):
                    continue
                if not torrent_matches(t, fs):
                    continue
                count += 1
        rows.append(
            {
                "slug": r.slug,
                "label": r.label,
                "description": r.description,
                "implemented": impl,
                "match_count": count,
            }
        )
    return templates.get_template("_rule_bar.html").render(request=request, rules=rows)


def register_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        qf_sid: str | None = Cookie(default=None),
        qf_has_cache: str | None = Cookie(default=None),
    ) -> Response:
        store: Store = request.app.state.store
        templates: Jinja2Templates = request.app.state.templates
        cookie_carrier = Response()
        sub, _sid = _get_or_create_subscription(request, cookie_carrier, qf_sid)
        visible = apply_filters(store, sub.filter_state)
        counts = count_by_facet(store)

        # Skip the (expensive) initial group render when the client tells us
        # it already has a fresh #groups snapshot in localStorage. Only honour
        # the hint for the default filter state -- a cached unfiltered list
        # can't represent a filtered view, so a returning visitor with active
        # filters gets a full server render. Search is part of FilterState
        # via active_count, so this naturally covers search too.
        cache_mode = (
            qf_has_cache == "1"
            and filter_parse.active_count(sub.filter_state) == 0
        )

        # Render the page chrome once with a placeholder where the group
        # cards will go. Split on the placeholder to get the bytes that
        # come before / after the group list. The browser parses HTML
        # incrementally, so flushing the head + app-bar + sidebar first
        # gives the user a usable shell within tens of milliseconds while
        # the (CPU-bound) group rendering catches up in the thread pool.
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
            cache_version=CACHE_VERSION,
            cache_mode=cache_mode,
            stream_mode=True,
        )
        before, after = chrome.split(_STREAM_MARKER, 1)

        group_tpl = templates.get_template("_group.html")
        empty_tpl = templates.get_template("_empty.html")

        def _render_group(group: Group) -> str:
            return group_tpl.render(
                request=request,
                group=group,
                torrents=store.torrents_in(group.key),
                seasons=seasons_of(group, store),
            )

        async def body() -> AsyncIterator[bytes]:
            yield before.encode("utf-8")
            if cache_mode:
                # Client paints #groups from localStorage. Skip the
                # CPU-heavy group render entirely and let the SSE channel
                # deliver row-level deltas on top of the cached snapshot.
                pass
            elif not visible:
                # Distinguish "store still warming up (no torrents yet)" from
                # "filter excludes everything". The former hands the screen to
                # the first-load progress overlay (see static/keys.js); the
                # latter shows a guidance message with a Clear-all button.
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
        return StreamingResponse(body(), media_type="text/html; charset=utf-8", headers=headers)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dev/version", response_class=Response)
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

    # ---------- SSE ----------------------------------------------------------

    @app.get("/sse")
    async def sse(
        request: Request,
        qf_sid: str | None = Cookie(default=None),
    ) -> StreamingResponse:
        store: Store = request.app.state.store
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        bus = request.app.state.bus

        async def stream() -> Any:
            # Register on the bus only while a stream is live. Refcount so
            # multi-tab (which shares one Subscription per sid) doesn't
            # remove the bus entry when one tab closes.
            bus.add(sub)
            sub.sse_refs += 1
            # Push an immediate RESYNC into this client's queue so the very
            # first SSE message replaces #groups with the live snapshot.
            # Without this, a client that painted stale HTML from
            # localStorage would keep showing it until a qBit-side change
            # triggered the reconciler's own RESYNC, which can be minutes
            # on a quiet instance. Coalesced with concurrent RESYNCs via
            # ``last_resync_at`` so back-to-back connects don't double-send.
            sub.notify(DomainEvent(kind=EventKind.RESYNC))
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        first = await asyncio.wait_for(
                            sub.queue.get(), timeout=SSE_PING_INTERVAL
                        )
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    # Reconciler.apply emits one event per changed torrent
                    # synchronously, so by the time we wake there may be a
                    # whole tick's worth of events queued. Drain + render
                    # once instead of N times -- the difference between ~1
                    # render/sec and ~1000 renders/sec at 1310 torrents.
                    batch: list[DomainEvent] = [first]
                    while True:
                        try:
                            batch.append(sub.queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    payload = _render_event_batch(request, store, sub, batch)
                    if payload:
                        yield f"event: message\ndata: {payload}\n\n"
            finally:
                sub.sse_refs -= 1
                if sub.sse_refs <= 0:
                    bus.remove(sub)
                    sub.drain()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ---------- Filters ------------------------------------------------------

    @app.post("/filters", response_class=HTMLResponse)
    async def post_filters(
        request: Request,
        facet: str = Form(...),
        value: str = Form(""),
        qf_sid: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        if not filter_parse.is_facet(facet):
            raise HTTPException(status_code=400, detail=f"unknown facet: {facet!r}")
        store: Store = request.app.state.store
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        sub.set_filter(filter_parse.toggle(sub.filter_state, facet, value))
        return HTMLResponse(_oob_payload(request, store, sub))

    @app.post("/filters/search", response_class=HTMLResponse)
    async def post_search(
        request: Request,
        search: str = Form(""),
        qf_sid: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        store: Store = request.app.state.store
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        sub.set_filter(filter_parse.set_search(sub.filter_state, search))
        return HTMLResponse(_oob_payload(request, store, sub))

    @app.post("/filters/clear", response_class=HTMLResponse)
    async def post_clear(
        request: Request,
        qf_sid: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        store: Store = request.app.state.store
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        sub.set_filter(filter_parse.clear())
        return HTMLResponse(_oob_payload(request, store, sub))

    # ---------- Actions ------------------------------------------------------

    def _client() -> Any:
        client = getattr(app.state, "qbit", None)
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="qBittorrent connection not available",
            )
        return client

    # ---------- Bulk actions -------------------------------------------------
    # IMPORTANT: bulk routes must be registered BEFORE the /{torrent_hash}
    # parameterised routes. Starlette matches in registration order; if the
    # parameterised route came first, DELETE /torrents/bulk would be captured
    # as {torrent_hash="bulk"} and the hashes form body would never be read.

    # Cap the number of hashes accepted in a single bulk request. Each call
    # hits qBit's REST API which has its own URL-length limits; 500 leaves
    # plenty of headroom for the typical "select all visible" use case.
    _BULK_MAX = 500

    _HEX_CHARS = frozenset("0123456789abcdef")

    def _parse_bulk(raw: str, *, store: Store | None = None) -> list[str]:
        # Client sends hashes joined by ``|`` (matches qBit's own wire format).
        # We accept ``,`` as well for forgiveness, dedupe, trim, and validate.
        # ``store`` opt-in restricts the result to known hashes -- pass it on
        # destructive endpoints (bulk delete / cleanup) so a stray cross-origin
        # form can't enumerate-and-purge hashes the user has never seen.
        seen: dict[str, None] = {}
        for h in raw.replace(",", "|").split("|"):
            h = h.strip().lower()
            if not h:
                continue
            if len(h) != 40 or any(c not in _HEX_CHARS for c in h):
                raise HTTPException(
                    status_code=400, detail=f"invalid infohash: {h!r}"
                )
            seen[h] = None
        out = list(seen)
        if len(out) > _BULK_MAX:
            raise HTTPException(
                status_code=413,
                detail=f"too many hashes ({len(out)} > {_BULK_MAX})",
            )
        if not out:
            raise HTTPException(status_code=400, detail="no hashes provided")
        if store is not None:
            unknown = [h for h in out if h not in store.torrents]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown infohash(es): {unknown[:3]}",
                )
        return out

    @app.post("/torrents/bulk/pause")
    async def bulk_pause(hashes: str = Form(...)) -> Response:
        await qbit_actions.pause(_client(), _parse_bulk(hashes))
        return Response(status_code=204)

    @app.post("/torrents/bulk/resume")
    async def bulk_resume(hashes: str = Form(...)) -> Response:
        await qbit_actions.resume(_client(), _parse_bulk(hashes))
        return Response(status_code=204)

    @app.post("/torrents/bulk/recheck")
    async def bulk_recheck(hashes: str = Form(...)) -> Response:
        await qbit_actions.recheck(_client(), _parse_bulk(hashes))
        return Response(status_code=204)

    @app.delete("/torrents/bulk")
    async def bulk_delete(request: Request, hashes: str = Form(...), purge: int = 0) -> Response:
        store: Store = request.app.state.store
        await qbit_actions.delete(
            _client(), _parse_bulk(hashes, store=store), purge=bool(purge)
        )
        return Response(status_code=204)

    @app.post("/torrents/bulk/cleanup")
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

        ``_parse_bulk`` enforces hex+length validation and intersects the
        list with ``store.torrents`` so a stray cross-origin form can't
        purge hashes the user has never seen.
        """
        if not hashes:
            return Response(status_code=204)
        store: Store = request.app.state.store
        hash_list = _parse_bulk(hashes, store=store)
        await qbit_actions.delete(_client(), hash_list, purge=bool(purge))
        return Response(status_code=204)

    # ---------- Cleanup rules ------------------------------------------------

    @app.get("/rules", response_class=HTMLResponse)
    async def list_rules(
        request: Request,
        qf_sid: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        """Render the rule-selector strip. Match counts are computed eagerly
        for implemented rules so the chip can show "Superseded quality (12)"
        without a second roundtrip.

        Counts are intersected with the subscription's active filter so the
        chip number matches what clicking the chip will reveal -- a category
        filter on ``radarr`` should not advertise candidates that live in
        ``sonarr`` groups, since the user can't see them.
        """
        store: Store = request.app.state.store
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        return HTMLResponse(_render_rule_bar(request, store, sub.filter_state))

    @app.post("/rules/{slug}/preview", response_class=HTMLResponse)
    async def preview_rule(
        request: Request,
        slug: str,
        qf_sid: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        """Run one rule against the live store and render only the matching
        groups, with the candidate rows pre-checked and reason chips inline.

        Intersects rule matches with the subscription's active
        :class:`FilterState` so that filtering down to e.g. category=radarr
        and then clicking a rule shows only candidates within that view --
        otherwise the selection footer would gain hashes from groups the
        user can't see, which is confusing and dangerous on bulk delete.
        """
        store: Store = request.app.state.store
        rule = BY_SLUG.get(slug)
        if rule is None or not is_implemented(rule):
            raise HTTPException(
                status_code=404, detail=f"unknown or unimplemented rule: {slug}"
            )
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        fs = sub.filter_state
        cands = rule.candidates(store)
        by_group: dict[GroupKey, dict[str, str]] = {}
        for c in cands:
            if c.group_key not in store.groups:
                continue
            if not group_matches(store, fs, c.group_key):
                continue
            t = store.torrents.get(c.torrent_hash)
            # Honour the user's torrent-level filter chips: a category filter
            # on ``radarr`` must not surface ``sonarr`` candidates even when
            # their group happens to pass (e.g. mixed-content group). Without
            # this, bulk-confirm would queue hashes the user cannot see.
            if t is None or not torrent_matches(t, fs):
                continue
            by_group.setdefault(c.group_key, {})[c.torrent_hash] = c.reason
        visible = [store.groups[k] for k in by_group]
        visible.sort(key=lambda g: g.title.lower())
        html = render.render_groups_payload(
            request,
            store,
            fs,
            visible=visible,
            rule_marks_by_group=by_group,
        )
        return HTMLResponse(html)

    # ---------- Viewport -----------------------------------------------------

    @app.post("/viewport")
    async def update_viewport(
        request: Request,
        qf_sid: str | None = Cookie(default=None),
        keys: str = Form(""),
    ) -> Response:
        """Client reports which group slugs are visible (+overscan). The
        subscription's :class:`Viewport` is what ``Subscription.notify`` reads
        to drop per-row SSE events for off-screen groups, the headline win
        for large stores."""
        sub, _ = _get_or_create_subscription(request, None, qf_sid)
        if not keys:
            sub.viewport = Viewport()
            return Response(status_code=204)
        hot = {s for s in keys.split("|") if s}
        sub.viewport = Viewport(hot=hot, updated_at=time.time())
        return Response(status_code=204)

    # ---------- Per-torrent actions ------------------------------------------

    @app.post("/torrents/{torrent_hash}/pause")
    async def pause(torrent_hash: str) -> Response:
        await qbit_actions.pause(_client(), torrent_hash)
        return Response(status_code=204)

    @app.post("/torrents/{torrent_hash}/resume")
    async def resume(torrent_hash: str) -> Response:
        await qbit_actions.resume(_client(), torrent_hash)
        return Response(status_code=204)

    @app.post("/torrents/{torrent_hash}/recheck")
    async def recheck(torrent_hash: str) -> Response:
        await qbit_actions.recheck(_client(), torrent_hash)
        return Response(status_code=204)

    @app.delete("/torrents/{torrent_hash}")
    async def remove(torrent_hash: str, purge: int = 0) -> Response:
        await qbit_actions.delete(_client(), torrent_hash, purge=bool(purge))
        return Response(status_code=204)


def _esc_for_sse(html: str) -> str:
    return html.replace("\r", "").replace("\n", "")


def _render_event_batch(
    request: Request,
    store: Store,
    sub: Subscription,
    events: list[DomainEvent],
) -> str:
    """Serialise one tick's worth of events into a single SSE ``data:`` line.

    Strategy:
    - Any RESYNC in the batch -> one full re-render of #groups + facets, skip
      the rest (they're already covered).
    - Otherwise dedupe per target: each affected group/torrent renders at most
      once. A group re-render shadows its torrent updates (whole-card swap
      includes the rows), so those are dropped.

    SSE event payload is a single line; newlines inside the HTML are stripped
    so htmx-ext-sse parses it as one ``data:`` field. ``hx-swap-oob`` lets a
    single ``message`` event update many DOM nodes.
    """
    fs = sub.filter_state

    has_resync = any(e.kind == EventKind.RESYNC for e in events)
    if has_resync:
        now = time.monotonic()
        if now - sub.last_resync_at < RESYNC_COALESCE_INTERVAL:
            # Recently sent a full RESYNC; skip this one and let the renderer
            # fall through to delta events for any non-RESYNC items in this
            # batch. The next RESYNC outside the coalesce window will rebase
            # state if anything was missed.
            events = [e for e in events if e.kind != EventKind.RESYNC]
            if not events:
                return ""
        else:
            sub.last_resync_at = now
            visible = apply_filters(store, fs)
            groups_html = render.render_groups_payload(request, store, fs, visible=visible)
            active_html = render.render_active_filters(request, store, fs, visible=visible)
            facets_html = render.render_filter_facets(request, store, fs)
            active_oob = (
                f'<div id="active-filters" hx-swap-oob="outerHTML" '
                f'aria-live="polite">{active_html}</div>'
            )
            payload = (
                f'<div id="groups" hx-swap-oob="innerHTML">{groups_html}</div>'
                + active_oob
                + f'<div id="filter-facets" hx-swap-oob="outerHTML">{facets_html}</div>'
            )
            return _esc_for_sse(payload)

    # Dedupe by target. dicts keep insertion order, which we use as render order.
    added_groups: dict[GroupKey, None] = {}
    removed_groups: dict[GroupKey, None] = {}
    added_torrents: dict[str, GroupKey] = {}
    changed_torrents: dict[str, GroupKey] = {}
    removed_torrents: dict[str, GroupKey] = {}

    for e in events:
        if e.kind == EventKind.GROUP_ADDED and e.group_key:
            added_groups[e.group_key] = None
        elif e.kind == EventKind.GROUP_REMOVED and e.group_key:
            removed_groups[e.group_key] = None
        elif e.kind == EventKind.TORRENT_ADDED and e.torrent_hash and e.group_key:
            added_torrents[e.torrent_hash] = e.group_key
        elif e.kind == EventKind.TORRENT_CHANGED and e.torrent_hash and e.group_key:
            changed_torrents[e.torrent_hash] = e.group_key
        elif e.kind == EventKind.TORRENT_REMOVED and e.torrent_hash and e.group_key:
            removed_torrents[e.torrent_hash] = e.group_key

    # A whole-group render already includes its torrent rows.
    shadowed_groups = set(added_groups) | set(removed_groups)
    for h in [h for h, gk in changed_torrents.items() if gk in shadowed_groups]:
        del changed_torrents[h]
    for h in [h for h, gk in added_torrents.items() if gk in shadowed_groups]:
        del added_torrents[h]
    for h in [h for h, gk in removed_torrents.items() if gk in shadowed_groups]:
        del removed_torrents[h]

    # Per-group visibility lookups, memoised for the batch. Avoids the
    # full ~622-group apply_filters pass on every tick: a typical batch
    # touches <10 groups, so we only evaluate those.
    visible_lookup: dict[GroupKey, bool] = {}

    def _visible(gk: GroupKey) -> bool:
        cached = visible_lookup.get(gk)
        if cached is None:
            cached = group_matches(store, fs, gk)
            visible_lookup[gk] = cached
        return cached

    # Group keys whose count chip we've already emitted in this batch -- we
    # only need one count update per group even if N rows came and went.
    counted: set[GroupKey] = set()

    def _count_oob(gk: GroupKey) -> str:
        group = store.groups.get(gk)
        if group is None or gk in counted:
            return ""
        counted.add(gk)
        n = len(group.torrent_hashes)
        label = "1 item" if n == 1 else f"{n} items"
        slug = gk.slug()
        return (
            f'<span id="group-{slug}-count" '
            f'hx-swap-oob="outerHTML">{label}</span>'
        )

    parts: list[str] = []

    for gk in removed_groups:
        parts.append(f'<div id="group-{gk.slug()}" hx-swap-oob="delete"></div>')

    for gk in added_groups:
        slug = gk.slug()
        if not _visible(gk):
            # Card was created server-side but doesn't pass the client's
            # filter -- nothing to render. The next filter change or
            # RESYNC will surface it if it starts matching.
            continue
        group = store.groups.get(gk)
        if group is None:
            continue
        torrents = store.torrents_in(gk)
        card_html = render.render_group(request, group, torrents, store=store)
        # New cards go into #groups via afterbegin; existing card lookups
        # using outerHTML would silently no-op for a brand-new id. Adding
        # qf-enter triggers the entrance animation -- only freshly inserted
        # cards animate; the initial page render and filter-driven redraws
        # do not.
        card_html = card_html.replace(
            'class="group-card"', 'class="group-card qf-enter"', 1
        )
        parts.append(card_html.replace(
            f'id="group-{slug}"',
            f'id="group-{slug}" hx-swap-oob="afterbegin:#groups"',
            1,
        ))

    for h, gk in removed_torrents.items():
        parts.append(f'<div id="torrent-{h}" hx-swap-oob="delete"></div>')
        parts.append(_count_oob(gk))

    for h, gk in changed_torrents.items():
        if not _visible(gk):
            continue
        t = store.torrents.get(h)
        if t is None:
            continue
        row = render.render_torrent(request, t)
        parts.append(row.replace(
            f'id="torrent-{t.hash}"',
            f'id="torrent-{t.hash}" hx-swap-oob="outerHTML"',
            1,
        ))

    for h, gk in added_torrents.items():
        if not _visible(gk):
            continue
        t = store.torrents.get(h)
        if t is None:
            continue
        slug = gk.slug()
        row = render.render_torrent(request, t)
        # qf-enter triggers the entrance animation on the new row only;
        # TORRENT_CHANGED rows above skip this so progress ticks don't
        # re-fire the animation on every update.
        row = row.replace(
            'class="torrent-row"', 'class="torrent-row qf-enter"', 1
        )
        # Insert at the end of the group body; preserves the original
        # append-order rendering.
        parts.append(row.replace(
            f'id="torrent-{t.hash}"',
            f'id="torrent-{t.hash}" hx-swap-oob="beforeend:#group-{slug}-body"',
            1,
        ))
        parts.append(_count_oob(gk))

    if not parts:
        return ""
    return _esc_for_sse("".join(parts))


# JSONResponse is imported for symmetry with future endpoints. Mark as used
# so unused-import linters stay quiet.
_ = (JSONResponse, json)
