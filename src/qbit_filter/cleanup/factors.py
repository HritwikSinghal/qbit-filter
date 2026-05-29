"""Shared factor builders for cleanup rules.

Each rule narrates *why* it flagged a torrent through a tuple of
:class:`ReasonFactor` pills. Several factor shapes (age delta, size delta,
source / codec mismatch) recur across rules, so they live here as pure
functions over primitive inputs. Rules import what they need and append to
their own factor list.

Intentionally minimal: helpers stay narrowly typed and free of business
rules. Picking which factors to emit, and in what order, remains the
rule's job -- this module just removes the formatting boilerplate.
"""

from __future__ import annotations

from qbit_filter.cleanup.scoring import age_days
from qbit_filter.cleanup.types import FactorKind, ReasonFactor
from qbit_filter.domain import Torrent


def age_factor(
    ts: int,
    now: int | None = None,
    *,
    label: str = "age",
    kind: FactorKind = "neutral",
) -> ReasonFactor:
    return ReasonFactor(label, f"{age_days(ts, now)}d", kind)


def added_gap_factor(
    t_added: int,
    keeper_added: int,
    *,
    label: str = "added",
    kind: FactorKind = "bad",
) -> ReasonFactor:
    """Days between this torrent's add-time and the keeper's. Positive value
    means the torrent landed *after* the keeper; negative means before. The
    rendered string is always positive with an after / before suffix."""
    delta_days = max(0, abs(t_added - keeper_added) // 86_400)
    suffix = "after keeper" if t_added >= keeper_added else "before keeper"
    return ReasonFactor(label, f"{delta_days}d {suffix}", kind)


def size_delta_factor(
    t_size: int,
    keeper_size: int,
    *,
    threshold_gb: float = 0.1,
    label: str = "size",
    kind: FactorKind = "neutral",
) -> ReasonFactor | None:
    """``None`` when |delta| < ``threshold_gb`` so a rule can skip
    appending the pill for negligible differences (a few MB of metadata
    diff isn't useful to surface)."""
    delta_gb = (t_size - keeper_size) / 1_073_741_824
    if abs(delta_gb) < threshold_gb:
        return None
    sign = "+" if delta_gb >= 0 else ""
    return ReasonFactor(label, f"{sign}{delta_gb:.1f} GB vs keeper", kind)


def source_codec_factors(
    t: Torrent, keeper: Torrent
) -> tuple[ReasonFactor, ...]:
    """Surface release-attribute mismatches the user might want to weigh
    before deleting (e.g. keeping a WEB-DL because the BluRay has burned-in
    subs). Empty tuple when source *and* codec match the keeper, so callers
    can just splice the return into their factor list."""
    out: list[ReasonFactor] = []
    if t.quality.source and t.quality.source != keeper.quality.source:
        out.append(
            ReasonFactor(
                "source",
                f"{t.quality.source} vs {keeper.quality.source or '-'}",
                "neutral",
            )
        )
    if t.quality.codec and t.quality.codec != keeper.quality.codec:
        out.append(
            ReasonFactor(
                "codec",
                f"{t.quality.codec} vs {keeper.quality.codec or '-'}",
                "neutral",
            )
        )
    return tuple(out)


def ratio_redeemer_factor(
    t: Torrent, keeper: Torrent, *, label: str = "ratio"
) -> ReasonFactor | None:
    """Emit a "good" pill when the flagged torrent has a meaningfully better
    seed history than the keeper. Worth surfacing because the keeper logic
    is added-on based, so an older copy with a lousy ratio can win over a
    well-seeded newer one -- the user might override."""
    if t.ratio > keeper.ratio and t.ratio >= 1.0:
        return ReasonFactor(label, f"{t.ratio:.2f} vs {keeper.ratio:.2f}", "good")
    return None
