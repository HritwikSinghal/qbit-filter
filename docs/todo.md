# Todo

> Going forward, structured tracking lives in **`/tasks.md`** (project root)
> and **`docs/progress.md`**. This file stays as a free-form running list
> of ideas / wishes -- tick items here when they land, then move the
> structured follow-up into `tasks.md`.

## Goal

- we originally had 1080p versions downloaded for many shows/movies, but later on upgraded them to 2160p (which should have date added after 1080p version). so we need to remove all torrents which are upgraded.
  - can select torrents based on custom filter .one such filter is below
  - select all torrents from each movie/show which matches below criteria.
  - torrent was added before 2026 in my client
  - the show/movie has both 1080p and 2160p torrents. in this case, select the 1080p torrent (for deletion).
  - others. think about it.

- How can we change code to create dynamic filters like these to bulk select torrents based on parameters like date added.
- Maybe have a set of filters like for date added, quality, size; and after selecting from each filter, we click on "select" so it goes and selects torrents from each tv/movie.

## Done

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
      `series.seasons[].statistics`.
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
