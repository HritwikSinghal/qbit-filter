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
from qbit_filter.arr.models import ArrSnapshot
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
    """Background task: every ``arr_poll_interval_seconds`` pull a snapshot
    from Radarr / Sonarr and rebuild the ``hash_to_arr`` index. Additionally,
    when qBit publishes RESYNC / RESYNC_PARTIAL, re-run ``build_index``
    against the most recent arr snapshot so newly-arrived torrents pick up
    their arr metadata without waiting for the 60 s arr-poll cadence.

    Degrades gracefully:
    - No arr configured -> task ends, app keeps working.
    - One arr down -> the other still populates its half of the index.
    - Both down briefly -> empty index for this tick; retry next tick.
    """
    if not arr_store.configured:
        logger.info("no arr instance configured; arr poller exiting")
        return

    last_snapshot: ArrSnapshot | None = None
    qbit_resync_event = asyncio.Event()

    class _QbitListener:
        """Subscriber on the qBit/arr bus that wakes the rebuild loop on
        qBit-side RESYNCs. Ignores the arr-side RESYNCs we publish below so
        we don't ping-pong."""

        def notify(self, event: DomainEvent) -> None:
            if event.kind in (EventKind.RESYNC, EventKind.RESYNC_PARTIAL):
                qbit_resync_event.set()

    listener = _QbitListener()
    bus.add(listener)

    def _apply_snapshot(snapshot: ArrSnapshot) -> None:
        nonlocal last_snapshot
        last_snapshot = snapshot
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
        arr_store.radarr_ok = (
            bool(snapshot.movies)
            or bool(snapshot.radarr_queue)
            or bool(snapshot.radarr_history)
        )
        arr_store.sonarr_ok = (
            bool(snapshot.series)
            or bool(snapshot.sonarr_queue)
            or bool(snapshot.sonarr_history)
        )

    def _rebuild_index_and_publish() -> int:
        """Re-run build_index against the cached arr snapshot + current
        store.torrents, atomically swap ``hash_to_arr``, bump rid, and
        publish a RESYNC. Temporarily removes our own listener from the bus
        so we don't recurse on the RESYNC we just published. Returns the
        match count for logging."""
        if last_snapshot is None:
            return 0
        new_index = build_index(
            last_snapshot,
            store.torrents.values(),
            title_fallback=settings.arr_title_fallback,
        )
        # Atomic dict-reference swap so concurrent readers (template render
        # under SSE) see either the old dict or the new one whole -- never
        # a half-mutated state.
        arr_store.hash_to_arr = new_index
        arr_store.rid += 1
        bus.remove(listener)
        try:
            bus.publish(DomainEvent(kind=EventKind.RESYNC))
        except Exception:
            logger.exception("arr poller: bus.publish raised")
        finally:
            bus.add(listener)
        return len(new_index)

    async def arr_fetch_loop() -> None:
        async for snapshot in poll_arr(settings):
            _apply_snapshot(snapshot)
            qbit_rid = store.rid
            matches = _rebuild_index_and_publish()
            logger.info(
                "arr fetched: %d movies, %d series, %d matches (qbit_rid=%d)",
                len(arr_store.movies_by_id),
                len(arr_store.series_by_id),
                matches,
                qbit_rid,
            )

    async def qbit_reindex_loop() -> None:
        """Wake on qBit RESYNC, debounce, re-run build_index against the
        cached arr snapshot. Cheap (no network) so safe to fire after every
        cold-boot chunk; the debounce coalesces a burst of chunks into one
        rebuild."""
        while True:
            await qbit_resync_event.wait()
            qbit_resync_event.clear()
            await asyncio.sleep(0.25)
            # Second clear absorbs any events that landed during the debounce.
            qbit_resync_event.clear()
            if last_snapshot is None:
                # Arr fetch hasn't completed yet -- nothing to index against.
                # When it does, arr_fetch_loop will publish on our behalf.
                continue
            matches = _rebuild_index_and_publish()
            logger.debug(
                "arr index re-built from qBit RESYNC: %d matches", matches
            )

    try:
        await asyncio.gather(arr_fetch_loop(), qbit_reindex_loop())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("arr poller raised; task exiting")
    finally:
        bus.remove(listener)


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
