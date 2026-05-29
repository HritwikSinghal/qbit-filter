"""Ratio-met-and-cold rule: ratio target reached and no recent activity."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qbit_filter.cleanup.scoring import age_days
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.state.store import Store

ORDER: int = 40


@dataclass(frozen=True, slots=True)
class RatioMetAndColdRule:
    """Ratio >= target and no transfer activity for ``cold_days_threshold``
    days. Safe to remove for space; you've paid your dues.

    Originally this rule used ``dlspeed == 0 and upspeed == 0``, but those
    are instantaneous samples -- a seeding torrent is almost always idle at
    any single tick, yet bursts later. ``last_activity`` (when qBit supplies
    it) is the real "cold" signal. When the field is unavailable (== 0) we
    fall back to the older instantaneous check so behaviour degrades safely.
    """

    slug: str = "ratio-met-cold"
    label: str = "Ratio met + cold"
    description: str = (
        "Ratio >= 1.5 and no activity for 30+ days (last_activity)."
    )
    ratio_threshold: float = 1.5
    cold_days_threshold: int = 30

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        base = now if now is not None else int(time.time())
        cold_cutoff = base - self.cold_days_threshold * 86_400
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for h in group.torrent_hashes:
                t = store.torrents.get(h)
                if t is None:
                    continue
                if t.ratio < self.ratio_threshold:
                    continue
                if t.last_activity > 0:
                    if t.last_activity >= cold_cutoff:
                        continue
                    cold_days = age_days(t.last_activity, now)
                    reason = (
                        f"ratio {t.ratio:.2f} met, cold {cold_days}d "
                        f"(last activity)"
                    )
                    factors: tuple[ReasonFactor, ...] = (
                        ReasonFactor("ratio", f"{t.ratio:.2f} met", "good"),
                        ReasonFactor("cold", f"{cold_days}d", "bad"),
                    )
                else:
                    # Fallback: no last_activity from qBit. Use the original
                    # instantaneous check + age >= threshold.
                    if t.added_on >= cold_cutoff:
                        continue
                    if t.dlspeed > 0 or t.upspeed > 0:
                        continue
                    age = age_days(t.added_on, now)
                    reason = (
                        f"ratio {t.ratio:.2f} met, idle, "
                        f"age {age}d"
                    )
                    factors = (
                        ReasonFactor("ratio", f"{t.ratio:.2f} met", "good"),
                        ReasonFactor("idle", "now", "bad"),
                        ReasonFactor("age", f"{age}d", "neutral"),
                    )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=reason,
                        factors=factors,
                    )
                )
        return out


RULE: Rule = RatioMetAndColdRule()
