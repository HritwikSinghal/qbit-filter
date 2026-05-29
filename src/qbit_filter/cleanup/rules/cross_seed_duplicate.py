"""Cross-seed-duplicate rule (stub): same files, two infohashes."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.types import Candidate, Rule
from qbit_filter.state.store import Store

ORDER: int = 100


@dataclass(frozen=True, slots=True)
class CrossSeedDuplicateRule:
    slug: str = "cross-seed-duplicate"
    label: str = "Cross-seed duplicate"
    description: str = "Same files on disk, two infohashes. Keep the better one."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        # Requires per-torrent content_path / files list (not yet captured).
        raise NotImplementedError("requires content-path capture")


RULE: Rule = CrossSeedDuplicateRule()
