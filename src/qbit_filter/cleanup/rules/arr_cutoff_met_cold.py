"""arr cutoff-met-and-cold rule: quality cutoff reached, cold, ratio paid."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qbit_filter.cleanup.scoring import age_days
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.state.store import Store

ORDER: int = 50


@dataclass(frozen=True, slots=True)
class ArrCutoffMetColdRule:
    """*arr's quality cutoff is met AND torrent is cold + ratio paid.

    The user's own quality goal for this title (set in Radarr/Sonarr's quality
    profile) has been reached. Combined with cold last_activity and met ratio,
    the torrent has done its job; safe to retire.

    Sonarr exposes cutoff-met per episode, not per series, so for TV the
    rule uses the series-level heuristic from
    :func:`qbit_filter.arr.index._series_match` (episode_file_count >=
    total_episode_count).
    """

    slug: str = "arr-cutoff-met-cold"
    label: str = "arr: cutoff met + cold"
    description: str = (
        "Radarr/Sonarr quality cutoff met, no activity for 30+ days, "
        "ratio >= 1.5. Your own quality goal is reached -- safe to retire."
    )
    ratio_threshold: float = 1.5
    cold_days_threshold: int = 30

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        arr = store.arr
        if arr is None or not arr.hash_to_arr:
            return []
        base = now if now is not None else int(time.time())
        cold_cutoff = base - self.cold_days_threshold * 86_400
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for h in group.torrent_hashes:
                t = store.torrents.get(h)
                if t is None:
                    continue
                match = arr.hash_to_arr.get(t.hash.lower())
                if match is None or not match.quality_cutoff_met:
                    continue
                if t.ratio < self.ratio_threshold:
                    continue
                # Cold check: prefer last_activity (real signal), fall back to
                # added_on (so torrents predating the field still qualify).
                if t.last_activity > 0:
                    if t.last_activity >= cold_cutoff:
                        continue
                    cold_days = age_days(t.last_activity, now)
                    cold_factor = ReasonFactor("cold", f"{cold_days}d", "bad")
                else:
                    if t.added_on >= cold_cutoff:
                        continue
                    if t.dlspeed > 0 or t.upspeed > 0:
                        continue
                    cold_days = age_days(t.added_on, now)
                    cold_factor = ReasonFactor("cold", "idle", "bad")
                factors: tuple[ReasonFactor, ...] = (
                    ReasonFactor(
                        "cutoff", f"{match.source} met", "good"
                    ),
                    cold_factor,
                    ReasonFactor("ratio", f"{t.ratio:.2f} met", "good"),
                )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=(
                            f"{match.source} cutoff met, cold {cold_days}d, "
                            f"ratio {t.ratio:.2f}"
                        ),
                        factors=factors,
                    )
                )
        return out


RULE: Rule = ArrCutoffMetColdRule()
