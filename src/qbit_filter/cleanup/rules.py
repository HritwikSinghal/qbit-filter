"""Cleanup-rule presets. Each rule is a pure function from a Store snapshot to
a list of Candidate torrents marked for removal, with a reason.

Rules never mutate state. The web layer turns Candidates into a preview the
user confirms; only on confirm does ``qbit/actions.delete`` run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from qbit_filter.domain import (
    Group,
    GroupKey,
    GroupKind,
    QualityTier,
    Torrent,
    tier_rank,
)
from qbit_filter.grouping.parser import quick_season
from qbit_filter.state.store import Store

FactorKind = Literal["bad", "good", "neutral", "warning"]
Severity = Literal["normal", "warning"]


@dataclass(frozen=True, slots=True)
class ReasonFactor:
    """One structured piece of *why* a torrent was flagged.

    Rendered as a colored pill inline on the row (or in the compare strip).
    ``kind`` drives the colour: ``bad`` (red) = pushes toward removal,
    ``good`` (green) = a redeeming property the user might want to weigh,
    ``neutral`` (gray) = just a delta worth showing, ``warning`` (yellow)
    = caution, the user may want to double-check before confirming (e.g.
    a freshly-added freeleech torrent that hasn't met its seed window).
    """

    label: str
    value: str
    kind: FactorKind = "neutral"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A torrent the rule wants the user to consider deleting.

    ``reason`` is a short human-readable phrase shown inline next to the row.
    ``keeper_hash`` is the sibling (better-quality / live tracker / etc) the
    rule recommends keeping. Empty string if no obvious sibling.
    ``factors`` is the same reasoning broken into structured pills so the UI
    can colour-code the deltas (added date, ratio, size, tier).
    ``severity`` is a row-level signal independent of the per-factor colours:
    ``warning`` tints the whole row yellow to flag "be careful confirming
    this one" (e.g. would trigger a freeleech penalty). Factor pills carry
    the *why* details; severity carries the row-level "stop and look" hue.
    """

    torrent_hash: str
    group_key: GroupKey
    reason: str
    keeper_hash: str = ""
    factors: tuple[ReasonFactor, ...] = field(default_factory=tuple)
    severity: Severity = "normal"


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


def _partition_by_season(group: Group, store: Store) -> list[list[Torrent]]:
    """Partition a group's torrents into per-season buckets for TV groups.

    TV shows can hold multiple seasons under one group; running a "best tier
    in the group" comparison across seasons wrongly flags e.g. an S02 1080p
    when an unrelated S01 2160p sits alongside. For TV groups we bucket by
    :func:`quick_season` (cheap regex), keeping torrents with no detectable
    season -- typically full-series packs -- in their own bucket so they
    only compare against other no-season torrents.

    Movie and OTHER groups return a single all-in bucket; season scoping is
    meaningless there.
    """
    torrents = [
        store.torrents[h] for h in group.torrent_hashes if h in store.torrents
    ]
    if group.kind is not GroupKind.TV:
        return [torrents]
    buckets: dict[int | None, list[Torrent]] = {}
    for t in torrents:
        buckets.setdefault(quick_season(t.name), []).append(t)
    return list(buckets.values())


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
        from qbit_filter.cleanup import factors as F

        out: list[Candidate] = []
        for key, group in store.groups.items():
            for bucket in _partition_by_season(group, store):
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
                                f"(added {_age_days(keeper.added_on, now)}d later)"
                            ),
                            keeper_hash=keeper.hash,
                            factors=tuple(factors),
                        )
                    )
        return out


# Window during which deleting a freeleech torrent typically incurs a
# tracker-side penalty. Used by ``DuplicateSameQualityRule`` to mark
# candidates whose pair is *both* freshly added, since dropping the newer
# one before this window can cost upload credit even when the older copy
# still seeds. Conservative default; per-tracker freeleech awareness is a
# followup (see plan: out-of-scope).
_FREELEECH_PENALTY_WINDOW_DAYS = 10


@dataclass(frozen=True, slots=True)
class DuplicateSameQualityRule:
    """Within one group, multiple torrents at the same quality tier.

    Keep the oldest (it has the longest seed history and is least likely to
    trigger a freeleech penalty if removed); flag every newer same-tier
    copy. Complementary to :class:`SupersededQualityRule` -- where that one
    handles the cross-tier case, this one handles the same-tier case.

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
        "Multiple torrents at the same quality tier. Keep the oldest copy, "
        "flag the newer arrivals."
    )
    freeleech_window_days: int = _FREELEECH_PENALTY_WINDOW_DAYS

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        from qbit_filter.cleanup import factors as F

        base = now if now is not None else int(time.time())
        fl_cutoff = base - self.freeleech_window_days * 86_400
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for season_bucket in _partition_by_season(group, store):
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
                    bucket.sort(key=lambda t: t.added_on)
                    keeper = bucket[0]
                    for t in bucket[1:]:
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
                            0, (t.added_on - keeper.added_on) // 86_400
                        )
                        out.append(
                            Candidate(
                                torrent_hash=t.hash,
                                group_key=key,
                                reason=(
                                    f"duplicate {tier.value} "
                                    f"(added {gap_days}d after keeper)"
                                ),
                                keeper_hash=keeper.hash,
                                factors=tuple(factors),
                                severity=severity,
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
                factors: list[ReasonFactor] = [
                    ReasonFactor("age", f"{age}d old", "neutral"),
                    ReasonFactor("ratio", f"{t.ratio:.2f}", "bad"),
                ]
                if idle and not t.is_no_peers:
                    idle_age = _age_days(t.last_activity, now)
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
                    age = _age_days(t.added_on, now)
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
                age = _age_days(t.added_on, now)
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
                    cold_days = _age_days(t.last_activity, now)
                    cold_factor = ReasonFactor("cold", f"{cold_days}d", "bad")
                else:
                    if t.added_on >= cold_cutoff:
                        continue
                    if t.dlspeed > 0 or t.upspeed > 0:
                        continue
                    cold_days = _age_days(t.added_on, now)
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


@dataclass(frozen=True, slots=True)
class ArrImportBrokenRule:
    """arr downloaded the torrent but couldn't import the resulting file.

    arr's queue exposes ``trackedDownloadStatus`` (``ok`` / ``warning`` /
    ``error``) and a ``statusMessages`` array that carries the actual
    diagnostic ("No files found are eligible for import", "Sample file too
    small", "Permission denied", etc). When either signal is non-ok the
    torrent is by definition dead weight: arr has explicitly given up on
    using it, but the file keeps sitting on disk seeding to nobody useful.

    Deterministic signal -- no thresholds, no subjective scoring. We only
    surface what arr already decided was broken.
    """

    slug: str = "arr-import-broken"
    label: str = "arr: import broken"
    description: str = (
        "Radarr/Sonarr reports an import problem (statusMessages or "
        "trackedDownloadStatus=warning/error). The file is on disk but arr "
        "can't use it -- safe-deletion candidate."
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
                if match is None:
                    continue
                # Either an explicit status message OR a non-ok tracked
                # download status counts. Many arr versions populate one
                # but not the other depending on the failure mode.
                has_messages = bool(match.queue_status_messages)
                bad_status = match.queue_tracked_status.lower() in {
                    "warning", "error"
                }
                if not (has_messages or bad_status):
                    continue
                first_msg = (
                    match.queue_status_messages[0]
                    if match.queue_status_messages
                    else f"arr status: {match.queue_tracked_status or 'unknown'}"
                )
                # Reason chip is short; the full list lives in the row's
                # title attribute via the template.
                short = first_msg if len(first_msg) <= 80 else first_msg[:77] + "..."
                factors: tuple[ReasonFactor, ...] = (
                    ReasonFactor(
                        "arr",
                        f"{match.source} import broken",
                        "bad",
                    ),
                    ReasonFactor(
                        "status",
                        match.queue_tracked_status or "warning",
                        "warning",
                    ),
                )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=short,
                        factors=factors,
                        severity="warning",
                    )
                )
        return out


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
                age = _age_days(t.added_on, now)
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
