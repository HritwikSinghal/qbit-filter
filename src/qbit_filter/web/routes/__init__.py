"""FastAPI route package.

``routes.py`` grew to 1600+ lines spanning page render, SSE protocol, filters,
rule preview, actions, the activity widget, and the arr history dialog. It is
now split into cohesive modules, each owning one :class:`fastapi.APIRouter`.
``register_routes(app)`` keeps the same public entry point ``app.py`` calls, so
the split is transparent to the app factory.

Router include order is mostly free (the modules have disjoint path prefixes);
the one ordering constraint -- bulk ``/torrents/bulk/...`` routes must precede
the parameterised ``/torrents/{torrent_hash}`` routes -- is satisfied within
``actions.py`` itself, where they are declared in that order.
"""

from __future__ import annotations

from fastapi import FastAPI

from qbit_filter.web.routes import (
    actions,
    arr_history,
    filters,
    page,
    rules_preview,
    sse,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(page.router)
    app.include_router(rules_preview.router)
    app.include_router(actions.router)
    app.include_router(sse.router)
    app.include_router(filters.router)
    app.include_router(arr_history.router)
