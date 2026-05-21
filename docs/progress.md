# Project: qbit-filter
> Last updated: 2026-05-21 | Session: 6

## Current state

Phase 9.0 (visual verification sweep) complete. Session 6 landed a
client-side perf hardening pass to address user-reported browser lag
under the full 1310-torrent / 622-group catalogue (six surgical edits
in `keys.js` + `custom.css`, see Significant Changes). Phase 9 Nix
packaging is still open.

Smoke verification baseline: 1310 torrents classified into 622 groups
against the live qBit. Initial `/` is a `StreamingResponse` (chrome
flushed first, group cards rendered in an 8-worker thread pool with a
16 KB flush buffer). SSE keeps the page warm. Browser-cache flow:
server `cache_mode` + client paint from `localStorage` both wired up;
cookie + `CACHE_VERSION` gates promotion.

Pickup priorities (in order):

1. **Verify the session-6 perf wins in a real browser.** Open the
   live page (full catalogue loaded) and take a DevTools Performance
   trace before/after. Expected: scroll Rendering time drops sharply
   (Fix 1 -- `contain-intrinsic-size`), Scripting on SSE-idle drops to
   near-quiet (Fixes 2 + 3 -- observer + marked-row guards), and
   selecting 100+ rows feels instant (Fix 4 -- selection Map). Note
   any remaining hot paths in a new session-7 entry. Until this is
   done, the fixes are unverified.

2. **Re-test browser-cache save reliability.** Fix 5 wrapped
   `localStorage.setItem` in `requestIdleCallback`, capped payload at
   2 MB, and bumped debounce 5 s -> 10 s. Combined with the existing
   `window.load` immediate save, the original ~50% cold-start flake
   should be resolved. Cold-start the page 5 times, confirm
   `localStorage[qf_groups_cache_v0]` is populated after each. If
   still flaky, the next lever is to drop the save entirely from
   `htmx:oobAfterSwap` and keep it only on `htmx:afterSettle`.

3. **Nix flake derivation (Phase 9.4).** Convert the
   `writeShellApplication` shim in `flake.nix` into a proper Python
   derivation. Then 9.3 / 9.5 / 9.6 fall in line.

4. **README + CLAUDE.md final pass (Phase 9.2).** Note: CLAUDE.md
   currently has uncommitted local edits (pre-existing, not from
   session 6) -- check `git diff CLAUDE.md` before editing.

5. **Tests backfill.** No `tests/` directory yet. CLAUDE.md says
   `python3 -m pytest tests/ -v` is required before commit, but
   there's nothing to run. Memory note `[[feedback_defer_tests]]`
   captures the intent: tests are deferred and run in a dedicated
   backfill pass. Pick this up after Nix packaging lands.

**Out-of-scope (deferred, lower priority):**
- Lazy-render torrent rows per group (would halve DOM count again;
  is a real refactor -- not needed if session-6 wins suffice).
- Server-side: batch per-tick OOB swaps into one fragment per group
  per tick (current bottleneck is client-side, not server payload).
- Replace per-row `border-bottom` with parent `gap` (small win,
  larger CSS churn).
- Move `Reconciler._rebuild` into `asyncio.to_thread` (needs queue
  refactor; `Subscription.queue` is `asyncio.Queue`, not thread-safe).

**Always run before commit:** `ruff check && mypy --strict`. (mypy
not on $PATH in dev shells; use `uv run mypy --strict src/qbit_filter`
or install via `uv tool install mypy`.) Test suite does not exist
yet -- see priority 5.

## Architecture (one-liners)

- `qbit/` is the only place that imports `qbittorrent-api`.
  `client.py` carries the IP-auth-bypass workaround (`LoginFailed` ->
  `app_version()`).
- `state/store.py` is mutated **only** by `state/reconciler.py`;
  everyone else reads.
- `state/views.py` is pure: snapshot in, filtered groups out.
- `Subscription` (one per SSE client) owns a `FilterState` + bounded
  4096-slot queue.
- Action endpoints call `qbit/actions.py` and return 204; the
  reconciler picks up the change on the next poll, so no
  double-write.
- The shared kebab menu lives once in `index.html` (`<div
  id="kebab-menu">`) and is positioned/populated by `static/keys.js`
  per click. Per-row inline menus were removed in session 4 (~1310
  inline kebabs / ~6 k hx-* attributes -- dominant scroll-jank cost).
- `cleanup/` houses the rule engine (registry + rule presets) over
  the store snapshot. Operator Console restyle (session 6) replaced
  Beer CSS with hand-rolled `tokens.css` + `custom.css`.

## Module inventory

```
src/qbit_filter/
  __init__.py  __main__.py  app.py  config.py  domain.py  py.typed
  qbit/        client.py  sync.py  actions.py
  grouping/    parser.py  grouper.py  quality.py
  state/       store.py  events.py  reconciler.py  views.py
               subscribers.py  viewport.py
  cleanup/     registry.py  rules.py
  web/         routes.py  render.py  filter_parse.py
               templates/  base.html  index.html  _sidebar.html
                           _filters.html  _active_filters.html
                           _group.html  _torrent.html  _empty.html
                           _rule_bar.html  _selection_bar.html
               static/     tokens.css  custom.css  favicon.svg
                           keys.js  livereload.js
scripts/       capture_fixtures.py  screenshot.py
docker:        Dockerfile  docker-compose.yml  .dockerignore
nix:           flake.nix  flake.lock  (writeShellApplication shim --
                                       9.4 still owes a real derivation)
```

## Plan

### Phase 1: Foundation
- [x] 1.1 Initialise git + skeleton
- [x] 1.2 `pyproject.toml` + package marker + `.python-version` + `uv sync`
- [x] 1.3 `domain.py` (GroupKey, Torrent, Group, FilterState, MainDataDelta, DomainEvent)
- [x] 1.4 `config.py` (pydantic-settings) + `.env.example`
- [x] 1.5 `qbit/client.py` with auth-bypass workaround

### Phase 2: Sync pipeline
- [x] 2.1 Capture qBit fixtures via `scripts/capture_fixtures.py`
- [x] 2.2 `qbit/sync.py` (`normalise` + `poll`)

### Phase 3: Grouping
- [x] 3.1 `grouping/parser.py` (guessit + normalise_title)
- [x] 3.2 `grouping/grouper.py` pure `assign()`

### Phase 4: State
- [x] 4.1 `state/store.py` canonical Store dataclass
- [x] 4.2 `state/events.py` EventBus fan-out
- [x] 4.3 `state/reconciler.py` (sole store mutator + event emission)
- [x] 4.4 `state/views.py` `apply_filters` + `count_by_facet`
- [x] 4.5 `state/subscribers.py` Subscription with bounded queue

### Phase 5: Read-only web UI
- [x] 5.1 `app.py` factory + lifespan + `__main__.py`
- [x] 5.2 base / _torrent / _group / _empty templates
- [x] 5.3 `_filters.html` + `index.html`
- [x] 5.4 Static assets (favicon, custom.css, keys.js)
- [x] 5.5 `web/routes.py` (`GET /` + `GET /healthz`)

### Bonus: Containerisation (out-of-plan)
- [x] Dockerfile (python:3.13-slim + uv from official image, non-root, 290 MB)
- [x] docker-compose.yml (host 8080 -> container 8765, env_file=.env, /healthz healthcheck)
- [x] .dockerignore + README

### Phase 6: SSE live updates
- [x] 6.1 SSE endpoint + per-client subscription + `web/render.py`
      (single + bulk OOB swaps, RESYNC coalescing, 15 s ping,
      refcounted bus membership)

### Phase 7: Filters
- [x] 7.1 `POST /filters`, `/filters/clear`, `/filters/search` +
      `_active_filters.html` + `filter_parse.py`
      (search, status, category, tag, tracker, min_torrents)

### Phase 8: Actions
- [x] 8.1 `qbit/actions.py` wrappers
      (pause / resume / recheck / delete with `purge` flag)
- [x] 8.2 Per-torrent + bulk routes (capped at 500 hashes per call).
      `app.state.qbit` exposes the live client.

### Phase 9: Polish + Nix packaging  ← in progress
- [x] 9.0 Visual verification sweep -- 8 screenshots, 6 regressions fixed
- [x] 9.1 Client-side perf hardening pass 2 (session 6) -- six edits
      in `keys.js` + `custom.css`; verification still pending
      (priority 1 in Current State)
- [ ] 9.2 README + CLAUDE.md final pass
- [ ] 9.3 Verify Python deps exist in nixpkgs
- [ ] 9.4 Proper `flake.nix` Python derivation (replace shim) + `nix flake lock`
- [ ] 9.5 Nix build verification + dep shims if needed
- [ ] 9.6 Final acceptance check (tick spec acceptance criteria)

## Status Summary
| Phase | Status | Progress |
|-------|--------|----------|
| Phase 1: Foundation | Done | 5/5 |
| Phase 2: Sync pipeline | Done | 2/2 |
| Phase 3: Grouping | Done | 2/2 |
| Phase 4: State | Done | 5/5 |
| Phase 5: Read-only web UI | Done | 5/5 |
| Bonus: Containerisation | Done | 3/3 |
| Phase 6: SSE live updates | Done | 1/1 |
| Phase 7: Filters | Done | 1/1 |
| Phase 8: Actions | Done | 2/2 |
| Phase 9: Polish + Nix packaging | In progress | 2/7 |

## Significant changes

### Client-side perf hardening pass 2 (2026-05-21, session 6)

Targeted the choke points causing browser lag when the catalogue is fully
loaded (smooth when empty, laggy when full). Six surgical edits in
`keys.js` + `custom.css`; no Python / template changes.

1. **`contain-intrinsic-size` was wrong.** `.group-card` had `120px`
   but real cards are 200-800 px tall, so `content-visibility: auto`
   kept re-measuring layout as cards scrolled into view. Switched to
   `auto 480px` so the browser caches per-element measured size after
   first paint.
2. **IntersectionObserver re-attach storm.** Viewport observer's
   `attach()` ran `querySelectorAll('.group-card')` (walks 622 cards)
   on every `htmx:oobAfterSwap`; under a RESYNC that fires dozens of
   times per second. Added a `WeakSet` of already-observed nodes and
   gated `attach` on the swap target being inside `#groups`.
3. **Marked-row scan storm.** `htmx:afterSwap` callback was walking
   all 1310 rows looking for `data-marked="true"` on every per-row OOB
   swap. Restricted to swaps that actually replace `#groups` /
   `rule-bar-slot`, and scoped `querySelectorAll` to the swapped
   subtree.
4. **`repaintSelectionFooter` per-hash `querySelector` storm.**
   Footer sum did `document.querySelector('.torrent-row[data-hash=X]')`
   per selected hash. Switched `selection` from `Set<hash>` to
   `Map<hash, bytes>` populated at check-time. Footer total is now an
   O(N) iteration of small in-memory values with no DOM access.
5. **localStorage cache write throttle.** `saveCacheNow` was a sync
   ~900 KB serialisation + `localStorage.setItem` on the main thread
   every 5 s. Wrapped in `requestIdleCallback`, added a 2 MB cap, and
   bumped debounce to 10 s.
6. **Dropped per-group stacking context.** `.group-card` had
   `overflow: hidden` purely for rounded-corner clipping, which forced
   622 stacking contexts. Replaced with explicit corner radii on
   `.group-meta` and `.torrent-row:last-child`.

### Perf hardening (2026-05-21)

1. **SSE subscription leak.** `routes.py` SSE `stream()` never
   removed the Subscription from the EventBus on disconnect. Every
   reload / tab close left a dead sub whose 4096-slot queue the
   reconciler kept filling. Fix: refcount live streams; `bus.add` on
   connect, `bus.remove + drain` on last disconnect. Multi-tab safe.
2. **Livereload polling.** `static/livereload.js` was a 500 ms
   forever-fetch per tab. Throttled to 5 s, paused on
   `document.hidden`, exponential backoff on failure, hard-stop on
   404. Gated by `dev_mode`.
3. **`apply_filters` per SSE tick.** Visible-set computation walked
   all 622 groups every tick. Replaced with per-group
   `views.group_matches()` (only the touched groups). RESYNC +
   `_oob_payload` share a single `apply_filters` pass.
4. **`count_by_facet` traversal.** Memoised by `store.rid`, so
   concurrent SSE clients and back-to-back filter POSTs reuse one
   ~1300-torrent traversal per poll tick.
5. **`guessit` double-parse.** `parse()` is now
   `lru_cache(maxsize=8192)`; the reconciler exposes one
   `_classify(t)` returning `(GroupKey, ParsedName)` so `_attach` no
   longer re-parses the name.

Deferred: moving `Reconciler._rebuild` into `asyncio.to_thread`.
`Subscription.queue` is an `asyncio.Queue` (not thread-safe), so
off-loading needs a queue refactor.

### UX expansion (2026-05-21)

- **Smooth animations** behind `prefers-reduced-motion` -- fade /
  translate entrance for group cards & rows, rotating chevrons,
  hover scale on `kind-icon`, drawer + theme-picker popover.
- **Kebab z-stacking** -- `group-card` was `overflow: hidden` which
  clipped the dropdown on the last row; switched to `overflow:
  visible` + per-child `border-radius` + a `:has(.kebab-menu.active)`
  rule so the active card lifts above siblings.
- **Theme picker** (later replaced in Operator Console restyle) --
  12-swatch picker, accent + dark/light persisted via `localStorage`.
- **Multi-torrent filter** -- `FilterState.min_torrents` (default 1).
  Sidebar toggle + removable chip in the active-filter strip.
- **TV seasons on group row** -- `grouping/parser.quick_season(name)`
  (regex pre-check, no guessit cost) + `views.seasons_of(group,
  store)` feed small `S0N` chips into the group summary. Capped at 8
  visible, then a `+N` chip. Movie / Other groups skip the work.
- **Multi-select + bulk actions** -- per-torrent checkbox + fixed
  bottom bulk bar. Bulk endpoints accept `|`-separated hashes (max
  500). Selection is a JS `Set` repainted after every
  `htmx:afterSwap` / `htmx:sseMessage` so it survives partial swaps.

### Streaming initial render (2026-05-21)

`GET /` previously buffered the full 622-group HTML (~900 KB) before
flushing. Now `StreamingResponse`:
1. yields head + app-bar + sidebar + filter chrome immediately,
2. submits group renders to an 8-worker `ThreadPoolExecutor`, awaits
   futures in input order, flushes per ~16 KB byte buffer,
3. yields the closing markup.

The marker lives in `index.html` (`<!--QF_STREAM_INSERT-->`),
surfaced only when context is rendered with `stream_mode=True`.
Chrome and group bodies share one template definition. Thread pool
helps under the GIL because Jinja's bytecode interpreter releases it
during C extension work.

`<script src="/static/keys.js">` moved from `base.html`'s `</body>`
(`defer`) to **inline before `<!--QF_STREAM_INSERT-->`** in
`index.html`. Under `StreamingResponse`, `defer` only fires after
the entire response lands, so click/kebab/bulk handlers were dead
for the 3-5 s the stream took to flush.

### Session 4 -- scroll perf + layout-shift + dark mode

- **Lazy / shared kebab menu** -- single largest scroll-jank win
  (see Architecture above).
- **`backdrop-filter: blur(8px)` removed** from `.app-bar.fixed`.
  Was forcing continuous re-rasterisation of every layer under the
  bar on scroll. Replaced with a solid `var(--surface-container)`.
- **`content-visibility: auto`** kept on `.group-card` only; removed
  the redundant row-level layer. `contain: layout style` stays on
  `.torrent-row` for cheap isolation.
- **Row layout-shift** -- `.torrent-meta` was `flex-wrap: wrap` and
  the meta line toggled an extra `<span>` whenever
  `dlspeed`/`upspeed` crossed zero, growing/shrinking row height by
  ~14 px per SSE tick. Fixes: `.torrent-row` pinned to `min-height:
  72px; height: 72px;`; `.torrent-meta` set to `flex-wrap: nowrap;
  overflow: hidden; white-space: nowrap;`; speed chips always
  rendered (`muted` with 0.35 opacity when zero); `font-variant-
  numeric: tabular-nums` on `.meta-added` / `.meta-ratio`.
- **New row fields** -- `Torrent.ratio: float` added to `domain.py`;
  `_fmt_date(ts)` Jinja filter in `app.py` renders unix seconds as
  `DD-MM-YYYY` local time; `_torrent.html` shows size, dl, up,
  category, added-date, ratio (dl/up dimmed when zero).
- **Active-filter strip "Showing 0 of 622" bug** -- `routes.py` now
  passes `visible_count=len(visible)` to the chrome render;
  `_active_filters.html` prefers it over `visible_groups|length`.
  Was zero because the chrome template runs with `visible_groups=[]`
  as a placeholder for streaming.

### Session 5 -- Phase 9.0 visual sweep + screenshot speed-up + browser-cache WIP

**Visual regressions fixed (6 in one pass):**
- **A. App-bar notch** -- `position: sticky` lived inside Beer CSS's
  `padding-inline-start: calc(var(--left) + 20rem)` body indent,
  leaving a 320 px notch. Fix: `position: fixed; left: 0; right: 0;
  width: 100%;` escapes the inline padding entirely.
- **B. Sidebar facet chip text clipping** -- `.chip-label {
  max-width: 14ch }` with the count badge ate the space. Fix:
  `.chip-label { flex: 1 1 auto; min-width: 0; }` + tighten
  `.facet-chip { padding: 0.35rem 0.55rem; gap: 0.35rem; max-width:
  100%; }`.
- **C/E. "Filters" header stacking + raised band** -- `<header
  class="sidebar-header">` was getting Beer's element-level header
  styling. Fix: swap to `<div class="sidebar-header">`.
- **D. Long filename overflowed 72 px row pin** -- `.torrent-name`
  used `-webkit-line-clamp: 2` and broke the pin. Fix: `white-space:
  nowrap; overflow: hidden; text-overflow: ellipsis;`.
- **F. Mobile gutter** -- A's fix solved this too. `padding-inline-
  start: 0 !important;` on `<body>` overrides Beer's `:has()` rule
  and reclaims mobile width.
- **Kebab hover padding** -- buttons had `border-radius: 0.5rem`
  inside a `padding: 0.35rem` container, leaving hover backgrounds
  floating inset with diamond seams. Fix: remove menu padding
  (`padding: 0; overflow: hidden;`) and button radius
  (`border-radius: 0`).

Also added `html { scroll-padding-top: var(--qf-appbar-height); }` --
Playwright clicks were stalling on elements scrolled under the bar.

**Screenshot script speed-up** (76 s -> ~18 s for 8 screenshots):
1. `_set_theme` is a body classList swap + localStorage write, not
   a page reload. Reload re-streamed 622 cards twice; classList swap
   is ~50 ms. Dominant cost.
2. `wait_for_selector(..., state="attached")` for the initial
   "groups appeared" check (default visibility wait stalled on the
   `qf-enter` entrance animation).
3. Inter-step waits reduced from 200-500 ms to 80-150 ms anchored
   on selectors.
4. Step 04 (kebab) uses `click(force=True)` only -- the implicit
   element-stable check in `scroll_into_view_if_needed` times out
   under SSE updates.

**Browser cache (partial):**

Picked localStorage + paint-from-cache, with "always show cache +
diff-update from SSE" (no full replace).

- **Server** (`routes.py`): `qf_has_cache=1` cookie +
  `CACHE_VERSION` constant. Cookie set AND `FilterState` default ->
  `cache_mode=True`: the response is chrome-only and an inline
  `<script>window.qfPaintCache()</script>` is emitted inside
  `#groups`. Cached visit: 30 KB vs 2.5 MB cold. Cookie max-age 7 d.
- **Server** (SSE handler): on every connect, push
  `DomainEvent(kind=RESYNC)` into the subscriber's queue. The
  renderer sends a full `#groups` OOB swap on the first message, so
  a client that painted stale localStorage HTML gets it replaced
  within ~100 ms.
- **Client** (`keys.js`): `window.qfPaintCache` -- DOMParser-based
  paint (no `innerHTML`; avoids the security hook and drops any
  stray `<script>` from corrupted cache). Saves `#groups.innerHTML`
  to `localStorage[qf_groups_cache_v{N}]` on `htmx:afterSettle`,
  `htmx:oobAfterSwap`, and `window.load`, debounced 750 ms. Filter
  mutations (POSTs to `/filters*`) clear cache + cookie via
  `htmx:configRequest` so a stale filtered view can never get
  promoted.
- **Cache version**: bump `CACHE_VERSION` in `routes.py` on
  incompatible group/row HTML schema changes; keys are versioned so
  old caches are ignored, not mis-painted.

## Decisions & Notes
<!-- Append entries as: YYYY-MM-DD: [decision or important note] -->
- 2026-05-21 (session 6): `selection` is now `Map<hash, bytes>`, not
  `Set<hash>`. Bytes captured at check-time from the row's
  `data-bytes` attribute. Reason: footer total summed bytes by doing
  `document.querySelector('.torrent-row[data-hash=X]')` per selected
  hash, which was O(N) DOM queries per checkbox click. With the Map,
  the footer is a pure in-memory sum. Touches: `change` handler,
  `repaintSelectionFooter`, `applyDelete`, the `htmx:afterSwap`
  marked-row pass.
- 2026-05-21 (session 6): `htmx:afterSwap` marked-row scanner is
  now gated on `target.id` being `groups` or `rule-bar-slot`
  specifically -- not the looser `target.querySelector('.group-card')`
  check. Reason: per-row OOB swaps from the SSE storm matched the
  loose check (any swap inside a group card has `.group-card` in
  its subtree) and triggered a 1310-row document scan per tick. The
  tighter check restricts the scan to swaps that actually replace
  the group payload.
- 2026-05-21 (session 6): `IntersectionObserver` viewport attach
  uses a `WeakSet<Element>` of already-observed cards plus a
  swap-target guard. Reason: the previous attach() walked all 622
  `.group-card` nodes per `htmx:oobAfterSwap` and re-called
  `observe()` on each; under a RESYNC that fired dozens of times
  per second. `observe()` is a no-op on already-observed nodes but
  the querySelectorAll walk was not free.
- 2026-05-21 (session 6): `localStorage` cache write deferred to
  `requestIdleCallback` with a 2 MB payload cap and a 10 s debounce.
  Reason: serialising ~900 KB of `#groups.innerHTML` and writing it
  synchronously every 5 s was stalling input for 50-150 ms per
  write. The original 750 ms debounce that flaked under SSE storm
  is gone -- 10 s is long enough to coalesce a RESYNC burst, and the
  `window.load` immediate save still guarantees a first snapshot
  before SSE storms in.
- 2026-05-21 (session 6): `.group-card` no longer has
  `overflow: hidden`. Reason: it was forcing 622 stacking contexts
  whose only job was clipping rounded corners. Corner clipping is
  now handled by per-child border-radius rules on `.group-meta`
  (top-left + bottom-left) and `.torrent-row:last-child` (bottom-
  right). Mobile breakpoint flips the radii to top + bottom.
- 2026-05-21 (session 6): `.group-card` `contain-intrinsic-size`
  changed from `120px` to `auto 480px`. Reason: 120 px was far
  smaller than a real card (200-800 px), so `content-visibility:
  auto` kept re-measuring layout as cards scrolled into view -- the
  exact thing the property is supposed to avoid. The `auto` keyword
  lets the browser remember per-element measured size after first
  paint.
- 2026-05-21 (session 5): App-bar switched from `position: sticky`
  to `position: fixed`. Beer CSS's
  `*:has(>nav.drawer.left:not(.s,.m,.l))` rule indents body by
  320 px to reserve column space for the drawer; a sticky bar sat
  inside that indent and produced a visible notch. Fixed bar
  escapes the body's inline padding. `body { padding-top:
  var(--qf-appbar-height); padding-inline-start: 0 !important; }`
  zeroes out Beer's drawer reservation across all breakpoints and
  reserves space for our fixed bar instead. `!important` is
  required because Beer's `:has()` specificity (0,5,1) outranks
  ours.
- 2026-05-21 (session 5): Browser cache uses a cookie
  (`qf_has_cache=1`, max-age 7 d) as the signal for the server to
  skip the group render. Localstorage holds the actual HTML
  payload. Cookie clearing on its own would just cost one full
  render -- the cache rebuilds on next save. Filter mutations clear
  cache + cookie so a filtered view never gets promoted to the
  cache slot. `CACHE_VERSION` constant in `routes.py` is the
  schema-bump knob: increment when the group/row template changes
  incompatibly so old caches are ignored, not painted.
- 2026-05-21 (session 5): SSE handler pushes `RESYNC` into the
  Subscription queue on connect. Reason: a returning client that
  painted cached HTML would otherwise show stale state until the
  reconciler's own RESYNC (only emitted on rebuild events -- can be
  minutes apart on a quiet qBit instance). Coalesced via
  `last_resync_at` so this doesn't fire twice if a real RESYNC is
  also queued. ~2.5 MB SSE message arrives ~100 ms after connect.
- 2026-05-21 (session 5): Screenshot script's `_set_theme` no
  longer reloads the page -- it toggles `document.body.classList`
  + writes `localStorage.qf_theme`. Reload-based theme switching
  re-streamed all 622 groups twice per run (~12 s); the classList
  swap is ~50 ms. Wall-clock for `scripts/screenshot.py` went from
  76 s to ~18 s.
- 2026-05-21 (session 4): Kebab menu moved from per-row inline to
  one shared floating element. Trade-off: each click does a single
  `getBoundingClientRect` + JS positioning. Win: ~800 KB of menu
  HTML + ~6 k hx-* attributes off the initial page. `<menu>` was
  first tried then swapped to `<div>` because Beer CSS shipped its
  own `menu` styling that interfered.
- 2026-05-21 (session 4): `<script>` tag must be inline in the body
  before the streaming marker, not deferred in `<head>` or at end
  of `<body>`. `defer` waits for DOMContentLoaded, which under
  `StreamingResponse` only fires when the *entire* response
  arrives, so interactivity was gated by full-page render.
  Documented at the inline `<script>` site in `index.html`.
- 2026-05-21 (session 4): Material Symbol `filter_drama` is a
  cloud, not a funnel. Brand mark uses `theaters` instead.
- 2026-05-21 (session 4): Added Playwright + Firefox as dev tooling
  (not runtime). Screenshots are the only honest way to evaluate
  visual changes; without them every CSS edit was guesswork.
- 2026-05-20: Chose FastAPI + Jinja2 + HTMX over SPA approaches --
  single-language Python toolchain, trivial Nix packaging, no Node
  build step. (Beer CSS used through session 5; replaced by
  hand-rolled `tokens.css` + `custom.css` in the Operator Console
  restyle.)
- 2026-05-20: `state/store.py` split out of original monolithic
  design after architecture-reviewer feedback -- now `store`
  (canonical), `views` (pure filters), `subscribers` (per-client).
  Reconciler is the sole store mutator.
- 2026-05-20: Auth-bypass workaround lives in `qbit/client.py`
  (not config) -- workaround is transport behaviour, not
  user-facing toggle.
- 2026-05-20: Grouping precedence: explicit tag (`tmdb:` / `imdb:`)
  > category override > guessit verdict > raw title fallback.
- 2026-05-20: TMDB enrichment deferred -- `enrichment/` slot
  reserved, tag-based hook (`tmdb:<id>`) already supported by
  grouper.
- 2026-05-20: Using `uv` for dev environment (lockfile + venv)
  during phases 1-8; Nix packaging deferred to phase 9. Faster
  iteration, decouples Python work from nixpkgs availability
  checks. Nix flake still ships as final artifact.
- 2026-05-20: `MainDataDelta.added` / `changed` typed as `dict[str,
  dict[str, object]]` (not bare `dict`) so `mypy --strict` passes.
- 2026-05-20: `Settings.model_config` adds
  `enable_decoding=False`. pydantic-settings v2 JSON-decodes
  complex env values (e.g. `frozenset[str]`) at the source layer
  before `field_validator(mode="before")` runs, so
  `MOVIE_CATEGORIES=movies,films` blew up. Disabling source-level
  decoding lets `_parse_csv` see the raw string.
- 2026-05-20: Added `src/qbit_filter/py.typed` (PEP 561) so
  downstream consumers and ad-hoc `mypy --strict scripts/...`
  invocations resolve our types. Hatchling packages it into the
  wheel automatically.
- 2026-05-20: Reconciler state-string map includes qBit 5.x's
  `stoppedUP` / `stoppedDL` aliases (renamed from `pausedUP` /
  `pausedDL`) so the live instance doesn't fall through to the
  `unknown -> ERRORED` default.
- 2026-05-20: `.env.example` defaults include common Sonarr /
  Radarr category names (`radarr`, `tv-sonarr`, `sonarr`) so an
  arr-managed library classifies correctly out of the box.
- 2026-05-20: Containerisation slotted in before Phase 6 (not in
  the original plan). Single-stage `python:3.13-slim` + uv from
  `ghcr.io/astral-sh/uv`, non-root user, `env_file=.env` at
  runtime (no rebuild on credential change). Host 8080 -> container
  8765.
- 2026-05-20: Added `flake.nix` early (precursor to Phase 9.4)
  with two run wrappers: `nix run .#qbit` (uv host run --
  `uv sync && uv run python -m qbit_filter`) and `nix run
  .#qbit-docker` (host docker -- `docker compose up --build`).
  These are `writeShellApplication` shims, NOT a full Nix
  derivation -- 9.4 still owes the proper package build.

## Blockers
<!-- List any active blockers. Remove the line when resolved. -->
