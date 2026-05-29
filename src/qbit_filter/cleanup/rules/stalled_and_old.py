"""Stalled-and-old rule: old, no-peer / idle, ratio below target."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qbit_filter.cleanup.scoring import age_days
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.state.store import Store

ORDER: int = 30


@dataclass(frozen=True, slots=True)
class StalledAndOldRule:
    """Added > 90 days ago, no peer activity (stalled / paused / stopped, or
    inactive for >= ``idle_days_threshold`` days), ratio < 1.0. Almost
    certainly dead tracker or unpopular release.

    ``raw_state`` is consulted via :attr:`Torrent.is_no_peers` so that
    ``stalledUP`` -- which the rest of the UI shows as plain "seeding" --
    still counts as cleanup-eligible.
    """

    slug: str = "stalled-and-old"
    label: str = "Stalled + old"
    description: str = (
        "Added > 90 days ago, no peers (stalled / paused) or idle 7+ days, "
        "ratio below 1.0."
    )
    days_threshold: int = 90
    idle_days_threshold: int = 7
    ratio_threshold: float = 1.0

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        base = now if now is not None else int(time.time())
        cutoff = base - self.days_threshold * 86_400
        idle_cutoff = base - self.idle_days_threshold * 86_400
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for h in group.torrent_hashes:
                t = store.torrents.get(h)
                if t is None or t.added_on >= cutoff:
                    continue
                # Eligible if qBit says no peers, OR last_activity is older
                # than idle_days_threshold (last_activity == 0 means qBit
                # didn't supply the field -- fall back to the state check).
                idle = t.last_activity > 0 and t.last_activity < idle_cutoff
                if not (t.is_no_peers or idle):
                    continue
                if t.ratio >= self.ratio_threshold:
                    continue
                age = age_days(t.added_on, now)
                factors: list[ReasonFactor] = [
                    ReasonFactor("age", f"{age}d old", "neutral"),
                    ReasonFactor("ratio", f"{t.ratio:.2f}", "bad"),
                ]
                if idle and not t.is_no_peers:
                    idle_age = age_days(t.last_activity, now)
                    reason = f"idle {idle_age}d, age {age}d, ratio {t.ratio:.2f}"
                    factors.insert(0, ReasonFactor("idle", f"{idle_age}d", "bad"))
                else:
                    reason = f"no peers, age {age}d, ratio {t.ratio:.2f}"
                    factors.insert(0, ReasonFactor("peers", "0", "bad"))
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=reason,
                        factors=tuple(factors),
                    )
                )
        return out


RULE: Rule = StalledAndOldRule()
