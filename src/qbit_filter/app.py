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

from qbit_filter.arr import client as arr_client
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

# Cap on the rolling activity log surfaced in the dialog. The CSS panel
# scrolls beyond this anyway, but trimming server-side keeps the SSE
# payload small.
_ACTIVITY_LOG_MAX = 16


def _append_activity(store: Store, line: str) -> None:
    """Append one line to the shared background-activity log on ``store``.

    Trims to the most recent ``_ACTIVITY_LOG_MAX`` entries so a long-running
    process doesn't grow the in-memory log unboundedly (and so the
    rendered SSE payload stays small).
    """
    store.cold_boot_log.append(line)
    if len(store.cold_boot_log) > _ACTIVITY_LOG_MAX:
        del store.cold_boot_log[: len(store.cold_boot_log) - _ACTIVITY_LOG_MAX]


async def _poller(
    app: FastAPI,
    reconciler: Reconciler,
    settings: Settings,
) -> None:
    store: Store = app.state.store
    tel = store.telemetry
    tel.qbit_host = settings.qbittorrent_host
    _append_activity(store, f"Connecting to qBittorrent at {settings.qbittorrent_host}")
    # connect() blocks on a thread that ``asyncio.to_thread`` cannot
    # cancel; without a timeout, a hung qBit (paused container, slow DNS,
    # firewall) blocks lifespan shutdown indefinitely. 15s is comfortably
    # above a healthy login round-trip and short enough to surface a
    # hung instance during ``uvicorn --reload`` and Ctrl+C.
    try:
        client = await asyncio.wait_for(
            asyncio.to_thread(connect, settings), timeout=15.0
        )
    except TimeoutError:
        logger.error("qBit connect timed out; poller exiting")
        tel.qbit_connected = False
        tel.qbit_last_error = "connect timeout (15s)"
        _append_activity(store, "qBittorrent connect timed out (15s)")
        return
    except Exception as exc:
        logger.exception("could not connect to qBittorrent; poller exiting")
        tel.qbit_connected = False
        tel.qbit_last_error = f"connect failed: {exc}"
        _append_activity(store, f"qBittorrent connect failed: {exc}")
        return
    app.state.qbit = client
    tel.qbit_connected = True
    tel.qbit_last_error = ""
    _append_activity(store, "qBittorrent connected -- subscribing to sync/maindata")
    logger.info("qbit poller: connected, entering poll loop")
    # Track the previously-reported health so we only log the
    # healthy<->degraded edge, not every tick.
    last_failures = 0
    async for delta in poll(client, settings, store):
        logger.debug(
            "qbit poll tick: rid=%d full=%s added=%d changed=%d removed=%d failures=%d",
            delta.rid,
            delta.full_update,
            len(delta.added),
            len(delta.changed),
            len(delta.removed),
            tel.qbit_consecutive_failures,
        )
        try:
            await reconciler.apply(delta)
        except Exception as exc:
            logger.exception("reconciler.apply raised")
            tel.qbit_last_error = f"reconciler failed: {exc}"
            continue
        tel.qbit_last_poll_at = time.time()
        tel.qbit_poll_count += 1
        tel.qbit_last_error = ""
        # Healthy<->degraded transitions surface in the activity log.
        # ``poll()`` resets ``qbit_consecutive_failures`` to 0 on a
        # successful tick, so by the time we get here for a successful
        # delta, ``tel.qbit_consecutive_failures`` is 0 and
        # ``last_failures`` carries the count before this tick.
        if last_failures > 0 and tel.qbit_consecutive_failures == 0:
            _append_activity(
                store,
                f"qBittorrent recovered after {last_failures} failed poll(s)",
            )
        last_failures = tel.qbit_consecutive_failures


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
    # Tracks the last service-state we logged so steady-state successes don't
    # spam one "connected" line per minute. Keys are "radarr" / "sonarr",
    # values are the last reported state ("ok" / "down" / "").
    last_logged_state: dict[str, str] = {"radarr": "", "sonarr": ""}

    if settings.radarr_url and settings.radarr_api_key:
        _append_activity(store, f"Contacting Radarr at {settings.radarr_url}")
    if settings.sonarr_url and settings.sonarr_api_key:
        _append_activity(store, f"Contacting Sonarr at {settings.sonarr_url}")

    class _QbitListener:
        """Subscriber on the qBit/arr bus that wakes the rebuild loop on
        qBit-side RESYNCs. Ignores the arr-side RESYNCs we publish below so
        we don't ping-pong.

        Suppresses RESYNC_PARTIAL until cold-boot finishes. The reconciler
        emits one per chunk (every ~50-150 ms); reacting to each one would
        fire a full ``build_index`` + bus.publish(RESYNC) fan-out per chunk
        -- a thundering herd across SSE clients during the noisiest window
        of the app's lifetime. Waiting for the terminal RESYNC lets us
        index against the complete torrent set once and pay one fan-out.
        """

        def notify(self, event: DomainEvent) -> None:
            if event.kind == EventKind.RESYNC or (
                event.kind == EventKind.RESYNC_PARTIAL
                and store.cold_boot_done
            ):
                logger.debug(
                    "arr listener: waking on %s (cold_boot_done=%s)",
                    event.kind.name,
                    store.cold_boot_done,
                )
                qbit_resync_event.set()
            elif event.kind == EventKind.RESYNC_PARTIAL:
                logger.debug(
                    "arr listener: ignoring RESYNC_PARTIAL during cold-boot"
                )

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
        # ``radarr_ok`` / ``sonarr_ok`` now reflect "last fetch reached the
        # service" rather than "has any data". The activity dialog reads
        # both flags directly and renders the queue + last-fetch summary.
        # Untouched on the first failure-after-success so the dialog can
        # display the previously good counts with an inline "down" badge.
        now = time.time()
        if snapshot.radarr_attempted:
            arr_store.radarr_ok = snapshot.radarr_fetched
            if snapshot.radarr_fetched:
                arr_store.radarr_last_fetch_at = now
                arr_store.radarr_last_err = ""
                arr_store.radarr_queue_count = len(snapshot.radarr_queue)
                arr_store.radarr_history_count = len(snapshot.radarr_history)
            else:
                arr_store.radarr_last_err = snapshot.radarr_error
        if snapshot.sonarr_attempted:
            arr_store.sonarr_ok = snapshot.sonarr_fetched
            if snapshot.sonarr_fetched:
                arr_store.sonarr_last_fetch_at = now
                arr_store.sonarr_last_err = ""
                arr_store.sonarr_queue_count = len(snapshot.sonarr_queue)
                arr_store.sonarr_history_count = len(snapshot.sonarr_history)
            else:
                arr_store.sonarr_last_err = snapshot.sonarr_error
        arr_store.arr_fetch_cycles += 1

    def _rebuild_index_and_publish() -> int:
        """Re-run build_index against the cached arr snapshot + current
        store.torrents, atomically swap ``hash_to_arr``, bump rid, and
        publish a RESYNC. Temporarily removes our own listener from the bus
        so we don't recurse on the RESYNC we just published. Returns the
        match count for logging."""
        if last_snapshot is None:
            return 0
        # Snapshot the torrent values into a tuple before calling build_index.
        # Both pollers share the loop; ``Reconciler.apply`` yields at
        # ``await asyncio.to_thread(_warm_parse_cache, ...)`` and the chunked
        # cold-boot path clears+rebuilds ``store.torrents`` between yields.
        # Passing a live ``.values()`` view here lets build_index walk a dict
        # that the reconciler may then mutate while we're awaiting -- the
        # classic ``dictionary changed size during iteration`` race.
        torrents_snap = tuple(store.torrents.values())
        t0 = time.monotonic()
        new_index = build_index(
            last_snapshot,
            torrents_snap,
            title_fallback=settings.arr_title_fallback,
        )
        logger.debug(
            "arr rebuild: torrents=%d -> matches=%d (%.0f ms, qbit_rid=%d, arr_rid=%d)",
            len(torrents_snap),
            len(new_index),
            (time.monotonic() - t0) * 1000,
            store.rid,
            arr_store.rid,
        )
        # Atomic dict-reference swap so concurrent readers (template render
        # under SSE) see either the old dict or the new one whole -- never
        # a half-mutated state.
        arr_store.hash_to_arr = new_index
        arr_store.rid += 1
        # Counts per source so the activity dialog can render
        # "Linked N (Radarr) / M (Sonarr)" instead of a single opaque total.
        radarr_matches = sum(
            1 for m in new_index.values() if m.source == "radarr"
        )
        sonarr_matches = sum(
            1 for m in new_index.values() if m.source == "sonarr"
        )
        arr_store.radarr_match_count = radarr_matches
        arr_store.sonarr_match_count = sonarr_matches
        # Remove our own listener before publishing so the RESYNC we emit
        # below doesn't re-enter qbit_resync_event and re-trigger this
        # rebuild. ``EventBus.remove`` uses ``set.discard`` (idempotent),
        # so a stray double-remove from a racing tick is harmless.
        # ``bus.add(listener)`` MUST run regardless of how ``remove`` /
        # ``publish`` go -- wrapping each in its own try/finally ensures
        # we never leave the listener un-registered, which would silently
        # stop the arr poller from reacting to qBit RESYNCs.
        logger.debug("arr rebuild publish: removing listener")
        try:
            try:
                bus.remove(listener)
            except Exception:
                logger.exception("arr poller: bus.remove raised")
            try:
                bus.publish(DomainEvent(kind=EventKind.RESYNC))
            except Exception:
                logger.exception("arr poller: bus.publish raised")
        finally:
            try:
                bus.add(listener)
                logger.debug("arr rebuild publish: re-added listener")
            except Exception:
                logger.exception("arr poller: bus.add raised (listener lost!)")
        return len(new_index)

    def _log_arr_state_change(snapshot: ArrSnapshot) -> None:
        """Append log lines when a service's reachability flips.

        Steady-state success only emits one line on first connect; subsequent
        successful fetches are silent (the live counters under each service
        card already convey "still working"). Failures always log so the
        user can correlate a stalled UI with an arr outage.
        """
        for name, attempted, fetched, error, count_a, count_b in (
            (
                "Radarr",
                snapshot.radarr_attempted,
                snapshot.radarr_fetched,
                snapshot.radarr_error,
                len(snapshot.movies),
                len(snapshot.radarr_queue),
            ),
            (
                "Sonarr",
                snapshot.sonarr_attempted,
                snapshot.sonarr_fetched,
                snapshot.sonarr_error,
                len(snapshot.series),
                len(snapshot.sonarr_queue),
            ),
        ):
            if not attempted:
                continue
            key = name.lower()
            new_state = "ok" if fetched else "down"
            prior = last_logged_state[key]
            if new_state == prior:
                continue
            last_logged_state[key] = new_state
            if fetched:
                unit = "movies" if name == "Radarr" else "series"
                _append_activity(
                    store,
                    f"{name} OK -- {count_a} {unit}, {count_b} in queue",
                )
            else:
                _append_activity(
                    store,
                    f"{name} unreachable -- {error or 'unknown error'}",
                )

    async def arr_fetch_loop() -> None:
        async for snapshot in poll_arr(settings, http_client):
            _apply_snapshot(snapshot)
            _log_arr_state_change(snapshot)
            qbit_rid = store.rid
            matches = _rebuild_index_and_publish()
            # Surface the first successful link in the activity log so the
            # user sees arr work happening alongside qBit chunks. Subsequent
            # cycles add a "Refreshed" line so the dialog feels live even
            # during steady-state -- trim cap stops the log growing.
            if arr_store.arr_fetch_cycles == 1 and matches > 0:
                _append_activity(
                    store,
                    f"Linked {matches} torrents -- "
                    f"{arr_store.radarr_match_count} Radarr / "
                    f"{arr_store.sonarr_match_count} Sonarr",
                )
            elif arr_store.arr_fetch_cycles > 1 and (
                snapshot.radarr_fetched or snapshot.sonarr_fetched
            ):
                _append_activity(
                    store,
                    f"Refreshed arr index -- {matches} linked torrents",
                )
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

    # One client for the whole task lifetime. httpx explicitly recommends
    # reusing the same AsyncClient across many requests so the connection
    # pool survives and TLS handshakes are amortised across ticks.
    # ``follow_redirects`` mirrors the ``make_client`` default.
    http_client = arr_client.make_client()
    try:
        await asyncio.gather(arr_fetch_loop(), qbit_reindex_loop())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("arr poller raised; task exiting")
    finally:
        bus.remove(listener)
        with contextlib.suppress(Exception):
            await http_client.aclose()


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
        # Shutdown order matters:
        # 1. Cancel pollers + await them so no more events publish.
        # 2. Drain SSE subscriber queues so any in-flight events don't
        #    fire across the qBit logout boundary and so the per-client
        #    queues release their buffers promptly.
        # 3. Log out of qBit on a worker thread.
        # 4. Persist the parser cache.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if arr_task is not None:
            arr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await arr_task
        subscriptions = getattr(app.state, "subscriptions", None) or {}
        for sub in list(subscriptions.values()):
            with contextlib.suppress(Exception):
                sub.drain()
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
