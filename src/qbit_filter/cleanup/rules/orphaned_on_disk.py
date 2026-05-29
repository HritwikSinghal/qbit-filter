"""Orphaned-on-disk rule (stub): files not referenced by any torrent."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.types import Candidate, Rule
from qbit_filter.state.store import Store

ORDER: int = 110


@dataclass(frozen=True, slots=True)
class OrphanedOnDiskRule:
    slug: str = "orphaned-on-disk"
    label: str = "Orphaned on disk"
    description: str = "Files in the download dir not referenced by any torrent."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        # Requires disk-walk + cross-reference (out of scope for in-memory store).
        raise NotImplementedError("requires disk walker")


RULE: Rule = OrphanedOnDiskRule()
