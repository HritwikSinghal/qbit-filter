"""Cleanup-rule presets. Each rule is a pure function from a Store snapshot to
a list of Candidate torrents marked for removal, with a reason.

Rules never mutate state. The web layer turns Candidates into a preview the
user confirms; only on confirm does ``qbit/actions.delete`` run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from qbit_filter.domain import (
    GroupKey,
    QualityTier,
    tier_rank,
)
from qbit_filter.state.store import Store


@dataclass(frozen=True, slots=True)
class Candidate:
    """A torrent the rule wants the user to consider deleting.

    ``reason`` is a short human-readable phrase shown inline next to the row.
    ``keeper_hash`` is the sibling (better-quality / live tracker / etc) the
    rule recommends keeping. Empty string if no obvious sibling.
    """

    torrent_hash: str
    group_key: GroupKey
    reason: str
    keeper_hash: str = ""


class Rule(Protocol):
    """All rule presets implement this shape.

    The metadata attributes are exposed as read-only properties so that
    frozen-dataclass implementations (whose fields can't be reassigned)
    still satisfy the protocol under ``mypy --strict``.
    """

    @property
    def slug(self) -> str: ...
    @property
    def label(self) -> str: ...
    @property
    def description(self) -> str: ...

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]: ...


def _age_days(ts: int, now: int | None) -> int:
    base = now if now is not None else int(time.time())
    return max(0, (base - ts) // 86_400)


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
            torrents = [
                store.torrents[h]
                for h in group.torrent_hashes
                if h in store.torrents
                and store.torrents[h].quality.tier is not QualityTier.UNKNOWN
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
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=(
                            f"superseded by {keeper.quality.tier.value} "
                            f"(added {_age_days(keeper.added_on, now)}d later)"
                        ),
                        keeper_hash=keeper.hash,
                    )
                )
        return out


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
                age = _age_days(t.added_on, now)
                if idle and not t.is_no_peers:
                    idle_age = _age_days(t.last_activity, now)
                    reason = f"idle {idle_age}d, age {age}d, ratio {t.ratio:.2f}"
                else:
                    reason = f"no peers, age {age}d, ratio {t.ratio:.2f}"
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=reason,
                    )
                )
        return out


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
                    cold_days = _age_days(t.last_activity, now)
                    reason = (
                        f"ratio {t.ratio:.2f} met, cold {cold_days}d "
                        f"(last activity)"
                    )
                else:
                    # Fallback: no last_activity from qBit. Use the original
                    # instantaneous check + age >= threshold.
                    if t.added_on >= cold_cutoff:
                        continue
                    if t.dlspeed > 0 or t.upspeed > 0:
                        continue
                    reason = (
                        f"ratio {t.ratio:.2f} met, idle, "
                        f"age {_age_days(t.added_on, now)}d"
                    )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=reason,
                    )
                )
        return out


@dataclass(frozen=True, slots=True)
class DeadTrackerRule:
    slug: str = "dead-tracker"
    label: str = "Dead / unregistered tracker"
    description: str = "Tracker reports unregistered or returns 404. Won't seed."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        # Requires per-tracker status (not yet captured by reconciler).
        # When tracker-status sync lands, scan store.torrents for any tracker
        # whose status string contains 'unregistered' or HTTP 404.
        raise NotImplementedError("requires tracker-status sync")


@dataclass(frozen=True, slots=True)
class CrossSeedDuplicateRule:
    slug: str = "cross-seed-duplicate"
    label: str = "Cross-seed duplicate"
    description: str = "Same files on disk, two infohashes. Keep the better one."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        # Requires per-torrent content_path / files list (not yet captured).
        raise NotImplementedError("requires content-path capture")


@dataclass(frozen=True, slots=True)
class OrphanedOnDiskRule:
    slug: str = "orphaned-on-disk"
    label: str = "Orphaned on disk"
    description: str = "Files in the download dir not referenced by any torrent."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        # Requires disk-walk + cross-reference (out of scope for in-memory store).
        raise NotImplementedError("requires disk walker")


@dataclass(frozen=True, slots=True)
class PathCollisionRule:
    slug: str = "path-collision"
    label: str = "Path collision"
    description: str = "Two torrents claim overlapping save paths."

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        raise NotImplementedError("requires content-path capture")
