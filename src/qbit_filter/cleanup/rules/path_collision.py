"""Path-collision rule (stub): two torrents claim overlapping save paths."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.types import Candidate, Rule
from qbit_filter.state.store import Store

ORDER: int = 120


@dataclass(frozen=True, slots=True)
class PathCollisionRule:
    slug: str = "path-collision"
    label: str = "Path collision"
    description: str = "Two torrents claim overlapping save paths."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        raise NotImplementedError("requires content-path capture")


RULE: Rule = PathCollisionRule()
