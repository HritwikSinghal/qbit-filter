"""Form-data -> FilterState helpers. Filters are frozen; helpers return new copies."""

from __future__ import annotations

from typing import Literal

from qbit_filter.domain import FilterState, TorrentStatus

Facet = Literal[
    "status", "category", "tag", "tracker", "search", "min_torrents",
    "not_status", "not_category", "not_tag", "not_tracker",
    "arr_monitored", "arr_cutoff", "not_arr_tag",
]
_FACETS: frozenset[str] = frozenset(
    {
        "status", "category", "tag", "tracker", "search", "min_torrents",
        "not_status", "not_category", "not_tag", "not_tracker",
        "arr_monitored", "arr_cutoff", "not_arr_tag",
    }
)

_ARR_MONITORED_VALUES: frozenset[str] = frozenset(
    {"any", "monitored", "unmonitored", "orphan"}
)
_ARR_CUTOFF_VALUES: frozenset[str] = frozenset({"any", "met", "unmet"})


def is_facet(value: str) -> bool:
    return value in _FACETS


def toggle(fs: FilterState, facet: str, value: str) -> FilterState:
    """Return a new :class:`FilterState` with ``value`` toggled in ``facet``.

    For ``search``, ``value`` replaces the current text (empty string clears).
    ``not_*`` facets toggle into the corresponding negative set; the inverse
    set is automatically cleared of the same value so include/exclude are
    mutually exclusive (clicking exclude on an already-included value flips it).
    Unknown facets return the original state unchanged.
    """
    if facet == "search":
        return _replace(fs, search=value)

    if facet in ("status", "not_status"):
        try:
            status = TorrentStatus(value)
        except ValueError:
            return fs
        if facet == "status":
            statuses = set(fs.statuses) ^ {status}
            return _replace(
                fs,
                statuses=frozenset(statuses),
                not_statuses=frozenset(fs.not_statuses - {status}),
            )
        not_statuses = set(fs.not_statuses) ^ {status}
        return _replace(
            fs,
            not_statuses=frozenset(not_statuses),
            statuses=frozenset(fs.statuses - {status}),
        )

    if facet in ("category", "not_category"):
        v = value.lower()
        if facet == "category":
            cats = set(fs.categories) ^ {v}
            return _replace(
                fs,
                categories=frozenset(cats),
                not_categories=frozenset(fs.not_categories - {v}),
            )
        not_cats = set(fs.not_categories) ^ {v}
        return _replace(
            fs,
            not_categories=frozenset(not_cats),
            categories=frozenset(fs.categories - {v}),
        )

    if facet in ("tag", "not_tag"):
        if facet == "tag":
            tags = set(fs.tags) ^ {value}
            return _replace(
                fs,
                tags=frozenset(tags),
                not_tags=frozenset(fs.not_tags - {value}),
            )
        not_tags = set(fs.not_tags) ^ {value}
        return _replace(
            fs,
            not_tags=frozenset(not_tags),
            tags=frozenset(fs.tags - {value}),
        )

    if facet in ("tracker", "not_tracker"):
        if facet == "tracker":
            trks = set(fs.trackers) ^ {value}
            return _replace(
                fs,
                trackers=frozenset(trks),
                not_trackers=frozenset(fs.not_trackers - {value}),
            )
        not_trks = set(fs.not_trackers) ^ {value}
        return _replace(
            fs,
            not_trackers=frozenset(not_trks),
            trackers=frozenset(fs.trackers - {value}),
        )

    if facet == "min_torrents":
        try:
            requested = int(value)
        except (TypeError, ValueError):
            return fs
        new_min = 1 if (requested <= 1 or requested == fs.min_torrents) else requested
        return _replace(fs, min_torrents=new_min)

    if facet == "arr_monitored":
        if value not in _ARR_MONITORED_VALUES:
            return fs
        # Tri-state: clicking the active value clears it back to "any".
        new_value = "any" if value == fs.arr_monitored else value
        return _replace(fs, arr_monitored=new_value)

    if facet == "arr_cutoff":
        if value not in _ARR_CUTOFF_VALUES:
            return fs
        new_value = "any" if value == fs.arr_cutoff else value
        return _replace(fs, arr_cutoff=new_value)

    if facet == "not_arr_tag":
        # Toggle: clicking an active exclusion removes it. Empty / blank
        # values are no-ops so a misclick doesn't poison the set.
        v = value.strip()
        if not v:
            return fs
        not_arr_tags = set(fs.not_arr_tags) ^ {v}
        return _replace(fs, not_arr_tags=frozenset(not_arr_tags))

    return fs


def set_search(fs: FilterState, text: str) -> FilterState:
    return _replace(fs, search=text.strip())


def clear() -> FilterState:
    return FilterState()


def active_count(fs: FilterState) -> int:
    n = (
        len(fs.statuses) + len(fs.categories) + len(fs.tags) + len(fs.trackers)
        + len(fs.not_statuses) + len(fs.not_categories)
        + len(fs.not_tags) + len(fs.not_trackers)
    )
    if fs.search:
        n += 1
    if fs.min_torrents > 1:
        n += 1
    if fs.arr_monitored != "any":
        n += 1
    if fs.arr_cutoff != "any":
        n += 1
    n += len(fs.not_arr_tags)
    return n


def _replace(
    fs: FilterState,
    *,
    statuses: frozenset[TorrentStatus] | None = None,
    categories: frozenset[str] | None = None,
    tags: frozenset[str] | None = None,
    trackers: frozenset[str] | None = None,
    not_statuses: frozenset[TorrentStatus] | None = None,
    not_categories: frozenset[str] | None = None,
    not_tags: frozenset[str] | None = None,
    not_trackers: frozenset[str] | None = None,
    search: str | None = None,
    min_torrents: int | None = None,
    arr_monitored: str | None = None,
    arr_cutoff: str | None = None,
    not_arr_tags: frozenset[str] | None = None,
) -> FilterState:
    return FilterState(
        statuses=fs.statuses if statuses is None else statuses,
        categories=fs.categories if categories is None else categories,
        tags=fs.tags if tags is None else tags,
        trackers=fs.trackers if trackers is None else trackers,
        not_statuses=fs.not_statuses if not_statuses is None else not_statuses,
        not_categories=fs.not_categories if not_categories is None else not_categories,
        not_tags=fs.not_tags if not_tags is None else not_tags,
        not_trackers=fs.not_trackers if not_trackers is None else not_trackers,
        search=fs.search if search is None else search,
        min_torrents=fs.min_torrents if min_torrents is None else min_torrents,
        arr_monitored=fs.arr_monitored if arr_monitored is None else arr_monitored,  # type: ignore[arg-type]
        arr_cutoff=fs.arr_cutoff if arr_cutoff is None else arr_cutoff,  # type: ignore[arg-type]
        not_arr_tags=fs.not_arr_tags if not_arr_tags is None else not_arr_tags,
    )
