"""Server-Sent Events: the ``/sse`` stream plus the batch/delta protocol that
turns a tick's domain events into incremental OOB swaps for the browser."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import StreamingResponse

from qbit_filter.domain import DomainEvent, EventKind, GroupKey, Torrent
from qbit_filter.state.store import Store
from qbit_filter.state.subscribers import Subscription
from qbit_filter.state.views import (
    apply_filters,
    group_matches,
    torrent_matches,
    torrents_for_group,
)
from qbit_filter.web import render
from qbit_filter.web.routes._shared import get_or_create_subscription
from qbit_filter.web.routes.activity import render_activity_oob
from qbit_filter.web.routes.rules_preview import render_rule_bar, rule_preview_context

logger = logging.getLogger(__name__)

router = APIRouter()

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

# Adaptive batch sizes for a RESYNC delivered over SSE. The first batch is
# intentionally tiny so the user sees content within ~1 frame; later batches
# expand so total wall-clock isn't dominated by per-batch framing overhead.
# After the last entry, the final size is reused for any remaining groups.
_RESYNC_BATCH_SIZES = (10, 25, 50, 100, 100, 100)


@router.get("/sse")
async def sse(
    request: Request,
    qf_sid: str | None = Cookie(default=None),
) -> StreamingResponse:
    store: Store = request.app.state.store
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    bus = request.app.state.bus

    async def stream() -> Any:
        # Register on the bus only while a stream is live. Refcount so
        # multi-tab (which shares one Subscription per sid) doesn't
        # remove the bus entry when one tab closes.
        bus.add(sub)
        sub.sse_refs += 1
        logger.info(
            "sse open: sid=%s refs=%d total_subs=%d cold_boot_done=%s",
            getattr(sub, "sid", "?"),
            sub.sse_refs,
            len(bus),
            store.cold_boot_done,
        )
        # Push an immediate RESYNC into this client's queue so the very
        # first SSE message delivers the live snapshot. Without this, a
        # client connecting to a quiet qBit instance would stare at the
        # "Loading torrent list..." placeholder until the next reconciler
        # tick, which can be minutes apart. Coalesced via
        # ``last_resync_at`` so back-to-back connects don't double-send.
        #
        # During a cold-boot the store only holds the first N chunks; a
        # full RESYNC here would render with ``data-final=1``, telling
        # the client to prune any card not in that partial canonical
        # list. Send RESYNC_PARTIAL until cold-boot finishes so the
        # streaming path correctly treats this as "more to come".
        initial_kind = (
            EventKind.RESYNC
            if store.cold_boot_done
            else EventKind.RESYNC_PARTIAL
        )
        sub.notify(DomainEvent(kind=initial_kind))
        try:
            yield ": connected\n\n"
            while True:
                # No explicit ``request.is_disconnected()`` peek: when the
                # client closes, Starlette cancels this generator and the
                # awaiting ``queue.get()`` / ``yield`` raises
                # ``CancelledError`` (or ``ClientDisconnect``), which the
                # ``finally:`` below already handles via refcount + bus
                # removal. A stale client that hasn't fully RST'd is
                # detected on the next ping write or within
                # ``SSE_PING_INTERVAL`` -- acceptable HTTP-keepalive
                # latency.
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
            logger.info(
                "sse close: sid=%s refs=%d total_subs=%d",
                getattr(sub, "sid", "?"),
                sub.sse_refs,
                len(bus),
            )
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
    # Empty-store RESYNC: ship only the activity widget update so log lines
    # added by arr_fetch_loop (which runs before qBit responds) reach the
    # user, without wiping #groups -- the server-side chrome already left
    # it as the loading affordance. The reconciler's first chunk publishes
    # a fresh RESYNC_PARTIAL with non-empty store, which takes the normal
    # batched path below.
    if (has_resync or has_partial) and not store.torrents:
        yield _esc_for_sse(render_activity_oob(store))
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
    - ``#qf-activity`` (OOB outerHTML, first batch only) carrying the
      header activity widget update -- bar pct, status label, log lines.
      The state machine lives in :func:`render_activity_oob`.
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
        rule_preview_context(store, sub)
    )

    active_html = render.render_active_filters(request, store, fs, visible=visible)
    facets_html = render.render_filter_facets(request, store, fs)
    rule_bar_html = render_rule_bar(request, store, fs, sub.active_rule_slug)
    chrome_oobs = (
        f'<div id="active-filters" hx-swap-oob="outerHTML" '
        f'aria-live="polite">{active_html}</div>'
        f'<div id="filter-facets" hx-swap-oob="outerHTML">{facets_html}</div>'
        f'<div id="rule-bar-slot" hx-swap-oob="innerHTML">{rule_bar_html}</div>'
    )

    activity_oob = render_activity_oob(store)

    if total == 0:
        if not is_final:
            # Partial RESYNC fired before any visible groups exist. Skip --
            # the next partial / final will deliver content. Keep the
            # header activity widget visible (the user can still peek
            # into the dropdown to see what's happening).
            return
        # Filter excludes everything (or store empty). Wipe #groups and
        # let the empty-state template render if the store has any
        # torrents at all. The activity widget is updated either way.
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
            + activity_oob
            + chrome_oobs
        )
        yield _esc_for_sse(payload)
        return

    canonical_slugs = "|".join(g.key.slug() for g in visible)

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
                rule_keepers=rule_keepers.get(g.key, frozenset()),
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
        logger.debug(
            "sse batch %d: loaded=%d/%d final=%s cards=%d",
            batch_n,
            end,
            total,
            carries_final_flag,
            end - idx,
        )

        parts = [staging]
        # Activity widget + chrome only on the first batch of a partial.
        # The store fields ``activity_oob`` reads from don't change while
        # we're inside one _emit_resync_batches call, so re-emitting per
        # batch would just churn bytes for the same payload.
        if is_first:
            parts.append(activity_oob)
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
        rule_preview_context(store, sub)
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
            rule_keepers=rule_keepers.get(gk, frozenset()),
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
            rule_keepers=rule_keepers.get(gk, frozenset()),
            rule_factors=rule_factors.get(gk, {}),
            rule_severity=rule_severity.get(gk, {}),
        )
        # New cards go in at the END of #groups so existing cards keep
        # their vertical position -- inserting at top (``afterbegin``) made
        # every visible card shift down on each delta tick, which read as
        # the page "jumping". Canonical sort order is restored on the
        # next RESYNC; for in-session adds the new card joins the bottom
        # with the qf-enter animation so the user notices it.
        card_html = card_html.replace(
            'class="group-card"', 'class="group-card qf-enter"', 1
        )
        parts.append(card_html.replace(
            f'id="group-{slug}"',
            f'id="group-{slug}" hx-swap-oob="beforeend:#groups"',
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
