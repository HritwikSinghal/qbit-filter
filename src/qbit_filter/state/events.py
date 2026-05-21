"""Event fan-out to subscriptions."""

from __future__ import annotations

import logging
from typing import Protocol

from qbit_filter.domain import DomainEvent

logger = logging.getLogger(__name__)


class Subscriber(Protocol):
    def notify(self, event: DomainEvent) -> None: ...


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()

    def add(self, sub: Subscriber) -> None:
        self._subs.add(sub)

    def remove(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def __len__(self) -> int:
        return len(self._subs)

    def __contains__(self, sub: object) -> bool:
        return sub in self._subs

    def publish(self, event: DomainEvent) -> None:
        subs = list(self._subs)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "bus.publish kind=%s subs=%d group=%s hash=%s",
                event.kind.name,
                len(subs),
                event.group_key.slug() if event.group_key else "-",
                (event.torrent_hash or "-")[:8],
            )
        for sub in subs:
            try:
                sub.notify(event)
            except Exception:
                logger.exception("subscriber.notify raised")
