"""Dead-tracker rule (stub): tracker reports unregistered / 404."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.types import Candidate, Rule
from qbit_filter.state.store import Store

ORDER: int = 90


@dataclass(frozen=True, slots=True)
class DeadTrackerRule:
    slug: str = "dead-tracker"
    label: str = "Dead / unregistered tracker"
    description: str = "Tracker reports unregistered or returns 404. Won't seed."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        # Requires per-tracker status (not yet captured by reconciler).
        # When tracker-status sync lands, scan store.torrents for any tracker
        # whose status string contains 'unregistered' or HTTP 404.
        raise NotImplementedError("requires tracker-status sync")


RULE: Rule = DeadTrackerRule()
