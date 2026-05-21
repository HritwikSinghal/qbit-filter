"""Per-subscription viewport: which group slugs the client currently sees,
plus 5-card overscan above/below. Used to filter SSE event emission so a
client only receives events for groups it can actually display.

Slugs (not :class:`GroupKey` instances) are stored because the key's
``year`` field can be resolved late by guessit -- if we cached the
pre-resolution key, the post-resolution key wouldn't match and the client
would silently stop seeing updates for a group still on screen. Slugs are
stable across that resolution because :meth:`GroupKey.slug` already
flattens the year into the string."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Viewport:
    hot: set[str] = field(default_factory=set)
    updated_at: float = 0.0


def merged_hot_set(viewports: dict[str, Viewport]) -> set[str]:
    out: set[str] = set()
    for v in viewports.values():
        out |= v.hot
    return out
