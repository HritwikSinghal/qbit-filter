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

from qbit_filter.config import Settings
from qbit_filter.qbit.client import connect
from qbit_filter.qbit.sync import poll
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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    store = Store()
    bus = EventBus()
    reconciler = Reconciler(store=store, bus=bus, settings=settings)
    app.state.store = store
    app.state.bus = bus
    app.state.reconciler = reconciler
    app.state.subscriptions = {}
    app.state.qbit = None

    task = asyncio.create_task(_poller(app, reconciler, settings))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        client = app.state.qbit
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.auth_log_out)


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
