"""Header activity-widget rendering: the qBit / Radarr / Sonarr service cards
and the cold-boot progress headline, emitted as OOB swaps over SSE. Pure
HTML-string builders over :class:`Store` + its telemetry handle -- no routes.
"""

from __future__ import annotations

import html as html_lib
import time

from qbit_filter.state.store import Store

# Maximum number of activity-log lines rendered into the background-activity
# dialog. Mirrors the trim cap in ``app._append_activity``.
_ACTIVITY_LOG_MAX = 16

# Seconds-since-last-fetch thresholds that flip a service card from "ok"
# (live data) to "stale" (data on hand, but not refreshed recently). The
# ``down`` state is independent and is keyed off the last fetch raising.
_QBIT_STALE_AFTER = 15.0
_ARR_STALE_AFTER = 180.0


def render_activity_oob(store: Store) -> str:
    """Render the header activity widget as a single OOB swap.

    Always emits an outerHTML swap of ``#qf-activity`` so the widget's
    ``data-state`` (idle / active / done / stalled), button label,
    percentage, bar fill, services grid, and log can all update from one
    payload. The open/close state of the dropdown panel is owned
    client-side (``data-open`` on the same element, repainted by the
    keys.js click handler after each swap) so the swap doesn't fight a
    user who has the panel expanded.

    Three signal layers stack inside the panel:

    1. *Headline* -- one summary line + a progress bar. During cold-boot
       the bar shows torrent parse progress; after cold-boot it collapses
       to a thin "Live" tick.
    2. *Services* -- a small grid of cards (qBit, Radarr, Sonarr) each
       carrying state, counts, and a ``data-ts`` timestamp so the client
       can render "N s ago" relative to the browser clock.
    3. *Activity log* -- the rolling tail of human-readable events the
       pollers append, capped at :data:`_ACTIVITY_LOG_MAX` lines.
    """
    total = store.cold_boot_total
    done = store.cold_boot_processed
    tel = store.telemetry

    arr = store.arr
    arr_configured = arr is not None and arr.configured
    radarr_configured = arr is not None and bool(arr.radarr_url)
    sonarr_configured = arr is not None and bool(arr.sonarr_url)

    qbit_card = _render_qbit_service_card(store)
    radarr_card = (
        _render_arr_service_card(
            name="Radarr",
            ok=arr.radarr_ok if arr else False,
            last_fetch_at=arr.radarr_last_fetch_at if arr else 0.0,
            last_err=arr.radarr_last_err if arr else "",
            library_count=len(arr.movies_by_id) if arr else 0,
            library_label="movies",
            queue_count=arr.radarr_queue_count if arr else 0,
            match_count=arr.radarr_match_count if arr else 0,
            url=arr.radarr_url if arr else "",
            fetch_cycles=arr.arr_fetch_cycles if arr else 0,
        )
        if radarr_configured
        else ""
    )
    sonarr_card = (
        _render_arr_service_card(
            name="Sonarr",
            ok=arr.sonarr_ok if arr else False,
            last_fetch_at=arr.sonarr_last_fetch_at if arr else 0.0,
            last_err=arr.sonarr_last_err if arr else "",
            library_count=len(arr.series_by_id) if arr else 0,
            library_label="series",
            queue_count=arr.sonarr_queue_count if arr else 0,
            match_count=arr.sonarr_match_count if arr else 0,
            url=arr.sonarr_url if arr else "",
            fetch_cycles=arr.arr_fetch_cycles if arr else 0,
        )
        if sonarr_configured
        else ""
    )
    services_html = qbit_card + radarr_card + sonarr_card

    # Overall widget state. qBit failure dominates ("system isn't working");
    # cold-boot is active; arr-only failure stays "done" with a degraded
    # badge inside the card so the headline doesn't scream red.
    arr_degraded = arr_configured and (
        (radarr_configured and arr is not None and not arr.radarr_ok)
        or (sonarr_configured and arr is not None and not arr.sonarr_ok)
    )

    if not tel.qbit_connected and tel.qbit_last_error:
        state = "stalled"
        pct = 100
        label = "qBittorrent unreachable"
        pct_text = "X"
        summary = (
            f"qBittorrent at {tel.qbit_host} is unreachable -- "
            f"{tel.qbit_last_error}"
        )
        bar_visible = False
    elif not store.cold_boot_done and total <= 0:
        # Pre-chunk: qBit hasn't responded yet (or the reconciler hasn't
        # stamped a total). Show a low indeterminate-ish bar so the user
        # has feedback that the system is reaching out -- not "Idle".
        state = "active"
        pct = 5
        label = "Contacting qBittorrent"
        pct_text = ""
        summary = "Waiting for first response from qBittorrent"
        bar_visible = True
    elif not store.cold_boot_done:
        state = "active"
        pct = min(99, int(100 * done / total)) if total else 0
        label = f"Loading {pct}%"
        pct_text = f"{pct}%"
        summary = f"Parsing torrents -- {done} of {total}"
        bar_visible = True
    else:
        state = "done"
        pct = 100
        if arr_degraded:
            label = "Live (arr degraded)"
            pct_text = "!"
        else:
            label = "Live"
            pct_text = "OK"
        summary = (
            f"{len(store.torrents)} torrents in {len(store.groups)} groups -- "
            f"streaming live updates"
        )
        bar_visible = False

    log_html = "".join(
        f"<li>{html_lib.escape(line)}</li>"
        for line in store.cold_boot_log[-_ACTIVITY_LOG_MAX:]
    )
    if not log_html:
        log_html = '<li class="qf-activity-log-empty">No recent activity</li>'

    bar_html = (
        '<div class="qf-activity-bar">'
        f'<div class="qf-activity-bar-fill" style="width:{pct}%"></div>'
        '</div>'
        if bar_visible
        else ""
    )

    return (
        f'<div id="qf-activity" class="qf-activity" '
        f'data-state="{state}" data-open="false" '
        f'hx-swap-oob="outerHTML">'
        f'<button id="qf-activity-btn" type="button" '
        f'class="qf-activity-btn" aria-haspopup="dialog" '
        f'aria-expanded="false" aria-controls="qf-activity-panel" '
        f'title="{html_lib.escape(summary)}">'
        f'<span class="qf-activity-pulse" aria-hidden="true"></span>'
        f'<span class="qf-activity-button-label">{html_lib.escape(label)}</span>'
        f'</button>'
        f'<div id="qf-activity-panel" class="qf-activity-panel" '
        f'role="dialog" aria-label="Background activity" aria-hidden="true">'
        f'<div class="qf-activity-head">'
        f'<span class="qf-activity-title">Background activity</span>'
        f'<span class="qf-activity-pct">{html_lib.escape(pct_text)}</span>'
        f'</div>'
        f'<div class="qf-activity-summary">{html_lib.escape(summary)}</div>'
        f'{bar_html}'
        f'<div class="qf-activity-services">{services_html}</div>'
        f'<ol class="qf-activity-log">{log_html}</ol>'
        f'</div>'
        f'</div>'
    )


def _render_qbit_service_card(store: Store) -> str:
    """Service card for the qBit poll loop.

    Three terminal states:
    - ``connecting``: app just started, no successful poll yet
    - ``ok``: recent successful poll within :data:`_QBIT_STALE_AFTER`
    - ``stalled``: connected but no recent poll (or never connected)
    """
    tel = store.telemetry
    if not tel.qbit_connected:
        if tel.qbit_last_error:
            state = "down"
            detail = (
                f"<span class=\"qf-service-err\">"
                f"{html_lib.escape(tel.qbit_last_error)}</span>"
            )
        else:
            state = "connecting"
            detail = "Authenticating..."
        return _service_card_html(
            slug="qbit",
            name="qBittorrent",
            state=state,
            primary=detail,
            secondary=html_lib.escape(tel.qbit_host or ""),
            ts=0.0,
            ts_prefix="",
        )
    if tel.qbit_poll_count == 0 or tel.qbit_last_poll_at == 0:
        # Connected but the first poll hasn't returned yet -- carry the
        # auth-bypass milestone into the card so the user sees forward
        # motion even before sync/maindata responds.
        return _service_card_html(
            slug="qbit",
            name="qBittorrent",
            state="connecting",
            primary="Waiting for sync/maindata...",
            secondary=html_lib.escape(tel.qbit_host or ""),
            ts=0.0,
            ts_prefix="",
        )
    fresh = (time.time() - tel.qbit_last_poll_at) <= _QBIT_STALE_AFTER
    state = "ok" if fresh else "stale"
    primary = (
        f"<strong>{len(store.torrents)}</strong> torrents "
        f"&middot; <strong>{len(store.groups)}</strong> groups"
    )
    secondary = (
        f"{tel.qbit_poll_count} polls"
        + (f" &middot; {html_lib.escape(tel.qbit_host)}" if tel.qbit_host else "")
    )
    return _service_card_html(
        slug="qbit",
        name="qBittorrent",
        state=state,
        primary=primary,
        secondary=secondary,
        ts=tel.qbit_last_poll_at,
        ts_prefix="last poll",
    )


def _render_arr_service_card(
    *,
    name: str,
    ok: bool,
    last_fetch_at: float,
    last_err: str,
    library_count: int,
    library_label: str,
    queue_count: int,
    match_count: int,
    url: str,
    fetch_cycles: int,
) -> str:
    """Service card for a single arr instance (Radarr or Sonarr).

    State precedence: ``down`` (last fetch raised) beats ``stale`` (last
    fetch succeeded but is older than :data:`_ARR_STALE_AFTER`) beats
    ``ok`` (recent successful fetch). ``connecting`` covers the gap
    between task spin-up and the first fetch returning.
    """
    if fetch_cycles == 0 and last_fetch_at == 0:
        state = "connecting"
        primary = "Fetching library..."
        secondary = html_lib.escape(url) if url else ""
        ts = 0.0
    elif not ok:
        state = "down"
        primary = (
            f"<span class=\"qf-service-err\">"
            f"{html_lib.escape(last_err or 'unreachable')}</span>"
        )
        secondary = (
            "last good fetch shown above"
            if last_fetch_at > 0
            else (html_lib.escape(url) if url else "")
        )
        ts = last_fetch_at
    else:
        fresh = (time.time() - last_fetch_at) <= _ARR_STALE_AFTER if last_fetch_at else False
        state = "ok" if fresh else "stale"
        primary = (
            f"<strong>{library_count}</strong> {library_label} "
            f"&middot; <strong>{queue_count}</strong> queued"
        )
        secondary = (
            f"<strong>{match_count}</strong> linked torrents"
            if match_count
            else "no linked torrents yet"
        )
        ts = last_fetch_at
    return _service_card_html(
        slug=name.lower(),
        name=name,
        state=state,
        primary=primary,
        secondary=secondary,
        ts=ts,
        ts_prefix="last fetch",
    )


def _service_card_html(
    *,
    slug: str,
    name: str,
    state: str,
    primary: str,
    secondary: str,
    ts: float,
    ts_prefix: str,
) -> str:
    """Build one ``.qf-service`` card.

    ``primary`` / ``secondary`` are already-escaped HTML fragments -- the
    caller is responsible for escaping any user-supplied substrings. The
    timestamp is emitted as ``data-ts`` (unix seconds) so the client-side
    formatter can render relative time against the browser's clock; the
    server inlines a fallback string for clients without JS.
    """
    if ts > 0:
        rel_fallback = _format_relative_seconds(time.time() - ts)
        ts_html = (
            f'<span class="qf-service-ts" data-ts="{ts:.0f}" '
            f'data-prefix="{html_lib.escape(ts_prefix)}">'
            f'{html_lib.escape(ts_prefix)} {html_lib.escape(rel_fallback)}'
            f'</span>'
        )
    else:
        ts_html = '<span class="qf-service-ts" data-ts="0"></span>'
    return (
        f'<div class="qf-service" data-service="{slug}" data-state="{state}">'
        f'<div class="qf-service-head">'
        f'<span class="qf-service-dot" aria-hidden="true"></span>'
        f'<span class="qf-service-name">{html_lib.escape(name)}</span>'
        f'<span class="qf-service-state">{html_lib.escape(_state_label(state))}</span>'
        f'</div>'
        f'<div class="qf-service-primary">{primary}</div>'
        f'<div class="qf-service-meta">{secondary} {ts_html}</div>'
        f'</div>'
    )


_STATE_LABELS = {
    "ok": "Live",
    "stale": "Stale",
    "stalled": "Stalled",
    "down": "Down",
    "connecting": "Connecting",
    "idle": "Idle",
}


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state.title())


def _format_relative_seconds(delta: float) -> str:
    """Coarse human-readable elapsed string used as a no-JS fallback."""
    if delta < 0:
        return "just now"
    if delta < 2:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"
