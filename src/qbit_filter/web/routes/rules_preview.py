"""Cleanup-rule selector + preview routes, plus the rule-bar / preview-context
builders that the SSE and filter modules reuse.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from qbit_filter.cleanup.registry import BY_SLUG, RULES, is_implemented
from qbit_filter.domain import FilterState, GroupKey
from qbit_filter.state.store import Store
from qbit_filter.state.subscribers import Subscription
from qbit_filter.state.views import apply_filters, group_matches, torrent_matches
from qbit_filter.web import render
from qbit_filter.web.routes._shared import get_or_create_subscription

router = APIRouter()


def render_rule_bar(
    request: Request,
    store: Store,
    fs: FilterState,
    active_slug: str = "",
) -> str:
    """Render the rule selector chips, intersecting each rule's candidates
    with the active filter so the count reflects what clicking the chip
    will actually surface. ``active_slug`` marks the currently-previewed
    rule (aria-pressed) so the chip stays visually pressed across SSE
    re-renders of the rule bar."""
    templates: Jinja2Templates = request.app.state.templates
    rows: list[dict[str, Any]] = []
    for r in RULES:
        impl = is_implemented(r)
        count = 0
        if impl:
            for c in r.candidates(store):
                t = store.torrents.get(c.torrent_hash)
                if t is None or c.group_key not in store.groups:
                    continue
                if not group_matches(store, fs, c.group_key):
                    continue
                if not torrent_matches(t, fs, store):
                    continue
                count += 1
        rows.append(
            {
                "slug": r.slug,
                "label": r.label,
                "description": r.description,
                "implemented": impl,
                "match_count": count,
                "active": r.slug == active_slug,
            }
        )
    return templates.get_template("_rule_bar.html").render(
        request=request, rules=rows, active_slug=active_slug
    )


def rule_preview_context(
    store: Store, sub: Subscription
) -> tuple[
    dict[GroupKey, dict[str, str]],
    dict[GroupKey, frozenset[str]],
    dict[GroupKey, dict[str, tuple[Any, ...]]],
    dict[GroupKey, dict[str, str]],
    list[GroupKey],
]:
    """Compute rule preview context for the subscription's active rule.

    Returns ``(marks_by_group, keepers_by_group, factors_by_group,
    severity_by_group, ordered_group_keys)``. When ``sub.active_rule_slug``
    is empty or the rule no longer exists, returns empty dicts and an empty
    list. ``ordered_group_keys`` is the title-sorted list of groups the
    rule matched -- used by ``/rules/{slug}/preview`` to scope the visible
    set to just the matching groups, but ignored by SSE renders that need
    rule context overlaid on the full visible set.
    """
    slug = sub.active_rule_slug
    if not slug:
        return {}, {}, {}, {}, []
    rule = BY_SLUG.get(slug)
    if rule is None or not is_implemented(rule):
        return {}, {}, {}, {}, []
    fs = sub.filter_state
    by_group: dict[GroupKey, dict[str, str]] = {}
    # Accumulate ALL keeper hashes per group, not just the first one. TV
    # groups now partition by season inside the rule (see
    # ``cleanup/scoring.partition_by_season``), so a multi-season show
    # produces one candidate per affected season -- each with its own
    # keeper. Storing a single keeper per group_key would silently hide
    # the per-season keepers for S01/S02 and leave only the S03 keeper
    # showing in the compare strip.
    keepers_acc: dict[GroupKey, set[str]] = {}
    factors_by_group: dict[GroupKey, dict[str, tuple[Any, ...]]] = {}
    severity_by_group: dict[GroupKey, dict[str, str]] = {}
    for c in rule.candidates(store):
        if c.group_key not in store.groups:
            continue
        if not group_matches(store, fs, c.group_key):
            continue
        t = store.torrents.get(c.torrent_hash)
        if t is None or not torrent_matches(t, fs, store):
            continue
        by_group.setdefault(c.group_key, {})[c.torrent_hash] = c.reason
        factors_by_group.setdefault(c.group_key, {})[c.torrent_hash] = c.factors
        if c.severity != "normal":
            severity_by_group.setdefault(c.group_key, {})[
                c.torrent_hash
            ] = c.severity
        if c.keeper_hash:
            keepers_acc.setdefault(c.group_key, set()).add(c.keeper_hash)
    keepers = {k: frozenset(v) for k, v in keepers_acc.items()}
    ordered = sorted(by_group.keys(), key=lambda k: store.groups[k].title.lower())
    return by_group, keepers, factors_by_group, severity_by_group, ordered


@router.get("/rules", response_class=HTMLResponse)
async def list_rules(
    request: Request,
    qf_sid: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Render the rule-selector strip. Match counts are computed eagerly
    for implemented rules so the chip can show "Superseded quality (12)"
    without a second roundtrip.

    Counts are intersected with the subscription's active filter so the
    chip number matches what clicking the chip will reveal -- a category
    filter on ``radarr`` should not advertise candidates that live in
    ``sonarr`` groups, since the user can't see them.
    """
    store: Store = request.app.state.store
    sub, _ = get_or_create_subscription(request, None, qf_sid)
    return HTMLResponse(render_rule_bar(request, store, sub.filter_state))


@router.post("/rules/{slug}/preview", response_class=HTMLResponse)
async def preview_rule(
    request: Request,
    slug: str,
    qf_sid: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Run one rule against the live store and render only the matching
    groups, with the candidate rows highlighted and reason chips inline.

    Clicking the currently-active rule chip clears the preview (toggle).
    The chosen slug is persisted on the subscription so SSE-driven
    re-renders (RESYNC + per-row TORRENT_CHANGED) keep the compare-strip
    layout instead of flattening back to the plain row stack.

    Intersects rule matches with the subscription's active
    :class:`FilterState` so that filtering down to e.g. category=radarr
    and then clicking a rule shows only candidates within that view --
    otherwise the selection footer would gain hashes from groups the
    user can't see, which is confusing and dangerous on bulk delete.

    The response also returns an OOB ``data-just-activated`` attribute
    on the staging marker so the client can do its one-shot
    bulk-auto-select on the freshly-rendered flagged rows without
    re-checking them on every subsequent SSE re-render.
    """
    store: Store = request.app.state.store
    rule = BY_SLUG.get(slug)
    if rule is None or not is_implemented(rule):
        raise HTTPException(
            status_code=404, detail=f"unknown or unimplemented rule: {slug}"
        )
    sub, _ = get_or_create_subscription(request, None, qf_sid)

    toggled_off = sub.active_rule_slug == slug
    if toggled_off:
        sub.active_rule_slug = ""
    else:
        sub.active_rule_slug = slug

    fs = sub.filter_state
    rule_bar_html = render_rule_bar(request, store, fs, sub.active_rule_slug)
    rule_bar_oob = (
        f'<div id="rule-bar-slot" hx-swap-oob="innerHTML">{rule_bar_html}</div>'
    )

    if toggled_off:
        # Clearing: show the standard unfiltered view (no marks). The
        # client-side `pendingRuleActivation` marker is NOT emitted so
        # afterSwap won't bulk-select anything.
        visible_groups = apply_filters(store, fs)
        html = render.render_groups_payload(
            request, store, fs, visible=visible_groups
        )
        return HTMLResponse(html + rule_bar_oob)

    by_group, keepers, factors_by_group, severity_by_group, ordered = (
        rule_preview_context(store, sub)
    )
    visible = [store.groups[k] for k in ordered]
    html = render.render_groups_payload(
        request,
        store,
        fs,
        visible=visible,
        rule_marks_by_group=by_group,
        rule_keepers_by_group=keepers,
        rule_factors_by_group=factors_by_group,
        rule_severity_by_group=severity_by_group,
        pre_check_flagged=True,
    )
    # Emit a one-shot marker INSIDE the #groups innerHTML swap so the
    # client knows THIS swap was triggered by a fresh rule activation
    # and should bulk-add flagged rows to the selection Map. Subsequent
    # SSE re-renders won't include the marker, so user edits to the
    # selection stick. Has to live inside the swap content because
    # ``hx-swap-oob`` targeting a non-existent id silently no-ops.
    activation_marker = (
        f'<span id="qf-rule-activation" data-slug="{slug}" hidden></span>'
    )
    return HTMLResponse(activation_marker + html + rule_bar_oob)
