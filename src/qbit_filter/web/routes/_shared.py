"""Shared helpers, constants, and the render pool used across the route
modules. Imports nothing from sibling route modules so it stays a leaf and
can't form an import cycle.
"""

from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import HTTPException, Request, Response

from qbit_filter.domain import FilterState
from qbit_filter.state.store import Store
from qbit_filter.state.subscribers import Subscription

SID_COOKIE = "qf_sid"

# Worker pool for streaming the initial page. Jinja2's renderer is a thin
# wrapper over C-extension code which releases the GIL during template
# bytecode execution, so a small pool gives a measurable wall-clock win for
# 100+ groups while keeping memory bounded. Module-level so it's shared
# across requests rather than re-created per request.
_RENDER_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qf-render")

# Cap the number of hashes accepted in a single bulk request. Each call
# hits qBit's REST API which has its own URL-length limits; 500 leaves
# plenty of headroom for the typical "select all visible" use case.
_BULK_MAX = 500

_HEX_CHARS = frozenset("0123456789abcdef")


def get_or_create_subscription(
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


def get_client(request: Request) -> Any:
    """Return the live qBittorrent client or raise 503 if the poller hasn't
    connected yet."""
    client = getattr(request.app.state, "qbit", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="qBittorrent connection not available",
        )
    return client


def parse_bulk(raw: str, *, store: Store | None = None) -> list[str]:
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
