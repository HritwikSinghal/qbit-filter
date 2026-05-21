# Web UI Design Principles (Reusable)

A condensed, source-cited reference for designing intuitive web app interfaces.
Distilled from Material Design 3 / M3 Expressive guidance, dashboard-design
research, media-library UI patterns (Plex, Sonarr, Radarr), filter-UX best
practices, and WCAG 2.2 AA.

This document is project-agnostic. Copy or link it from any new project's
`docs/` and apply the relevant sections.

---

## 1. Information Hierarchy — the top-to-bottom skim test

Every screen should be skimmable in this order, each level readable in under a
second:

1. **Identity + system state** — who/what this is, is the system alive?
2. **Primary action (search / create)** — the fastest path to "I want one
   specific thing"
3. **Filters / facets** — the second-fastest path to "I want a subset"
4. **Active-filter / context summary** — what is currently being shown and why
5. **Sort + meta** — how the result list is ordered, how many items
6. **Data** — the actual content (cards, rows, grid)

If a user can't answer "what am I looking at and why?" without scrolling, the
hierarchy is wrong.

---

## 2. Material Design 3 + M3 Expressive

### Why M3 Expressive (2025+)
- Backed by 46 studies / 18,000+ participants
- UI elements identified **up to 4× faster** vs. previous M3
- Spring-based motion system, 35 new shapes with morphing, heavier typography,
  background blur for depth
- 87% preference among 18–24 yr olds; closes the performance gap between older
  and younger users

### Core M3 concepts to use
- **Dynamic color / color roles**: `primary`, `secondary`, `tertiary`,
  `error`, `surface`, `surface-variant`, `outline`, `on-*` pairs. Never
  hardcode hex; use the role tokens. Status colours come from these:
  | Status | M3 role |
  |---|---|
  | success / active | `primary` or `tertiary` |
  | warning / stalled | a warning role (custom) or `tertiary` w/ icon |
  | error | `error` |
  | inactive / paused | `surface-variant` / `on-surface-variant` |
- **Elevation levels 0–5**: 0 = flat surface, 1 = card resting, 3 = nav drawer,
  5 = dialog. Use sparingly; over-elevated UIs feel busy.
- **Shape scale**: `extra-small` 4dp → `extra-large` 28dp. Larger radius for
  larger / more "expressive" surfaces; sharper for dense data tables.
- **Typography scale**: display / headline / title / body / label.
  M3 Expressive favors **heavier weights** for headlines.
- **Window size classes** (responsive breakpoints):
  | Class | Width | Layout |
  |---|---|---|
  | Compact | <600 dp | single column, collapse filters to bottom-sheet |
  | Medium | 600–839 dp | single column, optional rail |
  | Expanded | 840–1199 dp | dual column or sidebar + content |
  | Large | 1200–1599 dp | sidebar + wide content |
  | Extra-large | ≥1600 dp | multi-column dashboards |

### Library reality (2026)
- **`@material/web`** (official) is in **maintenance mode**; no M3 Expressive support.
- **Beer CSS** — class-based, ~10× smaller, M3 + M3 Expressive supported, no
  build step, integrates cleanly with HTMX partial swaps.
- **mdui** — Web Components (~85KB gz), 30+ components + 10K Material icons,
  works without a build step. Shadow DOM is a small friction for HTMX.
- **Angular Material** if you're on Angular. **Vuetify** for Vue.

For Python + Jinja2 + HTMX stacks: Beer CSS is the path of least resistance.

---

## 3. Dashboard layout best practices

### Do
- **Card-based modular layout** — each card answers one question
- **Visual hierarchy** — size, weight, colour, spacing all signal priority
- **Whitespace is content** — gives the eye places to rest, reduces cognitive load
- **Limited, intentional colour** — colour means status; never decorative
- **Limited typography** — 2–3 sizes / 2 weights per page max
- **Iconography paired with labels** — never icon-only for primary actions

### Don't
- "Chart junk" — 3-D effects, gradients, decorative grid lines
- Mix of icon styles (filled + outlined + duotone)
- Charts that don't answer a user question
- Multiple competing colour palettes
- Three-deep menu nesting

### Chart picks
| Goal | Chart |
|---|---|
| Trend over time | Line |
| Compare across categories | Bar (horizontal if labels are long) |
| Parts of a whole, few slices | Pie / donut (sparingly) |
| Quantity in a grid (heatmaps, etc.) | Heat / matrix |

If a chip-count answers the same question as a chart, prefer the chip-count.

---

## 4. Filter UX (the area most apps get wrong)

### Decide first: single or multi-select?
- **Simple table, ≤5 facets** → single-select dropdowns
- **Large dataset / faceted search** → multi-select chips with AND between
  facets, OR within a facet (this is standard faceted filtering)
- **Never mix** chip set behaviours on one page (some single, some multi). Pick one.

### Required elements for a usable filter UI
1. **Show active filters prominently** — usually as removable chips above the
   data. Top pitfall in UX research: users apply filters, scroll, forget. Always
   surface state.
2. **One-click `Clear all`**
3. **Result count per filter option** (`Downloading 12`) — eliminates the
   "no results" trap; users see impact before clicking
4. **Visible result count after filtering** (`Showing 14 of 89`)
5. **Don't reorder chips on selection** — cognitive load spike
6. **Group chips by facet** — each facet gets its own labelled row; never a
   flat soup of 30+ chips
7. **Keep filter state across page refresh** — cookie or localStorage
8. **Persist on refresh** so the user doesn't lose their view

### Keep labels short
- ≤20 characters per chip
- Single words preferred (`movies` not `Movie torrents`)
- If unavoidable, use `max-width` + ellipsis + tooltip

### Apply-immediately vs. apply-on-confirm
- Cheap query / small dataset → **apply on every change** (chip click)
- Expensive query / large dataset → **batch with Apply button** that shows the
  resulting count: `Apply (124 results)`
- For multi-select chips with cheap queries (qbit-filter, most personal tools):
  apply immediately is the right default

### Search bar vs. filters
- Search is for "I want a specific known item"; filters are for "show me a
  subset of unknowns". Both are needed; put search above filters.

### Mobile
- Filters into a bottom-sheet behind a button showing the count: `Filters (3)`
- Active filters can be shown as a horizontally scrollable strip
- Touch targets ≥ 44 × 44 px (WCAG AA)

---

## 5. List / group views (media-library patterns)

Distilled from Plex, Sonarr/Radarr, Pulsarr, Petio, Ombi.

### Patterns that consistently work
1. **Posters/thumbnails when available** — visual scanning is faster than
   reading. If you can't get posters cheaply, use typography + status chips
   instead.
2. **Group by primary identity** (e.g. show title), then list members (e.g.
   seasons / torrents). Collapsed by default; one expanded at a time.
3. **Status badges on every item** — colour + icon + label. Never colour alone.
4. **Per-item action menu (kebab `...`)** rather than 3+ inline buttons.
   Reduces noise, keeps rows scannable.
5. **Bulk-select with a slide-in action bar** for power workflows. Reserve
   for v2 if it's a personal tool — but leave layout space for it.
6. **"Monitor" / "Wanted" / quality-profile per-item toggles** — Sonarr's
   power feature; only relevant for media-acquisition apps.
7. **Pipeline status indicators** — show data flow (search → download → import
   → media library) as a visual breadcrumb. Useful when many backends contribute.

### When grid vs. when list?
- **Grid**: poster-rich content, browse-mode ("what shall I watch?")
- **List**: dense metadata, action-mode ("what needs cleanup?"). Default for
  personal-tool dashboards.

---

## 6. Live data UX

If the page shows live data (SSE, WebSocket, polling):

- **Visible health indicator** (a small "live" dot or label) — user must trust
  the data is current
- **Reconnecting state** when stream drops — never silently stop
- **Backpressure plan**: if updates outpace the client, drop oldest and emit a
  one-shot full-resync event rather than blocking
- **Targeted swaps** (not whole-page re-render) — only repaint the affected
  card/row. SSE + HTMX OOB swaps are a clean shape for this.
- **Respect `prefers-reduced-motion`** — disable pulse / spinner animations

---

## 7. Accessibility — WCAG 2.2 AA (mandatory baseline)

- **Colour contrast** ≥ 4.5:1 for normal text, ≥ 3:1 for large text and icons
- **Touch targets** ≥ 24 × 24 px (WCAG 2.2 minimum); ≥ 44 × 44 px strongly
  recommended (Apple/Google guidance — 3× error rate below this)
- **Focus rings retained** — never `outline: none` without replacement
- **Status conveyed by more than colour** — colour + icon + text label
- **Real semantic HTML**: `<input type="checkbox">`, `<button>`, `<form>`,
  `<details>` over divs with click handlers
- **ARIA labels** on icon-only buttons
- **Skip-to-content link** at the top
- **`prefers-reduced-motion`** honored for any non-essential motion
- **`prefers-color-scheme`** honored for theme default
- **Keyboard nav**: every interactive element reachable by Tab; `Esc` closes
  overlays; arrow keys navigate lists; `Enter`/`Space` activates
- **Screen-reader announces** state changes (use `aria-live="polite"` on the
  result-count region, etc.)

EU: European Accessibility Act enforced since June 2025 — non-compliance
penalties up to €100,000. EN 301 549 v4.1.0 expected Q3 2026. For US: ADA Title III
plus Section 508 if anywhere near gov. Bake in AA from day one — retrofitting is brutal.

---

## 8. Dark mode

- **82% of mobile users prefer dark mode** when offered
- **14–58% power savings** on OLED screens
- **92% of top-tier apps** support system-wide dark themes in 2026
- **Don't just invert** — re-pick colours from the M3 dark palette; surfaces
  shift, not just text/background
- **Honour `prefers-color-scheme`** for the default, persist a user override
  in `localStorage`
- **Both themes must pass WCAG contrast**

---

## 9. Motion (M3 Expressive guidance)

- **Spring-based**, not linear — feels more physical
- **Short durations** (150–300ms) for interactions; longer (400–600ms) for
  layout changes
- **Easing**: standard ease-in-out is fine; M3 has named tokens
  (`emphasized`, `emphasized-decelerate`, `emphasized-accelerate`)
- **Stagger reveals** (~40ms apart) for lists appearing
- **Always honour `prefers-reduced-motion: reduce`** — animation off, or use
  cross-fade only

---

## 10. Empty states & errors

- **Never strand the user on a blank page**
- Empty state should: explain the cause, repeat the relevant context (e.g.
  active filters), offer a single clear CTA to recover ("Clear filters",
  "Add your first item")
- Errors: human-readable message + retry CTA + log/trace id for support
- **404/500 pages** also follow this pattern

---

## 11. Performance / perceived speed

- **First content visible** in <1s (server-render the shell + initial data)
- **Skeleton placeholders** for streamed/lazy content
- **Optimistic updates** for low-risk actions (button click → update UI
  immediately, reconcile on response)
- **Debounce live-search** ~150–250ms
- **Lazy-load** below-the-fold images (`loading="lazy"`)
- **Code-split** by route if the JS bundle exceeds ~150KB gzipped

---

## 12. Common pitfalls (UX research recurring failures)

| Pitfall | Fix |
|---|---|
| Users apply filters, scroll, forget which ones are active | Always-visible active-filter summary with `[x]` per chip |
| Icon-only buttons leave users guessing | Pair icon with text label; at minimum, `aria-label` + tooltip |
| Status conveyed by colour alone | Colour + icon + text |
| 20+ chips in a flat row | Group by facet; collapse low-frequency facets behind `…more` |
| 3+ inline action buttons per row | Kebab menu (`...`) |
| Live data with no health indicator | "live" indicator dot or label, plus reconnect state |
| Modal-heavy UI for filters/actions | Prefer inline / bottom-sheet patterns |
| Charts that don't answer a question | Replace with KPI chips or remove |
| Saved-views / preset features added early | YAGNI; only ship after a user actually asks |

---

## 13. Design-first checklist (use before writing markup)

- [ ] Identity + system state visible at top
- [ ] Search bar above filters
- [ ] Filters grouped by facet, each with a label
- [ ] Each filter option shows a result count
- [ ] Active-filters summary row with `[x]` per chip + `Clear all`
- [ ] Result count visible (`Showing N of M`)
- [ ] Sort control visible above the list
- [ ] List items are collapsed by default, one expandable at a time
- [ ] Status conveyed by colour + icon + label
- [ ] Per-row actions in a kebab menu, not inline buttons
- [ ] Empty state designed with active-filter context + recovery CTA
- [ ] Dark mode designed with M3 dark palette (not just inverted)
- [ ] Touch targets ≥ 44 × 44 px
- [ ] All chips/buttons keyboard-reachable
- [ ] `prefers-reduced-motion` honoured
- [ ] `prefers-color-scheme` honoured for default theme
- [ ] Responsive at compact / medium / expanded
- [ ] Live data has health indicator + reconnect state
- [ ] No icon-only primary actions
- [ ] No charts that don't answer a user question

---

## 14. Stack recommendations by toolchain

| Toolchain | MD3 implementation | Notes |
|---|---|---|
| Python + Jinja2 + HTMX | **Beer CSS** | No build step, plays well with partial swaps |
| Plain HTML / vanilla JS | **Beer CSS** or **mdui** | Beer if static-style, mdui if component-style |
| React | Angular Material is not for you; **MUI v6** is M3-ish but not full M3 Expressive. mdui with React works. | Watch for shadow-DOM/event quirks |
| Vue | **Vuetify** (full MD3) | Mature; large ecosystem |
| Angular | **Angular Material** | Officially MD3-aligned now |
| Flutter | First-party MD3 / M3 Expressive | Best M3 support of any framework |
| Native Web Components | **mdui** | Use directly as `<mdui-button>` etc. |

---

## 15. References

- [Material Design 3 — Develop for Web](https://m3.material.io/develop/web)
- [Material Design 3 — Foundations: Applying Layout (Window Size Classes)](https://m3.material.io/foundations/layout/applying-layout/window-size-classes)
- [Material Design 3 — Components catalog](https://m3.material.io/components)
- [Beer CSS — Material Design 3 framework](https://www.beercss.com/material-design-3)
- [mdui — Material Design 3 UI components (Web Components)](https://www.mdui.org/en/)
- [App Design Best Practices for 2026 — CatDoes](https://catdoes.com/blog/app-design-best-practices)
- [Dashboard Design Guide 2026 — createbytes](https://createbytes.com/insights/ultimate-guide-dashboard-design-best-practices)
- [PatternFly — Filters Design Guidelines](https://www.patternfly.org/patterns/filters/design-guidelines/)
- [15 Filter UI Patterns That Actually Work in 2026 — Bricx Labs](https://bricxlabs.com/blogs/universal-search-and-filters-ui)
- [Filter UX Design Patterns & Best Practices — Pencil & Paper](https://www.pencilandpaper.io/articles/ux-pattern-analysis-enterprise-filtering)
- [Setproduct — Chip UI Design](https://www.setproduct.com/blog/chip-ui-design)
- [Setproduct — Filter UI Design](https://www.setproduct.com/blog/filter-ui-design)
- [Eleken — Filter UX and UI for SaaS](https://www.eleken.co/blog-posts/filter-ux-and-ui-for-saas)
- [Aufait UX — Dashboard Filter Design Guide](https://www.aufaitux.com/blog/dashboard-filter-design-guide/)
- WCAG 2.2 Level AA — [W3C](https://www.w3.org/TR/WCAG22/)
