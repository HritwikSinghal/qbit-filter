"""FastAPI app factory with background-poller lifespan."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from qbit_filter.arr.index import build_index
from qbit_filter.arr.sync import poll_arr
from qbit_filter.config import Settings
from qbit_filter.domain import DomainEvent, EventKind
from qbit_filter.grouping import parser as grouping_parser
from qbit_filter.qbit.client import connect
from qbit_filter.qbit.sync import poll
from qbit_filter.state.arr_store import ArrStore
from qbit_filter.state.events import EventBus
from qbit_filter.state.reconciler import Reconciler
from qbit_filter.state.store import Store
from qbit_filter.web.routes import register_routes

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


async def _poller(
    app: FastAPI,
    reconciler: Reconciler,
    settings: Settings,
) -> None:
    try:
        client = await asyncio.to_thread(connect, settings)
    except Exception:
        logger.exception("could not connect to qBittorrent; poller exiting")
        return
    app.state.qbit = client
    async for delta in poll(client, settings):
        try:
            await reconciler.apply(delta)
        except Exception:
            logger.exception("reconciler.apply raised")


async def _arr_poller(
    app: FastAPI,
    store: Store,
    arr_store: ArrStore,
    bus: EventBus,
    settings: Settings,
) -> None:
    """Background task: every ``arr_poll_interval_seconds`` (or whenever
    qBit store.rid changes between *arr polls), pull a snapshot from Radarr /
    Sonarr and rebuild the ``hash_to_arr`` index. RESYNCs SSE clients after
    every successful rebuild so posters and badges repaint.

    Degrades gracefully:
    - No arr configured -> generator exits on its first ``ok=False`` yield;
      task ends, app keeps working.
    - One arr down -> the other still populates its half of the index.
    - Both down briefly -> empty index for this tick; retry next tick.
    """
    last_qbit_rid = -1
    if not arr_store.configured:
        logger.info("no arr instance configured; arr poller exiting")
        return
    try:
        async for snapshot in poll_arr(settings):
            arr_store.movies_by_id = {m.id: m for m in snapshot.movies}
            arr_store.series_by_id = {s.id: s for s in snapshot.series}
            arr_store.tmdb_to_movie = {
                m.tmdb_id: m for m in snapshot.movies if m.tmdb_id
            }
            arr_store.tvdb_to_series = {
                s.tvdb_id: s for s in snapshot.series if s.tvdb_id
            }
            arr_store.quality_profiles = {
                **snapshot.quality_profiles_radarr,
                **snapshot.quality_profiles_sonarr,
            }
            arr_store.radarr_ok = bool(snapshot.movies) or bool(
                snapshot.radarr_queue
            ) or bool(snapshot.radarr_history)
            arr_store.sonarr_ok = bool(snapshot.series) or bool(
                snapshot.sonarr_queue
            ) or bool(snapshot.sonarr_history)
            # Rebuild the hash index against the latest qBit snapshot. Using
            # the qBit store.rid as the consistency anchor: if it changed
            # mid-poll, the index reflects the latest qBit state.
            last_qbit_rid = store.rid
            arr_store.hash_to_arr = build_index(
                snapshot,
                store.torrents.values(),
                title_fallback=settings.arr_title_fallback,
            )
            arr_store.rid += 1
            # SSE clients should refresh so posters + badges appear without
            # waiting for the next qBit-side change. The handler coalesces
            # via ``last_resync_at`` so back-to-back arr+qBit RESYNCs don't
            # double-render.
            try:
                bus.publish(DomainEvent(kind=EventKind.RESYNC))
            except Exception:
                logger.exception("arr poller: bus.publish raised")
            logger.info(
                "arr index rebuilt: %d movies, %d series, %d matches (qbit_rid=%d)",
                len(arr_store.movies_by_id),
                len(arr_store.series_by_id),
                len(arr_store.hash_to_arr),
                last_qbit_rid,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("arr poller raised; task exiting")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    store = Store()
    bus = EventBus()
    reconciler = Reconciler(store=store, bus=bus, settings=settings)
    arr_store = ArrStore(
        radarr_url=settings.radarr_url.rstrip("/"),
        sonarr_url=settings.sonarr_url.rstrip("/"),
    )
    store.arr = arr_store
    app.state.store = store
    app.state.bus = bus
    app.state.reconciler = reconciler
    app.state.arr_store = arr_store
    app.state.subscriptions = {}
    app.state.qbit = None

    task = asyncio.create_task(_poller(app, reconciler, settings))
    arr_task: asyncio.Task[None] | None = None
    if arr_store.configured:
        arr_task = asyncio.create_task(
            _arr_poller(app, store, arr_store, bus, settings)
        )
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if arr_task is not None:
            arr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await arr_task
        client = app.state.qbit
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.auth_log_out)
        # Persist the parser cache so the next boot can skip guessit for
        # every name still in qBit. Best-effort -- a save failure is logged
        # by the cache module and doesn't block shutdown.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(grouping_parser.dump_to_disk)


def create_app() -> FastAPI:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(lifespan=_lifespan)
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # uvicorn --reload restarts the whole worker on template change, so Jinja's
    # per-render mtime probe is pure overhead (~10% of render CPU on busy SSE).
    app.state.templates.env.auto_reload = False
    # Per-process boot id; the dev livereload route exposes this so the browser
    # can detect a uvicorn `--reload` restart and refresh.
    app.state.boot_id = str(time.monotonic_ns())
    app.state.templates.env.globals["dev_mode"] = settings.dev_mode
    app.state.templates.env.filters["fmt_date"] = _fmt_date
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    register_routes(app)
    return app


def _fmt_date(ts: int | float | None) -> str:
    """Return a DD-MM-YYYY date for a unix timestamp; empty string if missing."""
    if not ts:
        return ""
    try:
        # Use local time -- the user is browsing locally and expects their
        # zone's calendar day. ``time.localtime`` honours TZ env / system.
        t = time.localtime(int(ts))
    except (TypeError, ValueError, OSError):
        return ""
    return time.strftime("%d-%m-%Y", t)
