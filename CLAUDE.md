# qbit-filter

A local web UI grouping qBittorrent torrents by movie/TV show.

## End goal (read first)

**qbit-filter is a general qBittorrent management tool built around an
extensible set of selection criteria / rule presets.** It is not a monitoring
dashboard, and it is not a single-purpose upgrade-detector. The user opens it
occasionally to find and bulk-remove (or otherwise act on) torrents that match
a rule, review what the rule caught, and confirm a batch operation. Realtime
SSE updates matter less than comparison, reasoning, and per-row override.

The rule set will grow over time -- "quality upgrade check" (mark 1080p when a
newer 2160p of the same title exists) is the first preset, not the whole
project. Treat the cleanup engine (`cleanup/registry.py` + `cleanup/rules.py`)
as a plugin surface: adding a new criterion should be a small, well-isolated
change, not a refactor. Don't bake assumptions about any one rule into shared
infrastructure (selection UI, group layout, action surface, persistence) --
those layers must serve N rules.

Primary workflow:
1. Pick a saved rule preset (e.g. "Superseded quality").
2. App shows matched groups with quality-comparison strips (e.g. 1080p vs 2160p
   side by side, with the marked-for-removal version flagged and the reason
   inline: added date, ratio, size).
3. User reviews per-group, deselects mistakes, confirms.
4. Bulk delete runs through `qbit/actions.py`.

Core rule presets to support (non-exhaustive -- more will be added):
- **Superseded quality** -- same title has 1080p and 2160p (or 720p/1080p, x264/x265,
  WEB-DL/BluRay at same resolution) and the older/lower was added first. Mark the lower.
- **Stalled + old** -- added > 90d, no peers in 7d, ratio < 1.0.
- **Ratio met + cold** -- ratio >= target, no activity > 30d.
- **Dead / unregistered tracker** -- tracker reports unregistered.
- **Cross-seed duplicate** -- same files on disk, two infohashes; keep the better one.
- **Orphaned on disk** -- files exist that no torrent references (inverse).
- **Path collision** -- two torrents claim overlapping save paths.

Design implications:
- Per-group layout is `1/4 left (title/year/kind/rule-match) + 3/4 right
  (quality-comparison strip)`, always visible -- no expand-on-click `<details>`.
- The "interesting" groups are multi-quality cleanup candidates; single-quality
  groups collapse to one line.
- A persistent "Selected: N torrents, X.X GB, [Delete keep-files / purge]" footer
  is the primary action surface.
- Polling is viewport-keyed by **group id** (visible + 5 above/5 below = hot set
  at ~3s; cold set at 30s). Parse/group is a one-shot CPU pass on the snapshot.
- UI style direction: **Operator Console** (Linear-grade dark, JetBrains Mono
  for data columns, IBM Plex Sans for titles, indigo accent #5E6AD2,
  hairline borders).

## Quick orientation

- **UI rules (reusable):** `docs/web-ui-design-principles.md`
- **Wishlist:** `docs/todo.md`
- **Live progress:** `docs/progress.md` (the source of truth for what's done)

## Architecture quick reference

- `qbit/` is the only place that imports `qbittorrent-api`. `client.py` carries
  the IP-auth-bypass workaround (`LoginFailed` -> `app_version()`).
- `arr/` is the only place that imports `httpx`. Wraps Radarr / Sonarr
  `/api/v3`; `index.py:build_index` is pure (snapshot + torrents ->
  `dict[hash -> ArrMatch]`). Optional -- disabled when both arr URLs are blank.
- `state/store.py` (canonical qBit state) is mutated only by
  `state/reconciler.py`. Everyone else reads. `state/arr_store.py` is the
  parallel arr-enrichment store, mutated only by the `_arr_poller` task in
  `app.py`. `Store.arr` is a one-way pointer.
- `state/views.py` is pure: snapshot in, filtered groups out.
- `cleanup/` is the rule plugin surface. `registry.py` registers rule classes;
  `rules.py` is the current library (one-file-many-rules; splitting into
  `cleanup/rules/<slug>.py` is on the P1 refactor list). Stub rules raise
  `NotImplementedError` and the registry surfaces them as greyed-out in the
  rule bar.
- Each SSE client owns a `Subscription` with its own `FilterState`. Filter
  changes update **that subscription's** state, not the store. FilterState is
  echoed to the client as `data-qf-state` JSON and replayed from `localStorage`
  on SSE reconnect so selections survive `uvicorn --reload`.
- Action endpoints (`/torrents/{hash}/{action}`) call `qbit/actions.py` and
  return 204. The reconciler picks up the change on next poll and SSE updates
  the UI naturally - no double-write.
- The header activity widget reads telemetry fields off `Store`
  (`qbit_connected`, `qbit_last_poll_at`, `qbit_consecutive_failures`, ...)
  and the per-service counters on `ArrSnapshot`/`ArrStore`.

## qBit instance

`https://arr.theoakenshield.com/qbittorrent` with IP-auth-bypass. Credentials
in `.env` can be any non-empty placeholder. Do NOT "fix" the `LoginFailed`
try/except in `qbit/client.py` - it exists for a real reason.

## Conventions

- `uv run ruff check src/qbit_filter` and `uv run mypy --strict
  src/qbit_filter` must pass before a change is done. ruff is clean on master;
  mypy has two pre-existing `arr/client.py:556,573` errors -- don't introduce
  new ones.
- No `tests/` directory yet -- tests are a dedicated backfill pass, not a
  per-change gate. When a test suite lands (`docs/progress.md` priority 7),
  this convention upgrades to "run pytest before done". Until then, don't
  invent `python3 -m pytest tests/ -v` invocations that will fail.
- Suggest a regression test when fixing a bug (helps the future backfill);
  don't block the fix on writing one.
- No emojis in any file (pre-commit hook enforces this).

## Long-Running Project

This project uses session-persistent tracking. At the start of every session:
1. Read `docs/progress.md` silently for a full catch-up -- do not ask the user to re-explain anything.
2. Do NOT automatically continue working -- wait for the user to indicate they want to proceed.
3. After each completed task, update `docs/progress.md` immediately (mark `[x]`, recount Status Summary, update date).
4. `docs/progress.md` is the primary task tracker.
