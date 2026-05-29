"""arr unmonitored-and-completed rule: arr stopped monitoring, ratio met."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.scoring import age_days
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.state.store import Store

ORDER: int = 60


@dataclass(frozen=True, slots=True)
class ArrUnmonitoredCompletedRule:
    """*arr no longer monitors this title, the torrent is at 100%, ratio met.

    The user has already decided they're done with this title in Radarr/Sonarr.
    Keeping the torrent alive just costs disk + (occasionally) seedbox bytes
    for no upside. Surfaces them as candidates; user reviews + confirms.

    No-op when *arr is not configured (``store.arr is None``) -- the rule
    appears in the registry but matches zero candidates, same pattern as the
    other "data not yet available" rules.
    """

    slug: str = "arr-unmonitored"
    label: str = "arr: unmonitored + ratio met"
    description: str = (
        "Radarr/Sonarr no longer monitors the title, torrent is fully "
        "downloaded, ratio >= 1.0. Safe-deletion candidate."
    )
    ratio_threshold: float = 1.0

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
                if match is None or match.monitored:
                    continue
                if t.progress < 1.0:
                    continue
                if t.ratio < self.ratio_threshold:
                    continue
                age = age_days(t.added_on, now)
                factors: tuple[ReasonFactor, ...] = (
                    ReasonFactor(
                        "arr", f"{match.source} unmonitored", "bad"
                    ),
                    ReasonFactor("ratio", f"{t.ratio:.2f}", "good"),
                    ReasonFactor("age", f"{age}d", "neutral"),
                )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=(
                            f"{match.source} no longer monitors "
                            f"'{match.title}', ratio {t.ratio:.2f}"
                        ),
                        factors=factors,
                    )
                )
        return out


RULE: Rule = ArrUnmonitoredCompletedRule()
