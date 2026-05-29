"""arr orphaned-tag rule: qBit carries a radarr:/sonarr: tag for an entity
arr no longer has."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.scoring import age_days
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.state.store import Store

ORDER: int = 80


@dataclass(frozen=True, slots=True)
class OrphanedArrTagRule:
    """qBit torrent carries ``radarr:N`` / ``sonarr:N`` but arr has no entity
    with that id.

    User deleted the movie/series from arr but the qBit torrent kept the
    tag. The torrent is now ownership-less from arr's POV -- no upgrade
    monitoring, no quality cutoff tracking, no future searches. It's just
    bytes on disk that arr already decided it doesn't want.

    Detection runs in :func:`qbit_filter.arr.index._match_by_tag`: a tag
    pointing at a missing entity produces an :class:`ArrMatch` with
    ``orphaned=True`` and the dangling ``entity_id``.
    """

    slug: str = "arr-orphaned-tag"
    label: str = "arr: orphaned tag"
    description: str = (
        "qBit torrent has a radarr:/sonarr: tag pointing at an entity that "
        "no longer exists in arr. The title was removed in arr -- the "
        "torrent is leftover."
    )

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        arr = store.arr
        if arr is None or not arr.hash_to_arr:
            return []
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for h in group.torrent_hashes:
                t = store.torrents.get(h)
                if t is None:
                    continue
                match = arr.hash_to_arr.get(t.hash.lower())
                if match is None or not match.orphaned:
                    continue
                age = age_days(t.added_on, now)
                factors: tuple[ReasonFactor, ...] = (
                    ReasonFactor(
                        "arr",
                        f"{match.source} entity #{match.entity_id} gone",
                        "bad",
                    ),
                    ReasonFactor("age", f"{age}d", "neutral"),
                    ReasonFactor("ratio", f"{t.ratio:.2f}", "neutral"),
                )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=(
                            f"{match.source} entity #{match.entity_id} "
                            "no longer in library"
                        ),
                        factors=factors,
                    )
                )
        return out


RULE: Rule = OrphanedArrTagRule()
