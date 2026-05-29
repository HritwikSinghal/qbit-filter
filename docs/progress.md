# Project: qbit-filter
> Last updated: 2026-05-30 | Session: 13 (P1 structural refactors)

## Current state

**Session 13 landed the four P1 structural refactors** on branch
`refactor/priority-3-structural` (4 commits, not yet merged to `master` or
pushed). ruff clean; mypy has only the two pre-existing
`arr/client.py:556,573` errors -- no new ones. Each refactor was verified
statically (ruff + mypy --strict) and via TestClient/import smokes; the
SSE-driven filter path and telemetry round-trip still want a manual boot
against the live qBit instance (see pickup priority 3).

- `ce8e71e refactor: split cleanup/rules.py into a pluggable package` --
  one rule per `cleanup/rules/<slug>.py`; `types.py` + `scoring.py` extracted;
  `registry.py` auto-discovers via `pkgutil`/`ORDER`. The plugin surface.
- `ea8c404 refactor: extract qBit telemetry off Store` -- new
  `state/telemetry.py` `Telemetry` dataclass via `Store.telemetry`;
  `cold_boot_*` stays on `Store`.
- `e0c2be2 refactor: split web/routes.py into a routes package` -- 8
  `APIRouter` modules under `web/routes/`; `register_routes` unchanged.
- `34c4d52 refactor: route filter changes through the SSE RESYNC path` --
  filter POSTs respond 204 + enqueue a coalesce-exempt `RESYNC_FILTER`.

**Session 12 (`b9dbe8e`) + housekeeping (`01a6f86`, `8e66d45`) precede
session 13 on `master`.** Session 12 was the responsive `minmax(0, 1fr)`
layout fix + plain row-click-to-toggle (CSS + JS only); detail in items 1-2
below.

**Session 12 detail (shipped in `b9dbe8e`):** two small UI fixes.

1. **Responsive layout fix.** The page was rendering at a fixed
   `mainW = 2734 px` regardless of viewport (1920 / 1440 / 1024 / 720
   all showed the same horizontal scrollbar). Root cause: `.torrent-row
   .name` carries `white-space: nowrap; text-overflow: ellipsis`, but
   the row's grid was `display: grid; grid-template-columns: 28px 88px
   1fr auto auto`. Bare `1fr` is `minmax(auto, 1fr)`, so the unbreakable
   name's min-content (1516 px for the worst-case row) expanded the
   `1fr` track past the card's intended width; that pushed `.group-card`
   wider, which pushed `.shell`'s second column wider, which expanded
   the viewport. Cascade: `1fr` -> `minmax(0, 1fr)` on `.shell` (desktop
   + mobile breakpoint), `.group-card` (desktop + mobile),
   `.torrent-row` (name column), `.compare-strip` (mobile). Verified
   in Firefox via playwright -- `scrollW == vw` at 1920/1440/1024/720;
   `.name` now ellipsises as designed.

2. **Plain row click toggles selection.** Previously a plain click on
   a row body only set focus; toggling required ctrl/cmd-click or the
   row checkbox. Per user request, the entire row is now a click target
   for both flat rows and rows inside the compare strip. Text-selection
   guard: at `mousedown` we capture cursor coords; at `click` we
   suppress the toggle when the user dragged (>4 px) OR when
   `window.getSelection()` contains text whose anchor is inside the
   row. This preserves copy-text behaviour on the torrent name, size,
   factor pills, and arr chips. `.torrent-row { cursor: pointer; }` for
   affordance; explicit `user-select: text` on `.name`, `.meta`,
   `.reason-factors`, `.factor` (and their compare-strip equivalents)
   guards against a future `user-select: none` higher up the tree.

**Session 12 (`b9dbe8e`) is the most recent feature work on `master`;**
the two trailing commits (`01a6f86`, `8e66d45`) are housekeeping only.
Session 11 quick-fix and session 10 sweep details are retained below for
context. ruff clean; mypy has the two pre-existing
`arr/client.py:556,573` errors.

**Session 11 quick-fix (`3c31de9 fix: extend Sonarr history walk for
arr-live chip`):** the "arr live" chip was already wired for both
Radarr and Sonarr in `ArrMatch.arr_current`, but
`_fetch_current_download_ids` was capped at 4 pages * 250 records. For
Radarr (one entity per movie) that covers any library; for Sonarr (one
entity per *episode*) it only saw the most recent ~1000 imports.
Playwright check via Firefox showed TV coverage at 59/633 rows (9.3%)
vs movies at 435/506 (86%). Raised Sonarr's `pages` cap to 40 and added
an early-stop when a full page yields zero new entity ids. Recheck:
TV coverage now 537/633 (84.8%), on par with movies.

**Recent feature work also already committed (not previously
tracked):**

- `025c607 fix: scope TV cleanup rules per season` -- superseded-quality
  and duplicate-same-quality previously compared every torrent in a TV
  group together, so an S01 2160p pack marked an S02 1080p release as
  superseded. Rules now partition TV groups by `quick_season` (with a
  full-series bucket) before keeper/loser logic. Movies unchanged.
- `b4999cf fix: keep newest duplicate and match TV titles via guessit`
  -- `DuplicateSameQualityRule` now keeps the **newest** copy at a tier
  (arr only re-grabs on upgrade/repack); reason text + gap calc reframed
  relative to the newer keeper. `arr/index.py` title fallback routed
  through guessit so TV releases without a year token strip release
  noise before lookup -- the old prefix-cut left
  `S01.1080p.WEB-DL...` glued and never matched `series_by_norm`.
- `bd671d5 feat: add header activity widget for service telemetry` --
  cold-boot loading card replaced by a persistent header chip exposing
  qBit + arr poller state (connect status, last poll, cycles,
  queue/history/match counts) and a rolling 16-line log, pushed via OOB
  swaps on every RESYNC. Distinguishes "not configured" from
  "configured but unreachable" via per-service
  attempted/fetched/error counters in `ArrSnapshot`/`ArrStore`. Stamps
  `cold_boot_total` up-front so the streaming progress bar has a stable
  denominator across PARTIAL resyncs. Threads per-season keepers as a
  frozenset so multi-season TV groups show one keeper per season
  instead of dropping S01/S02 when S03 has a keeper.
- `ab72615 feat: prefer arr-current file as duplicate keeper` --
  `arr/client.py` now fetches `downloadFolderImported` history per
  movie/episode and unions the resulting hashes into `ArrSnapshot` so
  every `ArrMatch` carries an `arr_current` flag sourced from the live
  imported file. `DuplicateSameQualityRule` promotes the arr-current
  torrent to keeper (falls back to newest when arr has no opinion);
  reason text picks based on actual age gap. Green "arr live" chip
  renders on plain rows and on both sides of the compare strip.
  New-group delta inserts switched to `beforeend` so existing cards
  stop jumping on insert; order restored on next RESYNC. uvicorn
  `reload_includes` widened to html/css/js for dev-mode reloads.

**Session 10 sweep headline fixes (committed `b78fdb1`):**

- **No more dict-iter-while-mutate race in `_rebuild_index_and_publish`**
  (`app.py:189`): now snapshots `tuple(store.torrents.values())`
  before passing to `build_index`. The reconciler yields the loop on
  `to_thread(_warm_parse_cache)` and the chunked cold-boot's
  `store.torrents.clear()` was free to fire between the snapshot and
  the iteration.
- **Cold-boot RESYNC storm gone.** `_arr_poller._QbitListener` ignores
  `RESYNC_PARTIAL` until `store.cold_boot_done` so the arr poller no
  longer fires a fresh `build_index` + bus.publish(RESYNC) fan-out per
  chunk -- one terminal RESYNC instead of N partial-triggered rebuilds.
- **Long-lived `httpx.AsyncClient`** owned by `_arr_poller` lifetime
  (was created per `fetch_once` tick, defeating connection pooling).
- **Uncancellable thread-calls bounded.** `asyncio.wait_for` wraps the
  `connect()` (15 s) and `sync_maindata` (30 s) `to_thread` calls so a
  hung qBit can't pin lifespan shutdown indefinitely.
- **qBit poll backoff.** `Store.qbit_consecutive_failures` + capped
  exponential sleep in `qbit/sync.py:poll()` (base * 2^min(N-1,6),
  cap 60 s). Healthy<->degraded transitions log to the activity panel.
- **Lifespan shutdown order**: cancel pollers, await -> drain SSE
  subscriber queues -> qBit logout -> persist parser cache.
- **Per-card `contain-intrinsic-size`** (`_group.html` computes
  `max(140, 56 * torrents + 12)`). Compare-strip cards (rule preview
  with keepers + flagged) opt out of `content-visibility: auto` via
  `.no-cv` class -- factor-pill wrapping makes their height
  unpredictable. `html, body { scrollbar-gutter: stable;
  overflow-anchor: auto }`. CLS measured at 0 unexpected drift on
  scroll under load.
- **MutationObserver race fixed.** htmx innerHTML swaps remove the
  old subtree (including the hashes we just auto-selected) and
  insert the new one in the same tick. The MO microtask fires AFTER
  afterSwap, so the auto-select would land first, then the MO would
  see those same hashes in `removedNodes` and delete them. Fix:
  re-check `document.getElementById('torrent-' + h)` before deleting
  -- a hash whose row was "removed-and-replaced" survives.
- **MutationObserver attach** now retries on `DOMContentLoaded` +
  `htmx:afterSwap` because `keys.js` is inlined BEFORE
  `<!--QF_STREAM_INSERT-->`, so `#groups` doesn't exist at IIFE-eval
  time.
- **Drop `rAF(applyBatchStaging)`**: outerHTML OOB swap replaces
  `#qf-batch-staging`; the rAF closure held a detached prior node and
  could orphan its children under cold-boot load.
- **`is_disconnected()` peek dropped** in the SSE handler. Generator
  cancellation handles disconnects via the existing `finally`.
- **`count_by_facet` cache key** now includes `store.arr.rid`. Arr
  poller swaps in `hash_to_arr` without bumping `store.rid`; stale
  cached arr-facet counts were possible.
- **`arr/index.py` title-fallback collision** logs a WARNING when a
  title-only match would re-claim an entity already linked via
  tag/queue/history. Helps debug "rule miscounts a torrent".
- **`keys.js` Escape unified**. Single chain:
  arr-history-dialog -> confirm-modal -> cheatsheet -> activity-panel
  -> filter-drawer -> clear-selection -> blur-search. Each branch
  calls `preventDefault` only when it consumes the key.
- **FilterState session-replay**. `_active_filters.html` emits the
  canonical FilterState as `data-qf-state` JSON. `keys.js`
  persists to `localStorage[qf_session_v1]` on every successful
  `/filters*` or `/rules/*/preview` POST. On SSE open, if the saved
  state is non-empty AND the server's current state (read from the
  same data-qf-state) is empty, the client re-POSTs via `htmx.ajax`
  so the chrome chip OOBs swap correctly. Pre-existing UX
  cliff: any uvicorn `--reload` (or process restart) drops in-memory
  Subscriptions; the user's open tab still showed pressed chips
  while the server delivered the unfiltered view. This now self-heals.
- **Debug logging baseline**. `qfLog` JS namespace gated on
  `?qf_debug=1` or `localStorage.qf_debug='1'`. Instruments
  MutationObserver pruning, batch-staging boundaries, rule
  activation, selection lifecycle. Server-side: structured DEBUG
  calls in `_poller`, `_QbitListener.notify`,
  `_rebuild_index_and_publish` (with timing), reconciler chunked
  cold-boot, events.bus.publish (subscriber count),
  subscribers._enqueue overflow, SSE stream open/close + batch
  boundaries, arr/sync timings, arr/index build_index via-counts.
  Set `LOG_LEVEL=debug` in `.env` to enable.
- **Prior session-9 work intact** (cold-boot streaming, arr poller
  ping-pong avoidance, DEV_MODE auto-reload) -- those still
  describe steady-state behaviour.

**Baseline.** ~1310 torrents -> ~622 groups on production qBit. `GET /`
is a `StreamingResponse`: head + chrome + sidebar flushed first, group
cards rendered in an 8-worker `ThreadPoolExecutor` with a 16 KB flush
buffer, marker at `<!--QF_STREAM_INSERT-->`. SSE keeps the page warm.
Empty first-paint affordance is the SSE-driven "Loading torrent
list..." progress block.

### Pickup priorities (in order)

> [x] Session 12 committed (`b9dbe8e`) -- the former priority 0 is done.

1. **Live-verify reliability changes against the real qBit instance.**
   The Playwright smokes hit a live ~1310-torrent catalogue and went
   green, but the cold-boot RESYNC suppression and httpx-pool reuse
   want eyes on a fresh boot:
   - Kill the server. Boot fresh. Watch the activity log: should see
     one "Linked N torrents" line, NOT N "Refreshed arr index" lines
     during cold-boot chunks.
   - Stop Radarr mid-session; confirm qBit panel keeps updating; arr
     logs WARNING "fetch failed"; recovery on Radarr return surfaces
     in the activity log.
   - Pull the LAN cable for 30 s; confirm `qbit_consecutive_failures`
     climbs, sleep grows (visible in debug log), "qBittorrent
     recovered after N failed poll(s)" appears on reconnect.

2. **Phase 11 follow-ups** (independently landable, same as session 9):
   - **Identity-based regrouping** -- merge qBit groups sharing a
     TMDB/TVDB id. Post-pass in `state/views.py` after `apply_filters`.
   - **Sonarr-aware season grid** -- replace `[S01][S02]` chips with
     `[S01 OK 10/10][S03 X 7/10]`. Needs `_season_grid.html` partial.
     Per-season keepers are already threaded through render
     (`bd671d5`), so the data hook is ready.
   - **Below-cutoff anti-rule** -- warn factor (yellow) when
     `SupersededQualityRule` would mark a 1080p but arr is searching
     for an upgrade. Not a separate rule chip.
   - **Open in Radarr/Sonarr deep-link** -- kebab menu item using
     `arr_meta.title_slug` + `radarr_url` / `sonarr_url`.

3. **[x] Structural refactors flagged by session-10 arch review (P1) --
   DONE (session 13, all four landed as separate commits):**
   - [x] `cleanup/rules.py` split into a pluggable `cleanup/rules/<slug>.py`
     package + `cleanup/types.py` + `cleanup/scoring.py`; `registry.py`
     auto-discovers via `pkgutil` ordered by each module's `ORDER`
     (`ce8e71e`). Adding a rule is now "drop a file in `cleanup/rules/`".
   - [x] `state/store.py` telemetry moved into a `Telemetry` dataclass
     (`state/telemetry.py`) reached via a non-optional `Store.telemetry`
     handle; `cold_boot_*` deliberately kept on `Store` (reconciler-owned,
     SSE-hot-path) (`ea8c404`).
   - [x] `web/routes.py` (1640 lines) split into a `web/routes/` package
     of `APIRouter` modules (page, sse, activity, rules_preview, filters,
     actions, arr_history, _shared); `register_routes` unchanged for
     `app.py` (`e0c2be2`). Pure code-move, no behaviour change.
   - [x] `_oob_payload` removed; filter POSTs now respond 204 and enqueue a
     coalesce-exempt `RESYNC_FILTER` so SSE delivers the re-render
     (`34c4d52`). Template `hx-swap` -> `none`; session-save retimed to the
     `#active-filters` OOB swap.
   - **Live-verify still owed:** the SSE-driven filter path and the
     telemetry round-trip were verified statically + via TestClient (no
     live qBit here). Re-run the manual boot checklist below against the
     real instance: filter toggles update `#groups` via SSE, rapid toggles
     don't blank the list, a toggle during a poller tick still applies,
     rule preview + bulk delete still work.

4. **Poster proxy `/poster/{src}/{id}`.** ~50 LOC async route in
   `web/routes.py`; pipes `httpx` -> `StreamingResponse`, strips API
   key. Only matters if this ever runs outside LAN.

5. **Nix flake derivation (Phase 9.4).** Replace the
   `writeShellApplication` shim with a proper Python derivation.
   Then 9.3 / 9.5 / 9.6 fall in line.

6. **README + CLAUDE.md final pass (Phase 9.2).**

7. **Tests backfill (task #4).** No `tests/` yet. Highest leverage:
   `scripts/test_event_loops.py` Playwright harness (formal version
   of the ad-hoc smokes in `/tmp/qf_smoke*.py` from session 10),
   `arr/index.py:build_index` (pure), `arr/client.py` (httpx-mock
   harness), `qbit/sync.py:_backoff_seconds`, the chunked cold-boot
   path. Memory `[[feedback_defer_tests]]` says tests are a dedicated
   pass -- when that lands, lock in the session-10 regressions.

### Known caveats

- **Sonarr lacks per-series cutoff.** `arr/index.py:_series_match`
  uses `episode_file_count >= total_episode_count` as a proxy. Real
  per-series cutoff needs a `/episode` walk; out of scope for v1.
- **Title-fallback false matches.** `ARR_TITLE_FALLBACK=true`
  (default) catches more but can mis-match "Avatar" (2009) against
  an unrelated entry. Set to `false` for strict downloadId-only
  matching; cost is pre-history torrents appear as orphans. Session
  10: `_claim` now logs a WARNING when title fallback re-claims an
  entity already linked via tag/queue/history -- useful diagnostic.
- **API key visible in browser** for posters. Acceptable on LAN; see
  pickup priority 5.
- **Two-poller fan-out.** qBit poller ticks ~1 s, arr poller ~60 s.
  Arr also re-indexes on qBit RESYNCs (NOT RESYNC_PARTIAL during
  cold-boot -- session 10 suppresses those to avoid a thundering herd).
- **`ArrStore` concurrent mutation.** Mitigated by atomic dict-
  reference swap on `hash_to_arr` (build new, assign whole). Other
  fields follow the same pattern.
- **FilterState lost on every uvicorn `--reload`.** Subscriptions
  live in process memory. Session-10 client-side replay
  (`localStorage[qf_session_v1]`) self-heals once SSE reconnects, so
  this is now a ~1 s flicker rather than a hard divergence -- but
  there's still a window where the user's tab shows old chips and the
  groups list rebuilds. Proper server-side persistence is deferred.
- **Pre-existing mypy errors** at `arr/client.py:556,573`. Not
  introduced by current session-11 work (the Sonarr-coverage edit
  shifted them down from 540,557). Future cleanup.

### Out-of-scope (deferred)

- Lazy-render torrent rows per group (DOM count halving; real
  refactor -- not needed if cold-boot streaming + session-6 wins
  perform well in practice).
- Server-side: batch per-tick OOB swaps into one fragment per group
  per tick (current bottleneck is client-side, not server payload).
- Move `Reconciler._rebuild` into `asyncio.to_thread` (needs queue
  refactor; `Subscription.queue` is `asyncio.Queue`, not thread-safe).
- Tag backfill (push `radarr:<id>` / `sonarr:<id>` to qBit when arr
  index establishes a match) -- worth doing once match quality is
  validated against real data.
- Per-episode `/episode` walk for honest Sonarr per-series cutoff.

### Conventions

- **Before commit:** `uv run ruff check src/qbit_filter` and `uv run
  mypy --strict src/qbit_filter`. ruff is clean on master; mypy has
  two pre-existing errors at `arr/client.py:556,573` (Item "None" of
  "Any | dict | None" has no attribute "get") -- don't introduce new
  ones.
- `python3 -m pytest tests/ -v` is the eventual gate; `tests/`
  doesn't exist yet (priority 7). Tests are a dedicated backfill
  pass -- do not try to run pytest as a per-change gate today.
- No emojis in any file (pre-commit hook enforces). Use `[OK]` /
  `[X]` / arrow `->` instead.

## Architecture (one-liners)

- `qbit/` is the only place that imports `qbittorrent-api`.
  `client.py` carries the IP-auth-bypass workaround (`LoginFailed` ->
  `app_version()`). Do NOT "fix" the try/except.
- `arr/` is the only place that imports `httpx`. `client.py` wraps
  Radarr/Sonarr `/api/v3`; `sync.py` polls; `index.py:build_index` is
  pure (snapshot + torrents -> dict[hash -> ArrMatch]). `models.py`
  carries trimmed dataclasses.
- `state/store.py` is mutated **only** by `state/reconciler.py`.
  `Store.arr` is a read-only handle to `ArrStore`, which is mutated
  only by `_arr_poller` in `app.py`. Separation rationale: qBit
  reconciler rebuilds `Torrent` snapshots whole every ~1 s while arr
  state changes minute-scale -- bolting arr fields onto `Torrent`
  would race the reconciler.
- `state/views.py` is pure: snapshot in, filtered groups out.
  `torrent_matches(t, fs, store=None)` -- optional `store` arg lets
  arr facets run; absent store + active arr facet -> "no match" (UI
  doesn't leak rows from a configured-but-loading arr store).
- `Subscription` (one per SSE client) owns a `FilterState` + bounded
  4096-slot queue + viewport + `last_resync_at` + `last_partial_at`.
- Action endpoints (`/torrents/{hash}/{action}`) call
  `qbit/actions.py`, return 204. Reconciler picks up the change on
  next poll; SSE updates naturally. No double-write.
- `cleanup/` houses the rule engine. `registry.py` auto-discovers rules
  via `pkgutil.iter_modules` over the `cleanup/rules/` package, ordering by
  each module's `ORDER: int` (slug tiebreak), and caches an `is_implemented`
  probe at import (a rule that raises `NotImplementedError` from
  `candidates(Store())` greys itself out instead of 500ing the bar). Each
  rule lives in its own `cleanup/rules/<slug>.py` declaring `ORDER` + a
  `RULE` instance -- adding a rule is "drop a file"; `rules/__init__.py`
  re-exports the historic flat namespace for back-compat. `types.py` holds
  the leaf types (`ReasonFactor`, `Candidate`, `Rule` Protocol, `FactorKind`,
  `Severity`); `scoring.py` holds shared selection helpers (`age_days`,
  `partition_by_season`, `pick_arr_current_keeper`); `factors.py` builds the
  `ReasonFactor` pills. Splitting `types` out broke the old
  `factors -> rules` import cycle. The rule set is the plugin surface.

  **Registered rules (12 total -- 8 live, 4 stubbed/greyed-out):**
  - Live: `superseded-quality`, `duplicate-same-quality`,
    `stalled-and-old`, `ratio-met-cold`, `arr-unmonitored`,
    `arr-cutoff-met-cold`, `arr-import-broken`, `arr-orphaned-tag`.
  - Stub (raise `NotImplementedError`): `dead-tracker` (needs
    tracker-status sync), `cross-seed-duplicate` + `path-collision`
    (need content-path capture), `orphaned-on-disk` (needs disk
    walker).
- The shared kebab menu lives once in `index.html` (`<div
  id="kebab-menu">`); `static/keys.js` positions + populates it per
  click. Per-row inline menus were removed -- they dominated
  scroll-jank.
- **Background tasks**: `app.py:_lifespan` spawns `_poller` (qBit,
  always) and `_arr_poller` (only when at least one arr URL is set).
  `_arr_poller` runs `arr_fetch_loop` + `qbit_reindex_loop`
  concurrently via `asyncio.gather`. Both publish RESYNC; SSE
  handler coalesces.
- **Cold-boot flow**: `apply()` detects first full_update
  (`store.rid == 0` and chunk-size threshold), calls
  `_rebuild_chunked`, which slices `delta.added` into chunks, parses
  each via the existing `_warm_parse_cache` (ProcessPoolExecutor at
  >=64 names), classifies + attaches inline (no per-torrent
  publish), bumps `store.rid`, publishes `RESYNC_PARTIAL`. Final
  chunk publishes `RESYNC`. Subsequent polls take the original
  one-shot path.

## Module inventory

```
src/qbit_filter/
  __init__.py  __main__.py  app.py  config.py  domain.py  py.typed
  qbit/        client.py  sync.py  actions.py
  arr/         client.py  sync.py  index.py  models.py
  grouping/    parser.py  grouper.py  quality.py  parse_cache.py
  state/       store.py  telemetry.py  events.py  reconciler.py  views.py
               subscribers.py  viewport.py  arr_store.py
  cleanup/     registry.py  types.py  scoring.py  factors.py
               rules/  __init__.py (re-export hub)  <slug>.py per rule
  web/         render.py  filter_parse.py
               routes/  __init__.py (register_routes)  _shared.py  page.py
                        sse.py  activity.py  rules_preview.py  filters.py
                        actions.py  arr_history.py
               templates/  base.html  index.html  _sidebar.html
                           _filters.html  _active_filters.html
                           _group.html  _torrent.html  _empty.html
                           _rule_bar.html  _selection_bar.html
                           _compare_strip.html  _confirm_delete.html
                           _arr_history_dialog.html  _kbd_cheatsheet.html
               static/     tokens.css  custom.css  favicon.svg
                           keys.js  livereload.js
scripts/       capture_fixtures.py  screenshot.py
docker:        Dockerfile  docker-compose.yml  .dockerignore
nix:           flake.nix  flake.lock  (writeShellApplication shim;
                                       9.4 still owes a real derivation)
```

## Status Summary

| Phase | Status | Progress |
|-------|--------|----------|
| Phase 1-8: Foundation, sync, grouping, state, UI, SSE, filters, actions | Done | 26/26 |
| Bonus: Containerisation | Done | 3/3 |
| Phase 9: Polish + Nix packaging | In progress | 2/7 |
| Phase 10: Rule-cleanup UX | Done | 8/8 |
| Phase 11: Radarr / Sonarr integration | Done | 6/6 |
| Phase 12: Cache removal + chunked cold-boot | Done | -- |
| Session 10: Reliability + UX sweep | Done (`b78fdb1`) | 12/12 |
| Session 10 features: per-season TV scoping, keep-newest dup, header activity widget, arr-current keeper | Done (`025c607`, `b4999cf`, `bd671d5`, `ab72615`) | 4/4 |
| Session 11: Sonarr arr-live coverage fix | Done (`3c31de9`) | 1/1 |
| Session 12: Responsive layout + row-click toggle | Done (`b9dbe8e`) | 2/2 |
| Session 13: P1 structural refactors (rules package, Telemetry, routes package, filters-via-SSE) | Done (`ce8e71e`, `ea8c404`, `e0c2be2`, `34c4d52`) | 4/4 |

Phase 9 remaining: 9.2 (README + CLAUDE.md final pass), 9.3 (verify
Python deps in nixpkgs), 9.4 (proper flake.nix derivation), 9.5 (Nix
build verification), 9.6 (final acceptance check).

Session 10 batch (closed this session): P0-1 snapshot store.torrents,
P0-2 wait_for on blocking thread-calls, P0-3 long-lived httpx client,
P0-5 suppress RESYNC_PARTIAL in arr listener, P0 cold-boot SSE-open
synthetic RESYNC -> PARTIAL, qbit_consecutive_failures + backoff,
lifespan drain SSE subs, arr bus listener hardening, P1-8 selection
MO prune, P1-9 drop rAF in applyBatchStaging, P1-10 relocate rule
activation marker, count_by_facet arr_rid cache key, arr title-
fallback collision warning, keys.js Escape unification, /sse drop
is_disconnected peek, UI layout-shift fix (per-card intrinsic-size +
.no-cv + scrollbar-gutter), FilterState session-replay via
data-qf-state, structured debug logging baseline (Python + JS).

## Decisions & Notes

<!-- Append as: YYYY-MM-DD: [decision]. Keep entries that still affect
     current behaviour; let git log own the rest. -->

- 2026-05-30 (session 13): P1 structural refactors, all four landed
  (`ce8e71e`, `ea8c404`, `e0c2be2`, `34c4d52`). Key decisions worth keeping:
  - Rule registry auto-discovery uses a per-module `ORDER: int` constant
    (sorted with `slug` as tiebreak), chosen over a registration decorator
    (fragile global mutable state under `--strict`) and a central ordered
    slug list (defeats "drop a file to add a rule"). `pkgutil.iter_modules`
    lists, `import_module` runs each body to collect its `RULE` instance;
    modules missing `RULE`/`ORDER` are skipped with a warning.
  - `cleanup/types.py` holds the leaf types (`ReasonFactor`, `Candidate`,
    `Rule` Protocol, `FactorKind`, `Severity`) so `factors.py` and the rule
    modules import from it -- this breaks the old `factors -> rules` cycle
    that forced the function-local `import factors as F` workaround.
  - `Telemetry` is qBit-only and NOT unified with `ArrStore`'s parallel
    telemetry (different mutator + cadence). `cold_boot_*` stays on `Store`
    (reconciler-owned, read in the SSE hot path); only the 6 poller-owned
    `qbit_*` fields moved. The handle is non-optional to avoid `arr`-style
    Optional-chaining noise.
  - `RESYNC_FILTER` is coalesce-exempt by design: a user filter click is
    rare vs poller ticks and must never be swallowed by the 1 s RESYNC
    window. It renders with full-RESYNC semantics (canonical slugs +
    data-final). Filter POSTs return 204; the client `hx-swap="none"` +
    SSE delivers the render. Rule preview was deliberately NOT migrated --
    it depends on a one-shot `#qf-rule-activation` marker in its HTML body.
  - Session-save (localStorage) retimed from the filter POST's
    `htmx:afterSettle` (now reads pre-change state, since the strip arrives
    via SSE) to the `#active-filters`/`#rule-bar-slot` `oobAfterSwap`, with
    a dirty-check so the per-RESYNC re-OOB doesn't thrash storage.
  - Branch `refactor/priority-3-structural`, not yet merged/pushed. No
    `tests/` dir still (priority 7) -- a registry slug-set/order regression
    test is the obvious first backfill.
- 2026-05-23 (session 12): Every grid track that hosts user-driven
  content must use `minmax(0, 1fr)`, not bare `1fr`. CSS resolves bare
  `1fr` to `minmax(auto, 1fr)`, which lets the track's min-content
  (e.g. a `white-space: nowrap` torrent name) expand the track past
  the available space and cascade upward through nested grids. The
  payoff for getting this right is `text-overflow: ellipsis` actually
  truncating, and the viewport not turning into a horizontal
  scrollbar. Touched grids: `.shell`, `.group-card`, `.torrent-row`,
  `.compare-strip`.
- 2026-05-23 (session 12): Click-to-toggle on torrent rows uses a
  mousedown-pos + window.getSelection() guard, not a brittle
  user-select: none escape hatch. Mousedown records `_mdX/_mdY`; click
  computes drag distance and inspects `getSelection()` for a non-empty
  range whose `anchorNode` is inside the row. Either signal suppresses
  the toggle, so click-to-toggle and drag-to-copy coexist without
  fighting each other. Rule keepers in the compare strip are togglable
  on plain click (consistent with the existing `x` keyboard shortcut,
  which also doesn't skip keepers); only auto-select paths
  (master-select, "select losers", invert, range) honour `data-keeper`.

- 2026-05-23 (session 11): `_fetch_current_download_ids` walks pages
  with an early-stop once a full page contributes zero new entity ids
  (descending-by-date means anything beyond is older imports already
  superseded by what we've recorded). Sonarr defaults to `pages=40`
  vs Radarr's `4` because Sonarr's entity is the *episode* -- a
  thousand-episode library blows past 4 * 250 records before the
  per-entity de-dup converges, leaving most TV rows without the "arr
  live" chip. Empirically TV coverage went 9.3% -> 84.8% on the live
  ~1310-torrent / 148-TV-group instance. The `len(records) <
  page_size` short-circuit catches the "fewer than a full page came
  back" case too.
- 2026-05-21 (session 10): `_rebuild_index_and_publish` snapshots
  `tuple(store.torrents.values())` before calling `build_index`. The
  reconciler yields on `to_thread(_warm_parse_cache)` and the chunked
  cold-boot's `self.store.torrents.clear()` was free to fire between
  the snapshot and the iteration in `build_index`, raising
  `RuntimeError: dictionary changed size during iteration` on a
  ~1-in-N race. The single-event-loop model wasn't enough -- explicit
  snapshot is required at every yield point that exposes the dict.
- 2026-05-21 (session 10): `_QbitListener.notify` ignores
  `RESYNC_PARTIAL` until `store.cold_boot_done`. Cold-boot emits one
  per 200-torrent chunk (every ~50-150 ms); reacting to each was a
  thundering herd of `build_index` + SSE fan-outs. One terminal
  RESYNC catches everything at the end of cold-boot.
- 2026-05-21 (session 10): SSE-open synthetic RESYNC is now
  `RESYNC_PARTIAL` when `not store.cold_boot_done`. A full RESYNC
  there carried `data-final=1` and the canonical slug list of the
  PARTIAL store, telling `applyBatchStaging` to prune any non-listed
  cards -- the user saw "Loading 47%" + an apparently complete card
  list.
- 2026-05-21 (session 10): `httpx.AsyncClient` owned by
  `_arr_poller` lifetime, not `fetch_once`. Per httpx docs, per-tick
  client creation defeats the connection pool and pays TLS handshake
  every cycle. Closed in `finally`.
- 2026-05-21 (session 10): Blocking `to_thread` calls bounded by
  `asyncio.wait_for`. `connect` -> 15 s, `sync_maindata` -> 30 s. The
  thread can't be killed, but the coroutine no longer waits past the
  timeout -- shutdown proceeds.
- 2026-05-21 (session 10): `qbit/sync.py:poll()` takes an optional
  `Store` and applies capped-exponential backoff
  (`base * 2^min(N-1, 6)`, cap 60 s) when ticks fail. Reset on first
  success. `_poller` logs healthy<->degraded transitions to the
  activity log so the UI surfaces a stuck qBit.
- 2026-05-21 (session 10): `applyBatchStaging` runs synchronously
  inside `htmx:oobAfterSwap`, NOT in a `requestAnimationFrame`. htmx
  outerHTML swap REPLACES `#qf-batch-staging`; the rAF closure was
  holding the detached prior node, and successive batches could
  orphan cards in unreachable detached subtrees.
- 2026-05-21 (session 10): MutationObserver on `#groups` prunes the
  selection Map on row removal -- BUT only when
  `document.getElementById('torrent-' + h)` confirms the row is
  truly gone. htmx innerHTML swap removes the old subtree and
  inserts a new one in the same tick; the MO microtask fires AFTER
  afterSwap, so without the live-DOM re-check the MO would delete
  the hashes auto-selected by the rule chip 31 ms earlier.
- 2026-05-21 (session 10): Per-card `contain-intrinsic-size` set in
  `_group.html` as `max(140, 56 * torrents + 12)`. The universal
  480 px placeholder caused 300+ px shifts as small single-torrent
  cards came into view. Compare-strip cards (rule preview) opt out
  of `content-visibility: auto` via `.no-cv` because factor-pill
  wrapping makes their height unpredictable; the rule-active subset
  is small enough that paint cost is negligible.
- 2026-05-21 (session 10): `count_by_facet` cache key includes
  `store.arr.rid` (or 0 when no arr store). Arr poller swaps in
  `hash_to_arr` WITHOUT bumping `store.rid`, so memoised arr-facet
  counts went stale until the next qBit poll mutation.
- 2026-05-21 (session 10): `_active_filters.html` emits
  `data-qf-state` JSON carrying the canonical FilterState. `keys.js`
  reads it as the source of truth for the saved session (scraping
  individual chip nodes missed `min_torrents` because that button
  has class `.toggle-pill`, not `.facet-chip`). On SSE open, if the
  saved state is non-empty and the current DOM shows empty server
  state, the client replays via `htmx.ajax('POST', ...)` so OOB
  swaps fire correctly and the chrome chips redraw.
- 2026-05-21 (session 10): Debug logging baseline. Set
  `LOG_LEVEL=debug` (Python) and `?qf_debug=1` or
  `localStorage.qf_debug='1'` (JS) to enable. Both are off by default
  to keep production console quiet. The JS namespace is `qfLog`
  (`window.qfLog.debug/info/warn/error`); errors and warnings
  always go through `console` regardless of the flag.
- 2026-05-21 (session 9, `13d289d`): Cold-boot is chunked, not
  cached. qBit's `sync/maindata` is single-shot HTTP; "batching"
  happens post-fetch. First cold-boot calls `_rebuild_chunked`,
  publishing `RESYNC_PARTIAL` per 200-torrent chunk so first paint
  doesn't wait for the full ~1310-torrent rebuild. Reconnect-time
  full_updates (rid > 0) keep the one-shot path -- a streamed
  mid-session rebuild would visibly flash the page.
- 2026-05-21 (session 9): `RESYNC_PARTIAL` bypasses the 1 s
  `RESYNC_COALESCE_INTERVAL` but honours `RESYNC_PARTIAL_MIN_INTERVAL`
  = 100 ms. Partials never set `data-final="1"` and don't carry
  canonical slugs, so the client doesn't prune cards about to land in
  the next chunk.
- 2026-05-21 (session 9): `_arr_poller` listens to its own bus.
  Before publishing arr-side RESYNC the listener removes itself from
  the bus, publishes, then re-adds -- avoids ping-pong on the RESYNC
  we just caused. Microsecond gap is fine in async code.
- 2026-05-21 (session 9): `DEV_MODE=true` in `.env` enables
  `uvicorn --reload` with `reload_dirs=["src/qbit_filter"]`. The
  existing livereload.js + `/dev/version` give the page reload.
- 2026-05-21 (session 8): `ArrStore` is a *separate* store from
  qBit's `Store`. `Store.arr: ArrStore | None` is a one-way pointer
  set during lifespan setup; rules / views / templates reach through
  it, never write. Match precedence: tag -> Radarr queue -> Sonarr
  queue -> Radarr history -> Sonarr history -> title+year fallback
  (gated by `ARR_TITLE_FALLBACK`).
- 2026-05-21 (session 8): Poster URLs hot-link from arr with API key
  in query string. Acceptable on LAN; `/poster/{src}/{id}` proxy is
  follow-up.
- 2026-05-21 (session 8): Sonarr lacks a per-series cutoff-met flag.
  `_series_match` in `arr/index.py` uses
  `episode_file_count >= total_episode_count` as a proxy. Honest
  signal needs per-episode walk -- out of scope for v1.
- 2026-05-21 (session 6): `selection` is `Map<hash, bytes>` (not
  `Set<hash>`). Bytes captured at check-time from row's `data-bytes`.
  Footer total is pure in-memory sum -- no DOM access per checkbox
  click.
- 2026-05-20: FastAPI + Jinja2 + HTMX (not SPA) -- single-language
  Python toolchain, trivial Nix packaging, no Node build step.
- 2026-05-20: `state/store.py` split out after architecture review:
  `store` (canonical), `views` (pure filters), `subscribers`
  (per-client). Reconciler is the sole store mutator.
- 2026-05-20: Auth-bypass workaround lives in `qbit/client.py` (not
  config) -- it's transport behaviour, not a user-facing toggle.
- 2026-05-20: Grouping precedence: explicit tag (`tmdb:` / `imdb:`)
  > category override > guessit verdict > raw title fallback.
- 2026-05-20: TMDB enrichment deferred -- `enrichment/` slot
  reserved, tag-based hook (`tmdb:<id>`) already supported by
  grouper. Phase 11 (arr) covers the practical use case.
- 2026-05-20: `Settings.model_config` adds `enable_decoding=False`.
  pydantic-settings v2 JSON-decodes complex env values at the source
  layer before `field_validator(mode="before")` runs, so
  `MOVIE_CATEGORIES=movies,films` blew up without this.
- 2026-05-20: `MainDataDelta.added` / `changed` typed as
  `dict[str, dict[str, object]]` (not bare `dict`) so `mypy --strict`
  passes.
- 2026-05-20: Reconciler state-string map includes qBit 5.x's
  `stoppedUP` / `stoppedDL` aliases (renamed from `pausedUP` /
  `pausedDL`) so the live instance doesn't fall through to the
  `unknown -> ERRORED` default.
- 2026-05-20: Containerisation slotted in before Phase 6.
  `python:3.13-slim` + uv from `ghcr.io/astral-sh/uv`, non-root,
  `env_file=.env`. Host 8080 -> container 8765.
- 2026-05-20: `flake.nix` shipped as `writeShellApplication` shims
  (`nix run .#qbit` / `nix run .#qbit-docker`), NOT a proper Nix
  derivation -- Phase 9.4 owes that.

## Blockers
<!-- List any active blockers. Remove the line when resolved. -->
