"""Leaf types shared across the cleanup-rule engine.

These live in their own module (importing nothing else from ``cleanup``) so
that :mod:`qbit_filter.cleanup.factors`, the per-rule modules under
``cleanup/rules/``, and :mod:`qbit_filter.cleanup.scoring` can all depend on
them without forming an import cycle. Historically they lived at the top of
``cleanup/rules.py``; the package split moved them here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from qbit_filter.domain import GroupKey
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
