"""Per-SSE-client subscription. Holds its own :class:`FilterState` + bounded queue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from qbit_filter.domain import DomainEvent, EventKind, FilterState
from qbit_filter.state.viewport import Viewport

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class Subscription:
    """One per SSE client. ``eq=False`` keeps identity-based hashing so
    :class:`~qbit_filter.state.events.EventBus` can store these in a set."""

    filter_state: FilterState
    # Sized for a full per-tick burst (~1 event per active torrent). At 256 the
    # queue overflowed every tick and forced an overflow-RESYNC, which made the
    # SSE renderer re-render every group every second.
    max_queue: int = 4096
    queue: asyncio.Queue[DomainEvent] = field(init=False)
    # Number of live SSE streams using this Subscription. Multiple tabs share
    # one sid, so the bus subscription must stay alive until the last stream
    # disconnects.
    sse_refs: int = 0
    # Monotonic timestamp of the last RESYNC payload emitted to the client.
    # Used by the SSE renderer to coalesce back-to-back RESYNCs (each is a
    # ~900KB full-page swap; two within a second visibly stutters the UI).
    last_resync_at: float = 0.0
    # Monotonic timestamp of the last cold-boot RESYNC_PARTIAL emission.
    # Partials bypass the 1 s RESYNC_COALESCE_INTERVAL but honour a small
    # floor (RESYNC_PARTIAL_MIN_INTERVAL) to guard against runaway publishers.
    last_partial_at: float = 0.0
    # Viewport tracking: which group keys this client currently sees (plus
    # overscan). Populated by POST /viewport. notify() uses this to drop
    # per-row events for groups outside the visible window -- the user's
    # headline optimisation for ~1100-torrent stores.
    viewport: Viewport = field(default_factory=Viewport)
    # Slug of the currently-active cleanup rule preview, or "" when no
    # preview is active. SSE renders (RESYNC + per-row TORRENT_CHANGED) use
    # this to recompute rule marks/keepers so the compare-strip layout
    # survives background polls instead of reverting to the flat row stack.
    active_rule_slug: str = ""
    # Loop reference captured at construction so :meth:`notify` can be
    # called safely from a worker thread (e.g. ``asyncio.to_thread``).
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.max_queue)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # Constructed outside an async context (tests). notify() will
            # fall back to a direct put_nowait, which is fine while the
            # caller stays on the same thread.
            self._loop = None

    def drain(self) -> None:
        """Drop any pending events. Called when the last SSE stream exits so a
        future reconnect doesn't replay stale state."""
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def set_filter(self, fs: FilterState) -> None:
        self.filter_state = fs

    def _enqueue(self, event: DomainEvent) -> None:
        """Loop-thread half of :meth:`notify`. Never call from a worker
        thread directly -- :py:class:`asyncio.Queue` is not thread-safe."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            try:
                self.queue.put_nowait(DomainEvent(kind=EventKind.RESYNC))
            except asyncio.QueueFull:
                logger.warning("subscription queue still full after Resync")

    def notify(self, event: DomainEvent) -> None:
        """Enqueue without blocking. Safe to call from any thread: when the
        caller is the loop thread we put directly; from a worker thread we
        hop back via ``loop.call_soon_threadsafe`` since
        :py:class:`asyncio.Queue` is not thread-safe."""
        # Viewport-keyed filtering: drop per-row events for groups outside
        # the client's visible window. RESYNC and GROUP_ADDED/REMOVED always
        # go through -- they affect global counts / structure and are cheap.
        # An empty hot set means the client hasn't reported a viewport yet
        # (or scrolled to a region with zero groups), so we fall back to
        # delivering everything.
        if (
            event.group_key is not None
            and self.viewport.hot
            and event.kind
            in (
                EventKind.TORRENT_ADDED,
                EventKind.TORRENT_CHANGED,
                EventKind.TORRENT_REMOVED,
            )
            and event.group_key.slug() not in self.viewport.hot
        ):
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            self._enqueue(event)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._enqueue(event)
        else:
            loop.call_soon_threadsafe(self._enqueue, event)
