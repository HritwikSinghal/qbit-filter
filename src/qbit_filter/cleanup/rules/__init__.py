"""Cleanup-rule presets package.

Each rule lives in its own ``cleanup/rules/<slug>.py`` module declaring an
``ORDER: int`` and a ``RULE`` instance -- this is the plugin extension point:
dropping a new file registers a rule automatically (see
:mod:`qbit_filter.cleanup.registry`). This package ``__init__`` re-exports the
historic flat namespace so existing ``from qbit_filter.cleanup.rules import X``
imports keep resolving after the split.
"""

from __future__ import annotations

from qbit_filter.cleanup.rules.arr_cutoff_met_cold import ArrCutoffMetColdRule
from qbit_filter.cleanup.rules.arr_import_broken import ArrImportBrokenRule
from qbit_filter.cleanup.rules.arr_orphaned_tag import OrphanedArrTagRule
from qbit_filter.cleanup.rules.arr_unmonitored import ArrUnmonitoredCompletedRule
from qbit_filter.cleanup.rules.cross_seed_duplicate import CrossSeedDuplicateRule
from qbit_filter.cleanup.rules.dead_tracker import DeadTrackerRule
from qbit_filter.cleanup.rules.duplicate_same_quality import DuplicateSameQualityRule
from qbit_filter.cleanup.rules.orphaned_on_disk import OrphanedOnDiskRule
from qbit_filter.cleanup.rules.path_collision import PathCollisionRule
from qbit_filter.cleanup.rules.ratio_met_cold import RatioMetAndColdRule
from qbit_filter.cleanup.rules.stalled_and_old import StalledAndOldRule
from qbit_filter.cleanup.rules.superseded_quality import SupersededQualityRule
from qbit_filter.cleanup.types import (
    Candidate,
    FactorKind,
    ReasonFactor,
    Rule,
    Severity,
)

# Re-exported flat namespace: the cleanup types (now in cleanup.types) plus
# every rule class, so historic ``from ...rules import X`` imports resolve.
__all__ = [
    "ArrCutoffMetColdRule",
    "ArrImportBrokenRule",
    "ArrUnmonitoredCompletedRule",
    "Candidate",
    "CrossSeedDuplicateRule",
    "DeadTrackerRule",
    "DuplicateSameQualityRule",
    "FactorKind",
    "OrphanedArrTagRule",
    "OrphanedOnDiskRule",
    "PathCollisionRule",
    "RatioMetAndColdRule",
    "ReasonFactor",
    "Rule",
    "Severity",
    "StalledAndOldRule",
    "SupersededQualityRule",
]
