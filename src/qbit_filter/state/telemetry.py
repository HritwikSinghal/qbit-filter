"""qBit-poller connection telemetry.

Mutated only by the qBit poller (``app._poller`` and ``qbit/sync.poll()``);
everyone else reads. Lives off :class:`~qbit_filter.state.store.Store` (as a
non-optional ``Store.telemetry`` handle) so the canonical store stays focused
on torrent/group state and doesn't accrete poller-health fields.

All reads and writes happen on the single event loop -- ``poll()``'s only
off-loop hop is the awaited ``sync_maindata`` thread, and the counter writes run
on the loop after it returns -- so no locks or atomics are needed. A plain
mutable dataclass is correct, exactly like ``Store`` and ``ArrStore``.

Note: this is qBit-only on purpose. ``ArrStore`` already carries its own
parallel per-service telemetry on a different (60s) cadence; unifying the two
would re-couple the qBit reconciler and the arr poller the codebase
deliberately keeps apart. Cold-boot progress (``Store.cold_boot_*``) likewise
stays on ``Store`` because the reconciler -- the sole ``Store`` mutator -- owns
it and the SSE hot path reads it directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Telemetry:
    qbit_connected: bool = False
    # Wall-clock ``time.time()`` so the browser can render it as "N seconds
    # ago" relative to its own clock; monotonic time would be meaningless
    # across the process / client boundary.
    qbit_last_poll_at: float = 0.0
    qbit_poll_count: int = 0
    # Last poll/connect error; cleared on the next successful poll.
    qbit_last_error: str = ""
    qbit_host: str = ""
    # Rolling count of consecutive failed poll ticks. Reset to 0 on the next
    # successful tick. Drives the capped-exponential backoff inside
    # ``qbit/sync.py:poll()`` and the healthy<->degraded transition logged by
    # ``app._poller``.
    qbit_consecutive_failures: int = 0
