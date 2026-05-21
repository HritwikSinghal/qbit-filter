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
        for sub in list(self._subs):
            try:
                sub.notify(event)
            except Exception:
                logger.exception("subscriber.notify raised")
