"""Server-side render helpers used by SSE, filter POSTs, and action endpoints."""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from qbit_filter.domain import FilterState, Group, GroupKey, Torrent, tier_rank
from qbit_filter.state.store import Store
from qbit_filter.state.views import apply_filters, count_by_facet, seasons_of


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates  # type: ignore[no-any-return]


def _order_torrents_for_display(torrents: list[Torrent]) -> list[Torrent]:
    """Best quality first; among equal tiers, newest added_on first.

    Group-card rows are scanned top-to-bottom by humans triaging cleanup
    candidates, so the best available copy belongs at the top regardless of
    insertion order in ``Group.torrent_hashes``.
    """
    return sorted(
        torrents,
        key=lambda t: (tier_rank(t.quality.tier), t.added_on),
        reverse=True,
    )


def render_group(
    request: Request,
    group: Group,
    torrents: list[Torrent],
    *,
    store: Store | None = None,
    rule_marks: dict[str, str] | None = None,
) -> str:
    seasons = seasons_of(group, store) if store is not None else []
    return _templates(request).get_template("_group.html").render(
        request=request,
        group=group,
        torrents=_order_torrents_for_display(torrents),
        seasons=seasons,
        rule_marks=rule_marks or {},
    )


def render_torrent(request: Request, torrent: Torrent) -> str:
    return _templates(request).get_template("_torrent.html").render(
        request=request, torrent=torrent
    )


def render_groups_payload(
    request: Request,
    store: Store,
    fs: FilterState,
    visible: list[Group] | None = None,
    rule_marks_by_group: dict[GroupKey, dict[str, str]] | None = None,
) -> str:
    """Render the ``#groups`` inner HTML: active-filter strip + group cards.

    Returned blob is intended for ``hx-swap="innerHTML"`` into ``#groups``.
    Includes the active-filter strip swap via hx-oob? No -- to keep the
    contract simple, this caller returns the active-filter strip as a sibling
    via ``hx-swap-oob``. Routes layer composes both pieces.

    ``visible`` may be pre-computed by the caller to share one
    :func:`apply_filters` pass across the active-filter strip and group list.

    ``rule_marks_by_group`` is set by the cleanup-rule preview endpoint:
    ``{group_key: {torrent_hash: reason}}``. Rows whose hash is in the inner
    dict render pre-selected with the reason chip inline.
    """
    if visible is None:
        visible = apply_filters(store, fs)
    tpl = _templates(request)
    parts: list[str] = []
    if visible:
        for group in visible:
            rule_marks = (rule_marks_by_group or {}).get(group.key, {})
            parts.append(
                tpl.get_template("_group.html").render(
                    request=request,
                    group=group,
                    torrents=_order_torrents_for_display(store.torrents_in(group.key)),
                    seasons=seasons_of(group, store),
                    rule_marks=rule_marks,
                )
            )
    else:
        parts.append(
            tpl.get_template("_empty.html").render(
                request=request,
                filter_state=fs,
                total_torrents=len(store.torrents),
            )
        )
    return "".join(parts)


def render_active_filters(
    request: Request,
    store: Store,
    fs: FilterState,
    visible: list[Group] | None = None,
) -> str:
    if visible is None:
        visible = apply_filters(store, fs)
    return _templates(request).get_template("_active_filters.html").render(
        request=request,
        filter_state=fs,
        visible_groups=visible,
        total_groups=len(store.groups),
    )


def render_filter_facets(
    request: Request, store: Store, fs: FilterState
) -> str:
    counts = count_by_facet(store)
    return _templates(request).get_template("_filters.html").render(
        request=request, counts=counts, filter_state=fs
    )
