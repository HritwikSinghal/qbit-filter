"""Pure view functions over the store. No mutation."""

from __future__ import annotations

from collections import Counter
from typing import Any

from qbit_filter.domain import FilterState, Group, GroupKey, GroupKind, Torrent
from qbit_filter.grouping.parser import quick_season
from qbit_filter.state.store import Store


def torrent_matches(t: Torrent, fs: FilterState) -> bool:
    """Per-torrent filter check. Used by cleanup-rule candidate filtering so
    the rule view honours the user's active filter chips at the torrent
    level, not just the group level."""
    # Negative filters first: any match here excludes the torrent.
    if fs.not_statuses and t.state in fs.not_statuses:
        return False
    if fs.not_categories and t.category.lower() in fs.not_categories:
        return False
    if fs.not_tags and (fs.not_tags & set(t.tags)):
        return False
    if fs.not_trackers and (fs.not_trackers & set(t.trackers)):
        return False
    # Positive filters: must match at least one value per active facet.
    if fs.statuses and t.state not in fs.statuses:
        return False
    if fs.categories and t.category.lower() not in fs.categories:
        return False
    if fs.tags and not (fs.tags & set(t.tags)):
        return False
    return not (fs.trackers and not (fs.trackers & set(t.trackers)))


def _group_passes(group: Group, store: Store, fs: FilterState) -> bool:
    if fs.min_torrents > 1 and len(group.torrent_hashes) < fs.min_torrents:
        return False
    search = fs.search.strip().lower()
    if search and search not in group.title.lower():
        return False
    has_any = (
        fs.statuses or fs.categories or fs.tags or fs.trackers
        or fs.not_statuses or fs.not_categories
        or fs.not_tags or fs.not_trackers
    )
    if not has_any:
        return True
    for h in group.torrent_hashes:
        t = store.torrents.get(h)
        if t is not None and torrent_matches(t, fs):
            return True
    return False


def apply_filters(store: Store, fs: FilterState) -> list[Group]:
    """Groups whose title matches ``fs.search`` and contain >=1 matching torrent.

    Faceted: AND across facets, OR within a facet.
    """
    out = [g for g in store.groups.values() if _group_passes(g, store, fs)]
    out.sort(key=lambda g: g.title.lower())
    return out


def seasons_of(group: Group, store: Store) -> list[int]:
    """Return the sorted distinct season numbers found in a TV group.

    Empty for non-TV groups and for TV groups where no torrent name carries a
    parseable season token. Used by the group-row template to render small
    season chips alongside the title.
    """
    if group.kind is not GroupKind.TV:
        return []
    seen: set[int] = set()
    for h in group.torrent_hashes:
        t = store.torrents.get(h)
        if t is None:
            continue
        n = quick_season(t.name)
        if n is not None:
            seen.add(n)
    return sorted(seen)


def group_matches(store: Store, fs: FilterState, key: GroupKey) -> bool:
    """Cheap per-group visibility check. Use in SSE batch render to avoid
    re-running :func:`apply_filters` over all groups when only a handful
    were touched this tick.
    """
    group = store.groups.get(key)
    if group is None:
        return False
    return _group_passes(group, store, fs)


def count_by_facet(store: Store) -> dict[str, Any]:
    """Per-facet counts of all torrents in the store. Memoised by ``store.rid``
    so multiple SSE subscribers / interleaved filter POSTs within one poll
    tick reuse a single traversal of ~1300 torrents.

    Returns a dict with per-facet ``{value: count}`` mappings plus a few
    scalar fields used by toggles (``multi_groups`` = number of groups with
    more than one torrent).
    """
    if store.facet_cache is not None and store.facet_cache[0] == store.rid:
        return store.facet_cache[1]
    statuses: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    trackers: Counter[str] = Counter()
    for t in store.torrents.values():
        statuses[t.state.value] += 1
        if t.category:
            categories[t.category.lower()] += 1
        for tag in t.tags:
            if tag:
                tags[tag] += 1
        for tr in t.trackers:
            if tr:
                trackers[tr] += 1
    multi_groups = sum(1 for g in store.groups.values() if len(g.torrent_hashes) > 1)
    counts: dict[str, Any] = {
        "status": dict(statuses),
        "category": dict(categories),
        "tag": dict(tags),
        "tracker": dict(trackers),
        "multi_groups": multi_groups,
    }
    store.facet_cache = (store.rid, counts)
    return counts
