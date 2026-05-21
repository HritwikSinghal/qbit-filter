"""FastAPI routes."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from qbit_filter.cleanup.registry import BY_SLUG, RULES, is_implemented
from qbit_filter.domain import DomainEvent, EventKind, FilterState, Group, GroupKey, Torrent
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
    torrents_for_group,
)
from qbit_filter.web import filter_parse, render

logger = logging.getLogger(__name__)


SID_COOKIE = "qf_sid"
SSE_PING_INTERVAL = 15.0  # seconds between keep-alive comments
# Minimum gap between full RESYNC payloads to one client. Each RESYNC
# re-renders every visible group, so back-to-back RESYNCs from the two
# pollers (qBit + arr ticking close together) visibly stutter the UI.
# Coalescing drops subsequent RESYNCs within this window. RESYNC_PARTIAL
# events bypass this guard during cold-boot so chunks stream out.
RESYNC_COALESCE_INTERVAL = 1.0
# Floor on RESYNC_PARTIAL pacing. The reconciler's chunk loop is naturally
# throttled (each chunk's parse takes 50-150 ms) but defend against a
# runaway publisher by enforcing a minimum gap between consecutive partial
# renders to the same client.
RESYNC_PARTIAL_MIN_INTERVAL = 0.1

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

# Adaptive batch sizes for a RESYNC delivered over SSE. The first batch is
# intentionally tiny so the user sees content within ~1 frame; later batches
# expand so total wall-clock isn't dominated by per-batch framing overhead.
# After the last entry, the final size is reused for any remaining groups.
_RESYNC_BATCH_SIZES = (10, 25, 50, 100, 100, 100)


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


def _render_rule_bar(
    request: Request,
    store: Store,
    fs: FilterState,
    active_slug: str = "",
) -> str:
    """Render the rule selector chips, intersecting each rule's candidates
    with the active filter so the count reflects what clicking the chip
    will actually surface. ``active_slug`` marks the currently-previewed
    rule (aria-pressed) so the chip stays visually pressed across SSE
    re-renders of the rule bar."""
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
                if not torrent_matches(t, fs, store):
                    continue
                count += 1
        rows.append(
            {
                "slug": r.slug,
                "label": r.label,
                "description": r.description,
                "implemented": impl,
                "match_count": count,
                "active": r.slug == active_slug,
            }
        )
    return templates.get_template("_rule_bar.html").render(
        request=request, rules=rows, active_slug=active_slug
    )


def _rule_preview_context(
    store: Store, sub: Subscription
) -> tuple[
    dict[GroupKey, dict[str, str]],
    dict[GroupKey, str],
    dict[GroupKey, dict[str, tuple[Any, ...]]],
    dict[GroupKey, dict[str, str]],
    list[GroupKey],
]:
    """Compute rule preview context for the subscription's active rule.

    Returns ``(marks_by_group, keepers_by_group, factors_by_group,
    severity_by_group, ordered_group_keys)``. When ``sub.active_rule_slug``
    is empty or the rule no longer exists, returns empty dicts and an empty
    list. ``ordered_group_keys`` is the title-sorted list of groups the
    rule matched -- used by ``/rules/{slug}/preview`` to scope the visible
    set to just the matching groups, but ignored by SSE renders that need
    rule context overlaid on the full visible set.
    """
    slug = sub.active_rule_slug
    if not slug:
        return {}, {}, {}, {}, []
    rule = BY_SLUG.get(slug)
    if rule is None or not is_implemented(rule):
        return {}, {}, {}, {}, []
    fs = sub.filter_state
    by_group: dict[GroupKey, dict[str, str]] = {}
    keepers: dict[GroupKey, str] = {}
    factors_by_group: dict[GroupKey, dict[str, tuple[Any, ...]]] = {}
    severity_by_group: dict[GroupKey, dict[str, str]] = {}
    for c in rule.candidates(store):
        if c.group_key not in store.groups:
            continue
        if not group_matches(store, fs, c.group_key):
            continue
        t = store.torrents.get(c.torrent_hash)
        if t is None or not torrent_matches(t, fs, store):
            continue
        by_group.setdefault(c.group_key, {})[c.torrent_hash] = c.reason
        factors_by_group.setdefault(c.group_key, {})[c.torrent_hash] = c.factors
        if c.severity != "normal":
            severity_by_group.setdefault(c.group_key, {})[
                c.torrent_hash
            ] = c.severity
        if c.keeper_hash and c.group_key not in keepers:
            keepers[c.group_key] = c.keeper_hash
    ordered = sorted(by_group.keys(), key=lambda k: store.groups[k].title.lower())
    return by_group, keepers, factors_by_group, severity_by_group, ordered


def register_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        qf_sid: str | None = Cookie(default=None),
    ) -> Response:
        store: Store = request.app.state.store
        templates: Jinja2Templates = request.app.state.templates
        cookie_carrier = Response()
        sub, _sid = _get_or_create_subscription(request, cookie_carrier, qf_sid)
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
                # the batched-RESYNC progress block (see static/keys.js
                # setupLoadProgress + applyBatchStaging); the latter shows a
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
            # first SSE message delivers the live snapshot. Without this, a
            # client connecting to a quiet qBit instance would stare at the
            # "Loading torrent list..." placeholder until the next reconciler
            # tick, which can be minutes apart. Coalesced via
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
                    for payload in _render_event_batch_iter(
                        request, store, sub, batch
                    ):
                        if payload:
                            yield f"event: message\ndata: {payload}\n\n"
                            # Cooperative yield so each batch reaches the wire
                            # before we render the next one; otherwise large
                            # RESYNCs render N batches synchronously and only
                            # flush at the end, defeating the purpose.
                            await asyncio.sleep(0)
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
        groups, with the candidate rows highlighted and reason chips inline.

        Clicking the currently-active rule chip clears the preview (toggle).
        The chosen slug is persisted on the subscription so SSE-driven
        re-renders (RESYNC + per-row TORRENT_CHANGED) keep the compare-strip
        layout instead of flattening back to the plain row stack.

        Intersects rule matches with the subscription's active
        :class:`FilterState` so that filtering down to e.g. category=radarr
        and then clicking a rule shows only candidates within that view --
        otherwise the selection footer would gain hashes from groups the
        user can't see, which is confusing and dangerous on bulk delete.

        The response also returns an OOB ``data-just-activated`` attribute
        on the staging marker so the client can do its one-shot
        bulk-auto-select on the freshly-rendered flagged rows without
        re-checking them on every subsequent SSE re-render.
        """
        store: Store = request.app.state.store
        rule = BY_SLUG.get(slug)
        if rule is None or not is_implemented(rule):
            raise HTTPException(
                status_code=404, detail=f"unknown or unimplemented rule: {slug}"
            )
        sub, _ = _get_or_create_subscription(request, None, qf_sid)

        toggled_off = sub.active_rule_slug == slug
        if toggled_off:
            sub.active_rule_slug = ""
        else:
            sub.active_rule_slug = slug

        fs = sub.filter_state
        rule_bar_html = _render_rule_bar(request, store, fs, sub.active_rule_slug)
        rule_bar_oob = (
            f'<div id="rule-bar-slot" hx-swap-oob="innerHTML">{rule_bar_html}</div>'
        )

        if toggled_off:
            # Clearing: show the standard unfiltered view (no marks). The
            # client-side `pendingRuleActivation` marker is NOT emitted so
            # afterSwap won't bulk-select anything.
            visible_groups = apply_filters(store, fs)
            html = render.render_groups_payload(
                request, store, fs, visible=visible_groups
            )
            return HTMLResponse(html + rule_bar_oob)

        by_group, keepers, factors_by_group, severity_by_group, ordered = (
            _rule_preview_context(store, sub)
        )
        visible = [store.groups[k] for k in ordered]
        html = render.render_groups_payload(
            request,
            store,
            fs,
            visible=visible,
            rule_marks_by_group=by_group,
            rule_keepers_by_group=keepers,
            rule_factors_by_group=factors_by_group,
            rule_severity_by_group=severity_by_group,
            pre_check_flagged=True,
        )
        # Emit a one-shot marker INSIDE the #groups innerHTML swap so the
        # client knows THIS swap was triggered by a fresh rule activation
        # and should bulk-add flagged rows to the selection Map. Subsequent
        # SSE re-renders won't include the marker, so user edits to the
        # selection stick. Has to live inside the swap content because
        # ``hx-swap-oob`` targeting a non-existent id silently no-ops.
        activation_marker = (
            f'<span id="qf-rule-activation" data-slug="{slug}" hidden></span>'
        )
        return HTMLResponse(activation_marker + html + rule_bar_oob)

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

    # ---------- arr history dialog ------------------------------------------

    @app.get("/arr/history/{source}/{entity_id}", response_class=HTMLResponse)
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


def _esc_for_sse(html: str) -> str:
    return html.replace("\r", "").replace("\n", "")


def _render_event_batch_iter(
    request: Request,
    store: Store,
    sub: Subscription,
    events: list[DomainEvent],
) -> Iterator[str]:
    """Yield one or more SSE ``data:`` payloads for a tick's events.

    A RESYNC in the batch fans out into multiple payloads via
    :func:`_emit_resync_batches` so the browser paints the group list
    incrementally (adaptive 10/25/50/100... cards per batch) instead of
    blocking on one ~2.5 MB swap. Delta-only batches still yield a single
    deduped payload.

    Cold-boot :class:`EventKind.RESYNC_PARTIAL` events render the same way
    as RESYNC but skip the coalesce window so each reconciler chunk reaches
    the browser without being collapsed into a later one. The natural
    chunk pace (~50-150 ms per chunk for parsing) plus a 100 ms floor
    guards against runaway publishers.
    """
    has_resync = any(e.kind == EventKind.RESYNC for e in events)
    has_partial = any(e.kind == EventKind.RESYNC_PARTIAL for e in events)
    # Drop any RESYNC/RESYNC_PARTIAL when the store has no torrents yet --
    # emitting a snapshot payload against an empty store would wipe the
    # client-side "Loading torrent list..." progress UI. The reconciler's
    # first chunk lands data BEFORE its first RESYNC_PARTIAL fires, so this
    # only affects the synthetic connect-time RESYNC.
    if (has_resync or has_partial) and not store.torrents:
        events = [
            e for e in events
            if e.kind not in (EventKind.RESYNC, EventKind.RESYNC_PARTIAL)
        ]
        if not events:
            return
        has_resync = False
        has_partial = False
    if has_resync or has_partial:
        now = time.monotonic()
        # RESYNC_PARTIAL bypasses the 1 s coalesce window but honours a
        # 100 ms floor. A full RESYNC in the same batch upgrades the
        # treatment to "full RESYNC" semantics so the canonical-slugs
        # final batch still lands and the load-progress UI clears.
        if has_resync:
            window = RESYNC_COALESCE_INTERVAL
            last = sub.last_resync_at
        else:
            window = RESYNC_PARTIAL_MIN_INTERVAL
            last = max(sub.last_resync_at, sub.last_partial_at)
        if now - last >= window:
            sub.last_resync_at = now
            sub.last_partial_at = now
            is_final = has_resync
            yield from _emit_resync_batches(request, store, sub, is_final=is_final)
            # The batched snapshot is authoritative; drop any delta events
            # in the same tick. Their state is already reflected.
            return
        # Throttle window hit -- drop the RESYNC/RESYNC_PARTIAL, let deltas through.
        events = [
            e for e in events
            if e.kind not in (EventKind.RESYNC, EventKind.RESYNC_PARTIAL)
        ]
        if not events:
            return

    payload = _render_delta_events(request, store, sub, events)
    if payload:
        yield payload


def _emit_resync_batches(
    request: Request,
    store: Store,
    sub: Subscription,
    *,
    is_final: bool = True,
) -> Iterator[str]:
    """Yield batched SSE payloads that incrementally rebuild ``#groups``.

    Each payload contains:
    - ``#qf-batch-staging`` (OOB outerHTML) carrying the rendered group cards
      for this batch. The client moves children into ``#groups`` with a
      replace-or-append heuristic so warm RESYNCs swap in-place and cold
      RESYNCs append in sorted order.
    - ``#qf-load-progress`` (OOB innerHTML) carrying the visible progress
      block. Empty on the final batch of the final RESYNC so CSS hides it.
    - Chrome OOBs (``#active-filters``, ``#filter-facets``) on the first
      batch so headline counts update with the snapshot.

    Only the last batch of the FINAL RESYNC carries ``data-final="1"`` and
    the canonical ordered group-slug list. Cold-boot RESYNC_PARTIALs leave
    those empty so the client doesn't prune cards that are about to land
    in the next chunk.
    """
    fs = sub.filter_state
    visible = apply_filters(store, fs)
    total = len(visible)

    # Rule-preview context (active across SSE re-renders so the compare-strip
    # layout persists). Empty dicts when no rule is active -- the templates
    # then fall through to the flat row stack as before.
    rule_marks, rule_keepers, rule_factors, rule_severity, _ = (
        _rule_preview_context(store, sub)
    )

    active_html = render.render_active_filters(request, store, fs, visible=visible)
    facets_html = render.render_filter_facets(request, store, fs)
    rule_bar_html = _render_rule_bar(request, store, fs, sub.active_rule_slug)
    chrome_oobs = (
        f'<div id="active-filters" hx-swap-oob="outerHTML" '
        f'aria-live="polite">{active_html}</div>'
        f'<div id="filter-facets" hx-swap-oob="outerHTML">{facets_html}</div>'
        f'<div id="rule-bar-slot" hx-swap-oob="innerHTML">{rule_bar_html}</div>'
    )

    if total == 0:
        if not is_final:
            # Partial RESYNC fired before any visible groups exist. Skip --
            # the next partial / final will deliver content. Keep the
            # client-side loading placeholder visible.
            return
        # Filter excludes everything (or store empty). Wipe #groups, remove
        # any in-flight progress UI, and let the empty-state template render
        # if the store has any torrents at all.
        empty_html = ""
        if store.torrents:
            empty_html = render.render_groups_payload(
                request, store, fs, visible=[]
            )
        payload = (
            f'<div id="groups" hx-swap-oob="innerHTML">{empty_html}</div>'
            f'<div id="qf-batch-staging" hx-swap-oob="outerHTML" '
            f'data-final="1" data-canonical="" data-loaded="0" '
            f'data-total="0" hidden></div>'
            f'<div id="qf-load-progress" hx-swap-oob="innerHTML"></div>'
            + chrome_oobs
        )
        yield _esc_for_sse(payload)
        return

    canonical_slugs = "|".join(g.key.slug() for g in visible)
    header = (
        f"Connected -- {total} groups incoming"
        if is_final
        else f"Connected -- {total} groups so far"
    )
    log_lines: list[str] = [header]

    idx = 0
    batch_n = 0
    while idx < total:
        size = (
            _RESYNC_BATCH_SIZES[batch_n]
            if batch_n < len(_RESYNC_BATCH_SIZES)
            else _RESYNC_BATCH_SIZES[-1]
        )
        end = min(idx + size, total)
        is_first = idx == 0
        is_last_internal = end == total
        # Only the last batch of the FINAL RESYNC carries the canonical flag
        # + slugs. Cold-boot RESYNC_PARTIALs always emit data-final="0" so
        # the client doesn't prune cards that are about to arrive.
        carries_final_flag = is_last_internal and is_final

        cards = "".join(
            render.render_group(
                request,
                g,
                torrents_for_group(store, g.key, fs),
                store=store,
                rule_marks=rule_marks.get(g.key, {}),
                rule_keeper=rule_keepers.get(g.key, ""),
                rule_factors=rule_factors.get(g.key, {}),
                rule_severity=rule_severity.get(g.key, {}),
            )
            for g in visible[idx:end]
        )

        staging = (
            f'<div id="qf-batch-staging" hx-swap-oob="outerHTML" hidden '
            f'data-loaded="{end}" data-total="{total}" '
            f'data-final="{"1" if carries_final_flag else "0"}" '
            f'data-canonical="{canonical_slugs if carries_final_flag else ""}">'
            f'{cards}'
            f'</div>'
        )

        log_lines.append(f"Loaded {end} of {total} groups")

        if carries_final_flag:
            # Empty inner content -> CSS :empty hides the block.
            progress = (
                '<div id="qf-load-progress" hx-swap-oob="innerHTML"></div>'
            )
        else:
            # Within-emission percentage. For partial RESYNCs the
            # eventual total is unknown, so cap the visual at 95 % so
            # the bar doesn't briefly read "done" between chunks.
            raw_pct = int(100 * end / total) if total else 0
            pct = raw_pct if is_final else min(95, raw_pct)
            label = (
                f"Loading torrent list -- {end} of {total} groups"
                if is_final
                else f"Loading torrent list -- {end} groups so far"
            )
            log_html = "".join(
                f"<li>{line}</li>" for line in log_lines
            )
            progress = (
                f'<div id="qf-load-progress" hx-swap-oob="innerHTML">'
                f'<div class="fl-card" role="status" aria-live="polite">'
                f'<div class="fl-title">{label}</div>'
                f'<div class="fl-bar">'
                f'<div class="fl-bar-fill" style="width:{pct}%"></div>'
                f'</div>'
                f'<ol class="fl-log">{log_html}</ol>'
                f'</div>'
                f'</div>'
            )

        parts = [staging, progress]
        if is_first:
            parts.append(chrome_oobs)

        yield _esc_for_sse("".join(parts))

        idx = end
        batch_n += 1


def _render_delta_events(
    request: Request,
    store: Store,
    sub: Subscription,
    events: list[DomainEvent],
) -> str:
    """Serialise one tick's worth of NON-RESYNC events into a single SSE
    ``data:`` line.

    Dedupes per target: each affected group/torrent renders at most once. A
    group re-render shadows its torrent updates (whole-card swap includes the
    rows), so those are dropped. ``hx-swap-oob`` lets a single ``message``
    event update many DOM nodes.
    """
    fs = sub.filter_state

    # Rule preview context. When a rule is active, any row update inside a
    # rule-flagged group is escalated to a whole-card swap so the
    # compare-strip structure is preserved (a per-row outerHTML targeting
    # ``#torrent-{hash}`` would either land inside the compare-strip --
    # corrupting its layout -- or silently no-op for the keeper row, which
    # has no per-row id).
    rule_marks, rule_keepers, rule_factors, rule_severity, _ = (
        _rule_preview_context(store, sub)
    )

    # Dedupe by target. dicts keep insertion order, which we use as render order.
    added_groups: dict[GroupKey, None] = {}
    removed_groups: dict[GroupKey, None] = {}
    added_torrents: dict[str, GroupKey] = {}
    changed_torrents: dict[str, GroupKey] = {}
    removed_torrents: dict[str, GroupKey] = {}
    # Rule-preview escalations: re-render the whole card for these group
    # keys at the end of this delta batch.
    rule_card_rerenders: dict[GroupKey, None] = {}

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

    # Escalate any row update inside a rule-flagged group to a whole-card
    # re-render so the compare-strip layout survives the swap. Strips the
    # row from the per-row maps because the card render includes them.
    if rule_marks:
        rule_groups = set(rule_marks.keys())
        for h in [h for h, gk in changed_torrents.items() if gk in rule_groups]:
            rule_card_rerenders[changed_torrents[h]] = None
            del changed_torrents[h]
        for h in [h for h, gk in added_torrents.items() if gk in rule_groups]:
            rule_card_rerenders[added_torrents[h]] = None
            del added_torrents[h]
        for h in [h for h, gk in removed_torrents.items() if gk in rule_groups]:
            rule_card_rerenders[removed_torrents[h]] = None
            del removed_torrents[h]

    # A whole-group render already includes its torrent rows.
    shadowed_groups = set(added_groups) | set(removed_groups) | set(rule_card_rerenders)
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

    # Filtered torrent lists per group, memoised for the batch. The same
    # group can be touched by a count chip, an added-group render, and an
    # added-torrent insert in a single tick; without this each call
    # re-filters the same hashes.
    torrents_lookup: dict[GroupKey, list[Torrent]] = {}

    def _torrents(gk: GroupKey) -> list[Torrent]:
        out = torrents_lookup.get(gk)
        if out is None:
            out = torrents_for_group(store, gk, fs)
            torrents_lookup[gk] = out
        return out

    # Group keys whose count chip we've already emitted in this batch -- we
    # only need one count update per group even if N rows came and went.
    counted: set[GroupKey] = set()

    def _count_oob(gk: GroupKey) -> str:
        group = store.groups.get(gk)
        if group is None or gk in counted:
            return ""
        counted.add(gk)
        # Count torrents that survive the active filter so the chip matches
        # the rendered row count (group-level visibility allows a group with
        # one matching + one filtered-out torrent; the chip must say "1 item",
        # not "2 items").
        n = len(_torrents(gk))
        label = "1 item" if n == 1 else f"{n} items"
        slug = gk.slug()
        return (
            f'<span id="group-{slug}-count" '
            f'hx-swap-oob="outerHTML">{label}</span>'
        )

    parts: list[str] = []

    # Rule-preview card re-renders: swap the whole card outerHTML so the
    # compare-strip structure stays consistent. Emitted before per-row
    # OOBs in case a later op references the same card.
    for gk in rule_card_rerenders:
        if not _visible(gk):
            continue
        group = store.groups.get(gk)
        if group is None:
            continue
        slug = gk.slug()
        card_html = render.render_group(
            request,
            group,
            _torrents(gk),
            store=store,
            rule_marks=rule_marks.get(gk, {}),
            rule_keeper=rule_keepers.get(gk, ""),
            rule_factors=rule_factors.get(gk, {}),
            rule_severity=rule_severity.get(gk, {}),
        )
        parts.append(card_html.replace(
            f'id="group-{slug}"',
            f'id="group-{slug}" hx-swap-oob="outerHTML"',
            1,
        ))

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
        torrents = _torrents(gk)
        card_html = render.render_group(
            request,
            group,
            torrents,
            store=store,
            rule_marks=rule_marks.get(gk, {}),
            rule_keeper=rule_keepers.get(gk, ""),
            rule_factors=rule_factors.get(gk, {}),
            rule_severity=rule_severity.get(gk, {}),
        )
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
        if not torrent_matches(t, fs, store):
            # Was visible (or might have been); the change (category/tag/etc.)
            # makes it fail the filter now. Remove from DOM -- delete OOB is a
            # no-op if the row was already hidden.
            parts.append(
                f'<div id="torrent-{t.hash}" hx-swap-oob="delete"></div>'
            )
            parts.append(_count_oob(gk))
            continue
        # Non-rule-flagged groups: render with empty rule context. (Rule-
        # flagged groups were promoted to whole-card re-renders above.)
        row = render.render_torrent(request, t, store=store)
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
        if not torrent_matches(t, fs, store):
            # New torrent in a visible group, but the user's filter excludes
            # it -- e.g. a cross-seed copy showing up while ``not_tags`` has
            # ``cross-seed``. Don't render the row.
            continue
        slug = gk.slug()
        row = render.render_torrent(request, t, store=store)
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
