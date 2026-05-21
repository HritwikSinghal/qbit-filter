"""Slug-keyed registry of all cleanup rule presets. The web layer uses this to
populate the rule-selector chrome and to dispatch `/rules/{slug}/preview` /
`/rules/{slug}/apply`."""

from __future__ import annotations

import logging

from qbit_filter.cleanup.rules import (
    CrossSeedDuplicateRule,
    DeadTrackerRule,
    OrphanedOnDiskRule,
    PathCollisionRule,
    RatioMetAndColdRule,
    Rule,
    StalledAndOldRule,
    SupersededQualityRule,
)

logger = logging.getLogger(__name__)

RULES: tuple[Rule, ...] = (
    SupersededQualityRule(),
    StalledAndOldRule(),
    RatioMetAndColdRule(),
    DeadTrackerRule(),
    CrossSeedDuplicateRule(),
    OrphanedOnDiskRule(),
    PathCollisionRule(),
)

BY_SLUG: dict[str, Rule] = {r.slug: r for r in RULES}


def _probe(rule: Rule) -> bool:
    """Run ``rule.candidates`` against an empty :class:`Store`. Returns
    False on :class:`NotImplementedError` or any other unexpected error so
    a broken rule disables itself in the UI instead of 500ing the rule
    bar. The exception is logged for debuggability."""
    from qbit_filter.state.store import Store

    try:
        rule.candidates(Store())
    except NotImplementedError:
        return False
    except Exception:  # noqa: BLE001 -- rules are user-extension points
        logger.exception("rule %s raised during is_implemented probe", rule.slug)
        return False
    return True


# Cache results at import time. Rule implementations are pure dataclasses
# with no per-request state, so the probe result never changes for a given
# RULES tuple. Without this cache, ``list_rules`` did 2x the work per
# /rules GET (probe + real call), which is O(groups x torrents) each.
_IMPLEMENTED: dict[str, bool] = {r.slug: _probe(r) for r in RULES}


def is_implemented(rule: Rule) -> bool:
    return _IMPLEMENTED.get(rule.slug, False)
