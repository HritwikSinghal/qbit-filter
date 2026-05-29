# Todo

> Structured tracking lives in **`docs/progress.md`** (status, recent
> commits, pickup priorities, decisions). This file stays as a free-form
> running list of ideas / wishes -- tick items here when they land.

## Goal (historical, now shipped)

This section captures the origin question for the project. The
upgrade-detection ask below shipped as `SupersededQualityRule` +
`DuplicateSameQualityRule` (see `cleanup/rules.py`); leaving the
description here for context.


- we originally had 1080p versions downloaded for many shows/movies, but later on upgraded them to 2160p (which should have date added after 1080p version). so we need to remove all torrents which are upgraded.
  - can select torrents based on custom filter .one such filter is below
  - select all torrents from each movie/show which matches below criteria.
  - torrent was added before 2026 in my client
  - the show/movie has both 1080p and 2160p torrents. in this case, select the 1080p torrent (for deletion).
  - others. think about it.

- How can we change code to create dynamic filters like these to bulk select torrents based on parameters like date added.
- Maybe have a set of filters like for date added, quality, size; and after selecting from each filter, we click on "select" so it goes and selects torrents from each tv/movie.

## Done

- [x] **P1 structural refactors (session 13).** All four flagged by the
      session-10 arch review, landed as separate commits on branch
      `refactor/priority-3-structural`:
      `cleanup/rules.py` -> pluggable `cleanup/rules/<slug>.py` package +
      `types.py` + `scoring.py` + `pkgutil` registry (`ce8e71e`);
      qBit telemetry off `Store` into `state/telemetry.py` (`ea8c404`);
      `web/routes.py` -> `web/routes/` `APIRouter` package (`e0c2be2`);
      filter changes routed through the SSE `RESYNC_FILTER` path, 204 +
      `hx-swap="none"` (`34c4d52`). ruff + mypy --strict clean; verified
      via TestClient/import smokes. Manual live-boot verification still
      owed (see progress.md pickup #3).
- [x] **Responsive layout + row-click toggle (session 12,
      `b9dbe8e`).** Layout was rendering at a fixed ~2734 px wide
      regardless of viewport because a `white-space: nowrap` torrent
      name's min-content expanded the row's bare-`1fr` grid track,
      cascading up through `.group-card` and `.shell`. Changed every
      affected `1fr` to `minmax(0, 1fr)` (`.shell`, `.group-card`,
      `.torrent-row`, `.compare-strip`, plus their mobile breakpoints).
      Plain click on a torrent row now toggles selection (was: focus-
      only); drag-to-select and an active text selection both suppress
      the toggle so name / meta / factor pills remain copyable.
      `.torrent-row { cursor: pointer; }` for affordance, explicit
      `user-select: text` on copyable text spans.
- [x] **Sonarr arr-live coverage fix (`3c31de9`).** The "arr live"
      chip + arr-current keeper logic was already wired for both
      Radarr and Sonarr in `ArrMatch.arr_current`, but
      `_fetch_current_download_ids` paged Sonarr's history grouped by
      `episodeId` at the same 4-page (1000-record) cap as Radarr's
      per-movie walk. A ~thousand-episode library only got the most
      recent ~1000 imports flagged -- 9.3% of TV rows vs 86% of movie
      rows. Raised Sonarr's `pages` cap to 40 and added an early-stop
      (full page contributes no new entity ids). TV coverage now 84.8%.
- [x] **Header activity widget (`bd671d5`).** Persistent header chip
      exposes qBit + arr poller state (connect status, last poll,
      cycles, queue / history / match counts) + rolling 16-line log,
      OOB-swapped on every RESYNC. Distinguishes "not configured" from
      "configured but unreachable" via per-service counters.
      `cold_boot_total` stamped up-front so the streaming progress bar
      has a stable denominator. Per-season keepers threaded through
      render as a frozenset (data hook for the Sonarr-aware season
      grid below).
- [x] **Arr-current file as duplicate keeper (`ab72615`).**
      `arr/client.py` fetches `downloadFolderImported` history per
      movie/episode; resulting hashes union into `ArrSnapshot`, every
      `ArrMatch` carries an `arr_current` flag. `DuplicateSameQualityRule`
      promotes the arr-current torrent to keeper (falls back to newest
      when arr has no opinion). Green "arr live" chip renders on plain
      rows and on both sides of the compare strip.
- [x] **Keep-newest duplicate + guessit TV title fallback (`b4999cf`).**
      `DuplicateSameQualityRule` now keeps the **newest** copy at a
      tier (arr re-grabs only on upgrade/repack). `arr/index.py` title
      fallback routes through guessit so TV releases without a year
      token strip release noise before lookup -- old prefix-cut left
      `S01.1080p.WEB-DL...` glued and missed `series_by_norm`.
- [x] **Per-season TV cleanup-rule scoping (`025c607`).**
      `SupersededQualityRule` + `DuplicateSameQualityRule` partition TV
      groups by `quick_season` (with a full-series bucket) before
      keeper/loser logic. Fixes the bug where an S01 2160p pack marked
      an S02 1080p release as superseded. Movies unchanged.
- [x] **Session 10 reliability + UX sweep (`b78fdb1`).** Event-loop
      hardening (dict-iter-snapshot, asyncio.wait_for on blocking
      `to_thread`, long-lived `httpx.AsyncClient`, qBit poll backoff,
      lifespan SSE drain), cold-boot RESYNC_PARTIAL suppression in the
      arr listener, layout-shift fix (per-card `contain-intrinsic-size`,
      `.no-cv` opt-out, `scrollbar-gutter: stable`), keys.js cluster
      (MO race fix, drop rAF batch-staging, Escape chain unified,
      FilterState session replay), structured debug logging baseline
      (Python `LOG_LEVEL=debug` + JS `qfLog` namespace).
- [x] **Radarr / Sonarr integration (Phase 11).** New `arr/` package
      (client + sync + index + models), `state/arr_store.py`, \*arr
      polling task in `app.py` lifespan. Match precedence: tag -> queue
      -> history -> normalised title+year (toggleable). New rules:
      `arr-cutoff-met-cold`, `arr-unmonitored`. Posters hot-linked from
      `${arr_url}/api/v3/MediaCover/{id}/poster.jpg?apikey=...`,
      monitored / cutoff badges in group meta, per-row monitored eye
      indicator. New filter facets: Monitored / Unmonitored / Orphan
      and Cutoff met / Upgrade pending. Cache version bumped 4 -> 5.
      Settings: `RADARR_URL`, `RADARR_API_KEY`, `SONARR_URL`,
      `SONARR_API_KEY`, `ARR_POLL_INTERVAL_SECONDS`,
      `ARR_TITLE_FALLBACK`.
- [x] **Upgrade-detection bulk select.** Phase 10, session 7. Side-by-
      side compare strip (KEEPER vs FLAGGED) + structured factor pills + keeper "K" badge + per-group "Select losers" button + `a` key
      shortcut. Rule is `SupersededQualityRule` in
      `cleanup/rules.py`. Soft undo replaces blocking confirm.
- [x] **Operator Console UI polish (Phase 10, session 7).** Motion
      tokens, tier-color row borders + hover tints (extends user's
      1080p-blue / 4K-red idea to the whole row), keyboard nav
      (j/k/J/K/x/Shift+X/Ctrl+A/a/i/u/Enter/1-9/?), cheatsheet
      overlay, "Invert" + "All losers" selection-bar buttons.
- [x] High CPU + slow boot time -- Session 3, 2026-05-21. SSE
      subscription leak on disconnect (`routes.py` stream() refcounts +
      bus.remove + drain on exit), 500ms livereload throttled to 5s +
      visibility-aware (`static/livereload.js`), `apply_filters` skipped
      per SSE tick (`group_matches` per touched group + cached visible
      set in `_oob_payload`), `count_by_facet` memoised by `store.rid`,
      `parse()` lru_cached + reconciler `_classify()` removes
      double-parse.
- [x] Stream torrents and render as available, use multithreading --
      Session 3 (initial StreamingResponse) + Session 4 (8-worker pool,
      byte-threshold flush, inline script tag before marker so
      handlers attach during stream, not after).
- [x] Filter: "Multiple torrents only" -- Session 3.
- [x] Multi-select + bulk actions (pause/resume/recheck/delete/purge)
      -- Session 3. Endpoints under `/torrents/bulk/...`, JS-set
      selection survives SSE partial swaps.
- [x] **NOT filters** -- e.g. `NOT cross-seed` tag shows torrents that
      don't have the tag. Touches `FilterState`, `views.py`,
      `filter_parse.py`, `_filters.html`, `_active_filters.html`.

- lets make a big UI change. instead of a down arrow with bottow panel opening for each movie, show the basic movie info on 1/4th of left part of row and 3/4th part will be another element with all the torrents of that movie/show in rows.

- since at any time a user is only viewing approx 10 torrents which are on his screen, poll only their data and not
  the whole 1111 torrents. maybe add 5 more torrents before and after what user is watching so that if he scrolls up or down, the data is quickly available.

- make sure the "cleanup rules" are only applied to items in current view (filtered only if so). If i add a filter, then the rules should apply to my current view only. and there is not "NOT" filterss visible on screen or any box to show progress on first bootup. (its still showing 'The poller is still warming up, or the qBit instance is unreachable. Check the server logs.' instead of a progress bar and logs)

- need to optimize this website. it has horrible performance and the browser lags when all data is loaded. when no data is there, the website is smooth. figure out the choke points and figure out ways to optimise them. we need this website to be as fast as possible. make these as quickly as possible. figure out the handful of things which will cause most amount of speedup.

- have people select multiple items using shift+click and ctrl+click. Also optimize the JS wherever possible. And before running delete or 'delete + purge' (after clicking on the button), show a box window on current page with summary of items being removed and why. This will be confirmation dialog also.

- also, the first boot torrent loading progress is not showing. check for errors in those too. QUICKLY

- so now that "superseded quality" filter is working, we need a new filter which is smart enought to check if a movie/show has multiple torrents, and all of them are 4k (or 1080p), then it can select the 1 torrent which can be removed. the condition for it should be: the new torrent was added after old one. both are same quality. (one exception can be if both torrents of movie/show is downloaded within last 10 days, then the one for removal is highlighted in yellow since any freeleech torrents which are not seeded for 10days will add a penalty on your account.)figure out other things yourself. make the filter system extensible so that more such rules can be added easily in future.

- lets start with radarr/sonarr integration. before starting the plan, create another plan where you explore thoroughly how more smart filters and more UI features can be implemented in this application with sonarr/radarr integration data. find out atleast 10 ways. then start implementation. This was your response btw from last session: "arr would unlock different future rules (TMDB/IMDB identity dedup, monitored vs orphan, episode-level granularity). Worth its own phase only when a rule actually needs that precision.". Search on the internet for radarr/sonarr api and for use cases too.
-

## Open

- [ ] **Identity-based regrouping (Phase 11.7).** Merge guessit-split
      groups when they share a TMDB / TVDB id from arr. e.g. "Dune Part
      One 2021" + "Dune 2021" -> one group.
- [ ] **Sonarr-aware season grid (Phase 11.8).** Replace flat season
      chips with `[S01 OK 10/10][S02 OK 10/10][S03 X 7/10]` from
      `series.seasons[].statistics`. Per-season keepers are already
      threaded through render as a frozenset (shipped in `bd671d5`), so
      the data hook is in place; remaining work is the `_season_grid.html`
      partial.
- [ ] **Confirm-dialog enrichment (Phase 11.9).** "Deleting 5 GB
      across 12 monitored Sonarr episodes. Sonarr will re-search if you
      proceed." Concrete consequence text from arr_match data.
- [ ] **Poster proxy endpoint.** Currently posters hot-link to
      `${radarr_url}/api/v3/MediaCover/.../poster.jpg?apikey=...` so the
      API key is visible in the browser. Acceptable on LAN; add a
      `/poster/{src}/{id}` proxy if this ever runs over the open internet.
- [ ] **More arr rules.** Below-cutoff upgrade-pending anti-rule,
      orphan rule, identity-based duplicate, ended-series-+-complete,
      unmonitored season, quality-profile mismatch, stalled grab,
      size mismatch, removed-from-library. See exploration plan for
      details (`~/.claude/plans/make-these-sequential-kay.md`).
- [ ] **arr filter facets (more).** Quality profile, collection,
      genre, series status (continuing/ended), network, language.
- [ ] **"Open in Radarr/Sonarr" deep-link** in the kebab menu.
- [ ] **Search by TMDB / IMDB ID.** Global search accepts `tmdb:155`
      and `tt0468569` syntax.
- [ ] **Tag backfill.** Push `radarr:<id>` / `sonarr:<id>` tags to
      qBit when an ArrMatch is established via queue/history/title.
      Optional / behind a flag.
- [ ] **Dynamic filter rules** to bulk-select torrents. UI with
      reusable axes (date added, quality, size, presence-of-higher-
      quality-in-same-group) that compose into one selection rule. The
      upgrade-detection rule (now shipped) is one preset of this system.
- [ ] **Confidence sort.** Order rule-matched groups by clear-cut-ness
      (tier-step delta × log(added-day delta)) so ambiguous matches
      sink. See Phase 10 plan deferred section.
- [ ] **Protected tag** (dupeGuru "reference folder" analog). Tag a
      torrent `keep` to opt it out of every rule.
- [ ] **Command palette (Cmd-K).** Defer until rule count > 7.
