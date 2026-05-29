"""Slug-keyed registry of all cleanup rule presets. The web layer uses this to
populate the rule-selector chrome and to dispatch `/rules/{slug}/preview` /
`/rules/{slug}/apply`.

Rules are auto-discovered: every module under ``cleanup/rules/`` that defines an
``ORDER: int`` and a ``RULE`` instance is picked up automatically, ordered by
``ORDER`` (slug as a stable tiebreak). Dropping a new ``cleanup/rules/<slug>.py``
registers it with no edit here -- this is the plugin extension point.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from qbit_filter.cleanup import rules as _rules_pkg
from qbit_filter.cleanup.types import Rule

logger = logging.getLogger(__name__)


def _discover() -> tuple[Rule, ...]:
    """Import every rule module under ``cleanup.rules`` and collect its ``RULE``
    instance, ordered by the module's ``ORDER`` constant. ``pkgutil`` lists the
    submodules without importing them; ``import_module`` runs each body (which
    constructs its ``RULE``). Modules missing ``RULE``/``ORDER`` -- or named with
    a leading underscore -- are skipped with a warning so a stray helper file in
    the package can't masquerade as a rule.
    """
    found: list[tuple[int, str, Rule]] = []
    seen_order: dict[int, str] = {}
    for mod_info in pkgutil.iter_modules(_rules_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_rules_pkg.__name__}.{mod_info.name}")
        rule = getattr(module, "RULE", None)
        order = getattr(module, "ORDER", None)
        if rule is None or order is None:
            logger.warning(
                "cleanup rule module %s missing RULE/ORDER; skipped", mod_info.name
            )
            continue
        if order in seen_order:
            logger.warning(
                "cleanup rule ORDER %d reused by %s and %s; ordering falls back "
                "to slug",
                order,
                seen_order[order],
                rule.slug,
            )
        seen_order[order] = rule.slug
        found.append((order, rule.slug, rule))
    found.sort(key=lambda item: (item[0], item[1]))
    return tuple(rule for _, _, rule in found)


RULES: tuple[Rule, ...] = _discover()

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
    except Exception:  # rules are user-extension points
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
