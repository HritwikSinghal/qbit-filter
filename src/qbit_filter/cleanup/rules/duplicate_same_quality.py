"""Duplicate-same-quality rule: multiple copies at the same tier in one group
(per season for TV). Keep arr's live import (or the newest), flag the rest."""

from __future__ import annotations

import time
from dataclasses import dataclass

from qbit_filter.cleanup import factors as F
from qbit_filter.cleanup.scoring import (
    FREELEECH_PENALTY_WINDOW_DAYS,
    partition_by_season,
    pick_arr_current_keeper,
)
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule, Severity
from qbit_filter.domain import QualityTier, Torrent
from qbit_filter.state.store import Store

ORDER: int = 20


@dataclass(frozen=True, slots=True)
class DuplicateSameQualityRule:
    """Within one group, multiple torrents at the same quality tier.

    Keeper selection (in precedence order):

    1. The arr-current torrent -- whichever copy is backing arr's most
       recent ``downloadFolderImported`` event. arr drops the old
       ``downloadId`` from the imported head the moment it imports an
       upgrade, so this is a definitive "live file on disk" signal that
       trumps any local heuristic.
    2. Newest by add date. Used when arr isn't configured, the group
       isn't matched to an arr entity, or the matched entity has no
       imported history (e.g. a fresh search that hasn't completed).

    Every non-keeper at the same tier is flagged. Complementary to
    :class:`SupersededQualityRule` -- where that one handles the
    cross-tier case, this one handles the same-tier case.

    Source / codec mismatches between the keeper and the flagged copy are
    surfaced as neutral pills so a user keeping both a WEB-DL and a BluRay
    on purpose can deselect with full context.

    When both the keeper and the flagged copy were added within the last
    ``freeleech_window_days`` days, the candidate is tagged
    ``severity="warning"`` and gets an extra ``freeleech`` pill. Tracker
    penalty for dropping un-seeded freeleech downloads is real, so the
    yellow row is the "look twice before confirming" signal.
    """

    slug: str = "duplicate-same-quality"
    label: str = "Duplicate (same quality)"
    description: str = (
        "Multiple torrents at the same quality tier. Keep arr's currently "
        "imported copy (or the newest if arr has no opinion), flag the rest."
    )
    freeleech_window_days: int = FREELEECH_PENALTY_WINDOW_DAYS

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        base = now if now is not None else int(time.time())
        fl_cutoff = base - self.freeleech_window_days * 86_400
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for season_bucket in partition_by_season(group, store):
                torrents = [
                    t for t in season_bucket
                    if t.quality.tier is not QualityTier.UNKNOWN
                ]
                if len(torrents) < 2:
                    continue
                # Bucket by tier value (string), not by full Quality, so a 1080p
                # WEB-DL and a 1080p BluRay land in the same bucket and we can
                # surface the source mismatch as a factor pill on the flagged
                # row rather than silently skipping the comparison.
                by_tier: dict[QualityTier, list[Torrent]] = {}
                for t in torrents:
                    by_tier.setdefault(t.quality.tier, []).append(t)
                for tier, bucket in by_tier.items():
                    if len(bucket) < 2:
                        continue
                    # Newest first as the tiebreaker fallback. The primary
                    # keeper signal is arr's currently-imported file: if any
                    # torrent in the bucket is the source of arr's
                    # most-recent ``downloadFolderImported`` event for the
                    # owning entity, arr already considers it the live copy
                    # and we promote it to keeper. Falls back to "newest" so
                    # untagged / non-arr groups still flag duplicates.
                    bucket.sort(key=lambda t: t.added_on, reverse=True)
                    keeper = pick_arr_current_keeper(bucket, store) or bucket[0]
                    for t in bucket:
                        if t.hash == keeper.hash:
                            continue
                        factors: list[ReasonFactor] = [
                            ReasonFactor("tier", tier.value, "neutral"),
                            F.added_gap_factor(t.added_on, keeper.added_on),
                        ]
                        factors.extend(F.source_codec_factors(t, keeper))
                        size_factor = F.size_delta_factor(t.size, keeper.size)
                        if size_factor is not None:
                            factors.append(size_factor)
                        ratio_factor = F.ratio_redeemer_factor(t, keeper)
                        if ratio_factor is not None:
                            factors.append(ratio_factor)
                        in_window = (
                            keeper.added_on >= fl_cutoff
                            and t.added_on >= fl_cutoff
                        )
                        severity: Severity = "normal"
                        if in_window:
                            factors.append(
                                ReasonFactor(
                                    "freeleech",
                                    f"both <={self.freeleech_window_days}d",
                                    "warning",
                                )
                            )
                            severity = "warning"
                        gap_days = max(
                            0, abs(t.added_on - keeper.added_on) // 86_400
                        )
                        direction = (
                            "before keeper"
                            if t.added_on <= keeper.added_on
                            else "after keeper"
                        )
                        out.append(
                            Candidate(
                                torrent_hash=t.hash,
                                group_key=key,
                                reason=(
                                    f"duplicate {tier.value} "
                                    f"(added {gap_days}d {direction})"
                                ),
                                keeper_hash=keeper.hash,
                                factors=tuple(factors),
                                severity=severity,
                            )
                        )
        return out


RULE: Rule = DuplicateSameQualityRule()
