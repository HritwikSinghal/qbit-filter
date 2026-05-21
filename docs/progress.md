# Project: qbit-filter
> Last updated: 2026-05-21 | Session: 9

## Current state

**Phase 12 (cache removal + chunked cold-boot streaming) committed
in `13d289d`.** The localStorage paint-from-cache trick is gone.
Cold-boot now streams real data: qBit `sync/maindata` is post-fetch
chunked into 200-torrent slices (`Settings.qbit_cold_boot_chunk_size`,
env `QBIT_COLD_BOOT_CHUNK_SIZE`); each chunk parses + groups + publishes
`EventKind.RESYNC_PARTIAL`. The SSE renderer treats partials like RESYNC
but with a 100 ms floor instead of the 1 s coalesce window, so the first
~100 group cards reach the browser within a few hundred ms of qBit
responding (not after the full ~1310-torrent rebuild).

`_arr_poller` was restructured into two cooperating loops
(`asyncio.gather`): the existing 60 s `arr_fetch_loop`, plus
`qbit_reindex_loop` that wakes on every qBit RESYNC/RESYNC_PARTIAL,
debounces 250 ms, and re-runs `build_index` against the cached arr
snapshot. `hash_to_arr` swaps atomically (build new dict, assign whole)
so concurrent template reads never see a half-mutated state. Ping-pong
avoided by the listener removing itself from the bus before publishing
its own RESYNC.

Bonus: `DEV_MODE=true` in `.env` wires `python -m qbit_filter` to
`uvicorn --reload` watching `src/qbit_filter/`. Combined with the
existing `livereload.js` (`/dev/version` poll), it gives true
hot-reload for .py/.html edits.

**Baseline.** ~1310 torrents -> ~622 groups on production qBit. `GET /`
is a `StreamingResponse`: head + chrome + sidebar flushed first, group
cards rendered in an 8-worker `ThreadPoolExecutor` with a 16 KB flush
buffer, marker at `<!--QF_STREAM_INSERT-->`. SSE keeps the page warm.
Empty first-paint affordance is the SSE-driven "Loading torrent
list..." progress block.

### Pickup priorities

1. **Verify Phase 12 cold-boot live.** Boot the app, throttle DevTools
   to Fast 3G, confirm first batch of group cards appears within
   ~500 ms of `/` returning. Confirm no `qf_has_cache` cookie or
   `qf_groups_cache_v*` localStorage anywhere. Hard-reload, confirm
   identical first-paint timing (no cache magic).

2. **Phase 11 follow-ups** (independently landable):
   - **Identity-based regrouping** -- merge two qBit groups sharing a
     TMDB/TVDB id (e.g. "Dune 2021" / "Dune Part One 2021"). Post-pass
     in `state/views.py` after `apply_filters`; grouper stays pure.
   - **Sonarr-aware season grid** -- replace flat `[S01][S02]` chips
     with `[S01 OK 10/10][S03 X 7/10]`. Data already on
     `ArrSeries.season_monitored` + `episode_file_count` /
     `total_episode_count`; needs `_season_grid.html` partial.
   - **Below-cutoff anti-rule** -- warn (yellow severity factor) when
     `SupersededQualityRule` would mark a 1080p but arr is searching
     for an upgrade (`quality_cutoff_met == False`). Not a separate
     rule chip.
   - **Open in Radarr/Sonarr deep-link** -- kebab menu item using
     `arr_meta.title_slug` + `radarr_url` / `sonarr_url` already on
     `ArrStore`.

3. **Poster proxy `/poster/{src}/{id}`.** ~50 LOC async route in
   `web/routes.py`; pipes `httpx` -> `StreamingResponse`, strips API
   key. Only matters if this ever runs outside LAN.

4. **Nix flake derivation (Phase 9.4).** Replace the
   `writeShellApplication` shim in `flake.nix` with a proper Python
   derivation. Then 9.3 / 9.5 / 9.6 fall into line.

5. **README + CLAUDE.md final pass (Phase 9.2).**

6. **Tests backfill.** No `tests/` yet. CLAUDE.md says
   `python3 -m pytest tests/ -v` is required before commit; memory
   `[[feedback_defer_tests]]` defers it to a dedicated pass. Highest
   leverage: `arr/index.py:build_index` (pure), `arr/client.py`
   (httpx-mock harness), the new `_rebuild_chunked` path.

### Known caveats

- **Sonarr lacks per-series cutoff.** `arr/index.py:_series_match`
  uses `episode_file_count >= total_episode_count` as a proxy. Real
  per-series cutoff needs a `/episode` walk; out of scope for v1.
- **Title-fallback false matches.** `ARR_TITLE_FALLBACK=true`
  (default) catches more but can mis-match "Avatar" (2009) against
  an unrelated entry. Set to `false` for strict downloadId-only
  matching; cost is pre-history torrents appear as orphans.
- **API key visible in browser** for posters. Acceptable on LAN; see
  pickup priority 3.
- **Two-poller fan-out.** qBit poller ticks ~1 s, arr poller ~60 s,
  arr also re-indexes on qBit RESYNCs. SSE coalesces via
  `last_resync_at` + `last_partial_at` so duplicate RESYNCs collapse.
- **`ArrStore` concurrent mutation.** Mitigated by atomic dict-
  reference swap on `hash_to_arr` (build new, assign whole). Other
  fields (`movies_by_id`, etc.) follow the same pattern.

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
  mypy --strict src/qbit_filter`. Both pass on `master` as of
  `13d289d`. Two pre-existing mypy errors in `arr/client.py:460,477`
  (Item "None" of "Any | dict | None" has no attribute "get") --
  not introduced by recent work.
- `python3 -m pytest tests/ -v` is the eventual gate; `tests/`
  doesn't exist yet (priority 6).
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
- `cleanup/` houses the rule engine (registry + rule presets). The
  rule set is the plugin surface -- adding a new criterion should be
  a small, well-isolated change.
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
  state/       store.py  events.py  reconciler.py  views.py
               subscribers.py  viewport.py  arr_store.py
  cleanup/     registry.py  rules.py
  web/         routes.py  render.py  filter_parse.py
               templates/  base.html  index.html  _sidebar.html
                           _filters.html  _active_filters.html
                           _group.html  _torrent.html  _empty.html
                           _rule_bar.html  _selection_bar.html
                           _compare_strip.html
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

Phase 9 remaining: 9.2 (README + CLAUDE.md final pass), 9.3 (verify
Python deps in nixpkgs), 9.4 (proper flake.nix derivation), 9.5 (Nix
build verification), 9.6 (final acceptance check).

## Decisions & Notes

<!-- Append as: YYYY-MM-DD: [decision]. Keep entries that still affect
     current behaviour; let git log own the rest. -->

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
