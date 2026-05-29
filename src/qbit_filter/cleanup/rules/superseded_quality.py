"""Superseded-quality rule: a lower-tier copy with a later-added higher-tier
sibling in the same group (per season for TV)."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup import factors as F
from qbit_filter.cleanup.scoring import age_days, partition_by_season
from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.domain import QualityTier, tier_rank
from qbit_filter.state.store import Store

ORDER: int = 10


@dataclass(frozen=True, slots=True)
class SupersededQualityRule:
    """Within one group, if a torrent has a strictly higher-tier sibling AND
    that sibling was added later, the lower-tier torrent is superseded.

    The "added later" guard is important: if the user explicitly added a 1080p
    after an existing 2160p, they probably want it (mobile, language track,
    smaller for travel). Don't second-guess.
    """

    slug: str = "superseded-quality"
    label: str = "Superseded quality"
    description: str = (
        "1080p (or lower) torrent has a 2160p sibling added later. "
        "Mark the older lower-quality copy for removal."
    )

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for bucket in partition_by_season(group, store):
                torrents = [
                    t for t in bucket
                    if t.quality.tier is not QualityTier.UNKNOWN
                ]
                if len(torrents) < 2:
                    continue
                best_rank = max(tier_rank(t.quality.tier) for t in torrents)
                top_siblings = [
                    t for t in torrents if tier_rank(t.quality.tier) == best_rank
                ]
                # Compare lower-tier copies to the EARLIEST-added top-tier sibling,
                # so a later-added second 1080p is still flagged when an older
                # 2160p sits next to it. Using max() tiebreaks on added_on lets
                # the "best" pointer drift between same-tier copies and silently
                # filters genuine supersedes.
                keeper = min(top_siblings, key=lambda t: t.added_on)
                for t in torrents:
                    if tier_rank(t.quality.tier) >= best_rank:
                        continue
                    if t.added_on >= keeper.added_on:
                        # Lower-tier copy added AFTER the top-tier one is here.
                        # Treat as intentional (mobile / smaller / language) and
                        # don't second-guess.
                        continue
                    factors: list[ReasonFactor] = [
                        ReasonFactor(
                            "tier",
                            f"{t.quality.tier.value} → {keeper.quality.tier.value}",
                            "bad",
                        ),
                        F.added_gap_factor(t.added_on, keeper.added_on),
                    ]
                    size_factor = F.size_delta_factor(t.size, keeper.size)
                    if size_factor is not None:
                        factors.append(size_factor)
                    ratio_factor = F.ratio_redeemer_factor(t, keeper)
                    if ratio_factor is not None:
                        factors.append(ratio_factor)
                    out.append(
                        Candidate(
                            torrent_hash=t.hash,
                            group_key=key,
                            reason=(
                                f"superseded by {keeper.quality.tier.value} "
                                f"(added {age_days(keeper.added_on, now)}d later)"
                            ),
                            keeper_hash=keeper.hash,
                            factors=tuple(factors),
                        )
                    )
        return out


RULE: Rule = SupersededQualityRule()
