# qbit-filter

A local web UI grouping qBittorrent torrents by movie/TV show.

## End goal (read first)

**qbit-filter is a rule-based cleanup tool, not a monitoring dashboard.** The user
opens it occasionally to find and bulk-remove superseded or unwanted torrents,
review what a rule caught, and confirm a batch delete. Realtime SSE updates
matter less than comparison, reasoning, and per-row override.

Primary workflow:
1. Pick a saved rule preset (e.g. "Superseded quality").
2. App shows matched groups with quality-comparison strips (e.g. 1080p vs 2160p
   side by side, with the marked-for-removal version flagged and the reason
   inline: added date, ratio, size).
3. User reviews per-group, deselects mistakes, confirms.
4. Bulk delete runs through `qbit/actions.py`.

Core rule presets to support:
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
- `state/store.py` is mutated only by `state/reconciler.py`. Everyone else reads.
- `state/views.py` is pure: snapshot in, filtered groups out.
- Each SSE client owns a `Subscription` with its own `FilterState`. Filter
  changes update **that subscription's** state, not the store.
- Action endpoints (`/torrents/{hash}/{action}`) call `qbit/actions.py` and
  return 204. The reconciler picks up the change on next poll and SSE updates
  the UI naturally - no double-write.

## qBit instance

`https://arr.theoakenshield.com/qbittorrent` with IP-auth-bypass. Credentials
in `.env` can be any non-empty placeholder. Do NOT "fix" the `LoginFailed`
try/except in `qbit/client.py` - it exists for a real reason.

## Conventions

- Always run `python3 -m pytest tests/ -v` before considering a change done.
- `ruff check` + `mypy --strict` must pass.
- New features land with tests; bugfix commits include a regression test.
- No emojis in any file (pre-commit hook enforces this).

## Long-Running Project

This project uses session-persistent tracking. At the start of every session:
1. Read `docs/progress.md` silently for a full catch-up -- do not ask the user to re-explain anything.
2. Do NOT automatically continue working -- wait for the user to indicate they want to proceed.
3. After each completed task, update `docs/progress.md` immediately (mark `[x]`, recount Status Summary, update date).
4. `docs/progress.md` is the primary task tracker.
