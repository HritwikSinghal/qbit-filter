(function () {
  /* =========================================================================
     Browser cache for #groups. The server skips its expensive initial group
     render when the client signals `qf_has_cache=1`; we then paint #groups
     from localStorage so the user sees the list within ~1 frame instead of
     waiting 3-5 s for the StreamingResponse to flush 622 cards. SSE delivers
     row-level deltas (and the occasional full RESYNC) on top of the cached
     snapshot, keeping it live.

     The cache payload is self-generated HTML from a previous page render of
     this same origin, but we still parse it via DOMParser rather than
     innerHTML so any stray <script> tag from a stale cache won't execute
     and any inline event-handler attribute is dropped before attachment.
     ========================================================================= */
  const CACHE_VERSION = window.QF_CACHE_VERSION || 0;
  const CACHE_KEY = 'qf_groups_cache_v' + CACHE_VERSION;
  const CACHE_COOKIE = 'qf_has_cache';

  const setCacheCookie = (val) => {
    if (val) {
      /* 7-day TTL is generous; the cookie is harmless if it outlives the
         cache because the server-side ``cache_mode`` branch is gated on
         the cookie being set AND the FilterState being default. A stale
         cookie just costs an empty-render trip. */
      document.cookie = CACHE_COOKIE + '=1; path=/; max-age=' + (60 * 60 * 24 * 7) + '; SameSite=Lax';
    } else {
      document.cookie = CACHE_COOKIE + '=; path=/; max-age=0; SameSite=Lax';
    }
  };
  const dropCache = () => {
    try { localStorage.removeItem(CACHE_KEY); } catch (e) { /* quota / disabled */ }
    setCacheCookie(false);
  };

  const paintFromCache = (cached) => {
    const groups = document.getElementById('groups');
    if (!groups) return false;
    /* If a group card is already present, the server is streaming -- bail
       so we don't double-paint. Checking for .group-card specifically (not
       firstElementChild) is important because the inline <script> that
       calls us is itself a child of #groups at this point. */
    if (groups.querySelector('.group-card')) return false;
    const parser = new DOMParser();
    const doc = parser.parseFromString(cached, 'text/html');
    /* Walk the parsed body's direct children. We only expect <article
       class="group-card"> nodes (and whitespace text). Skip anything that
       isn't an article so script/style tags from a stale or corrupted
       cache are silently dropped. */
    const frag = document.createDocumentFragment();
    for (const node of Array.from(doc.body.children)) {
      if (node.tagName === 'ARTICLE') frag.appendChild(node);
    }
    if (!frag.firstChild) return false;
    groups.appendChild(frag);
    return true;
  };

  /* Exposed for the inline call inside <div id="groups">. keys.js is loaded
     *before* that element so the script can't paint directly here -- the
     div doesn't exist in the DOM yet at IIFE-execution time. The inline
     script in index.html runs once the parser is inside #groups, at which
     point this function can append children to it.

     We only paint when the server told us it's also skipping the group
     render (`QF_CACHE_MODE` true). Painting against a streaming response
     would race the parser and produce duplicate cards. */
  window.qfPaintCache = function () {
    if (!window.QF_CACHE_MODE) return;
    try {
      const cached = localStorage.getItem(CACHE_KEY);
      if (!cached) { setCacheCookie(false); return; }
      const ok = paintFromCache(cached);
      if (!ok) setCacheCookie(false);
    } catch (e) {
      console.warn('cache paint failed', e);
      setCacheCookie(false);
    }
  };

  /* Save strategy:
     - First save fires synchronously on `window.load`. No debounce. This
       captures the post-stream snapshot BEFORE SSE's RESYNC kicks off the
       OOB-swap storm; without this, the storm would starve the debounce
       timer and save would never fire (observed ~50% flake on cold-start
       probes with a 750 ms debounce).
     - Subsequent saves (from SSE-driven HTMX swaps) are debounced 5 s.
       Long enough that a burst of ~50 OOB swaps coalesces into one save;
       short enough that genuine state changes land in cache within seconds. */
  const filtersActive = () => {
    const strip = document.getElementById('active-filters');
    return !!(strip && strip.querySelector('.active-chip'));
  };

  /* Cache writes serialise the entire #groups subtree (~900 KB for a
     1300-torrent catalogue) and then do a synchronous localStorage.setItem.
     Both steps run on the main thread, so during an SSE storm this can
     stall input for ~50-150 ms. Wrap the write in requestIdleCallback so it
     yields to user interaction, and refuse to store payloads above
     MAX_CACHE_BYTES (rather than throw QuotaExceeded). */
  const MAX_CACHE_BYTES = 2_000_000;

  const saveCacheNow = () => {
    if (filtersActive()) { dropCache(); return; }
    const groups = document.getElementById('groups');
    if (!groups || !groups.firstElementChild) return;
    const writer = () => {
      try {
        const html = groups.innerHTML;
        if (html.length > MAX_CACHE_BYTES) { dropCache(); return; }
        localStorage.setItem(CACHE_KEY, html);
        setCacheCookie(true);
      } catch (e) {
        /* QuotaExceeded / private-browsing -- clean up and bail. */
        dropCache();
      }
    };
    if (typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(writer, { timeout: 2000 });
    } else {
      writer();
    }
  };

  let saveTimer = null;
  const saveCacheSoon = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      saveTimer = null;
      saveCacheNow();
    }, 10000);
  };

  document.body.addEventListener('htmx:afterSettle', saveCacheSoon);
  /* `htmx:oobAfterSwap` covers SSE OOB swaps in case they don't bubble
     through `afterSettle`. */
  document.body.addEventListener('htmx:oobAfterSwap', saveCacheSoon);

  /* Filter mutations make the cache stale. Drop it before HTMX fires the
     request so a reload mid-filter-change can never hit a polluted cache.
     We also rewrite the ``facet`` parameter to ``not_<facet>`` when the
     user shift-clicked a facet chip, so the same HTMX hx-vals payload
     can serve both include and exclude. */
  document.body.addEventListener('htmx:configRequest', (e) => {
    const detail = e.detail;
    const path = detail && detail.path;
    if (typeof path !== 'string' || path.indexOf('/filters') !== 0) return;
    dropCache();
    const evt = detail.triggeringEvent;
    const params = detail.parameters;
    if (!evt || !evt.shiftKey || !params) return;
    const f = params.facet;
    if (typeof f === 'string' && !f.startsWith('not_') && f !== 'search' && f !== 'min_torrents') {
      params.facet = 'not_' + f;
    }
  });

  /* First-load save: immediate snapshot + a debounced follow-up.
     - Immediate `saveCacheNow` captures whatever is in #groups at load
       time. On some runs Firefox fires `load` before the stream finishes
       parsing, so this snapshot can be tiny (~260 bytes).
     - `saveCacheSoon` queues a 5 s debounced save that the post-RESYNC
       SSE swap will refresh into a full snapshot. If the immediate save
       was already complete, this is just a no-op overwrite with the same
       content; if it was premature, this fixes it. */
  window.addEventListener('load', () => {
    saveCacheNow();
    saveCacheSoon();
  }, { once: true });

  /* =========================================================================
     First-load progress overlay.

     Shown only when the cache paint did NOT run (cold visits / cleared cache),
     so the user has feedback during the multi-second initial stream. We track
     a few milestones (script init, SSE open, first card painted, RESYNC done)
     and bump the progress bar accordingly. The overlay self-removes once a
     ``.group-card`` is in the DOM -- whichever event delivers it.
     ========================================================================= */
  function firstLoadOverlay() {
    const groups = document.getElementById('groups');
    /* The script tag is inlined ABOVE #groups and #first-load so handlers
       attach early under StreamingResponse. That means on first call those
       elements aren't yet parsed -- retry on the next frame until they
       appear (chrome is flushed in one go before group bodies stream in,
       so this only ever takes a frame or two), with a hard cap so we don't
       spin if something goes wrong. */
    if (!groups || !document.getElementById('first-load')) {
      if (firstLoadOverlay._retries === undefined) firstLoadOverlay._retries = 0;
      if (firstLoadOverlay._retries++ > 200) return;
      requestAnimationFrame(firstLoadOverlay);
      return;
    }
    /* Cache paint already inserts cards synchronously before this script's
       IIFE finishes (qfPaintCache appends children to #groups in-line with
       parsing). If we already see a card, skip the overlay entirely. */
    if (groups.querySelector('.group-card')) return;
    const overlay = document.getElementById('first-load');
    const bar = document.getElementById('fl-bar-fill');
    const log = document.getElementById('fl-log');
    if (!overlay || !bar || !log) return;
    overlay.hidden = false;

    const milestones = [
      { pct: 10, msg: 'Page loaded' },
      { pct: 30, msg: 'Connecting to live stream' },
      { pct: 55, msg: 'Receiving torrent snapshot' },
      { pct: 80, msg: 'Rendering group cards' },
      { pct: 100, msg: 'Done' },
    ];
    let step = 0;
    const advance = (msg, pct) => {
      const li = document.createElement('li');
      li.textContent = msg;
      log.appendChild(li);
      log.scrollTop = log.scrollHeight;
      bar.style.width = pct + '%';
    };
    const tick = () => {
      if (step >= milestones.length) return;
      const m = milestones[step++];
      advance(m.msg, m.pct);
    };
    tick();  // "Page loaded"

    document.body.addEventListener('htmx:sseOpen',  () => { if (step === 1) tick(); }, { once: true });
    document.body.addEventListener('htmx:sseError', () => {
      const li = document.createElement('li');
      li.textContent = 'Stream error -- retrying';
      li.className = 'err';
      log.appendChild(li);
    });

    const obs = new MutationObserver(() => {
      if (!groups.querySelector('.group-card')) return;
      /* Drain remaining milestones quickly so the bar ends at 100% before
         we fade out. The overlay leaves no residual DOM after removal. */
      while (step < milestones.length) tick();
      obs.disconnect();
      overlay.classList.add('fl-done');
      setTimeout(() => { overlay.remove(); }, 350);
    });
    obs.observe(groups, { childList: true });

    /* Safety: if nothing arrives in 30 s, fade away rather than nailing
       the user under a non-progressing bar. The page still works -- the
       overlay just stops being useful. */
    setTimeout(() => {
      if (!document.getElementById('first-load')) return;
      overlay.classList.add('fl-done');
      setTimeout(() => overlay.remove(), 350);
    }, 30000);
  }
  firstLoadOverlay();

  /* =========================================================================
     Theme: simple dark (default) / light toggle. Dark is the token default;
     `body.light` flips the CSS custom properties to the light palette.
     ========================================================================= */
  const applyMode = (mode) => {
    document.body.classList.toggle('light', mode === 'light');
    try { localStorage.setItem('qf_theme', mode); } catch (e) { /* private mode */ }
  };

  (function initTheme() {
    try {
      const saved = localStorage.getItem('qf_theme');
      if (saved === 'light') applyMode('light');
    } catch (e) { /* private mode */ }
  })();

  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    applyMode(document.body.classList.contains('light') ? 'dark' : 'light');
  });

  /* =========================================================================
     Mobile drawer toggle for the filter sidebar.
     ========================================================================= */
  document.getElementById('open-filters')?.addEventListener('click', () => {
    document.getElementById('filter-drawer')?.classList.toggle('open');
  });

  /* =========================================================================
     Selection model + selection footer.

     The DOM is rerendered piece-by-piece via SSE/HTMX. The source of truth is
     the in-memory `selection` map; after every relevant swap we re-paint
     checkboxes & data-marked attributes from it so selections survive
     partial re-renders.
     ========================================================================= */
  /* Map<hash, bytes>. Bytes are captured at check-time from the row's
     data-bytes attribute so the footer can sum them without re-querying the
     DOM per selected hash. Avoids an O(N) `document.querySelector` storm on
     every selection change when 100+ rows are selected. */
  const selection = new Map();

  function rowBytes(row) {
    if (!row) return 0;
    const sizeSpan = row.querySelector('.meta span[data-bytes]');
    if (sizeSpan) {
      const n = parseInt(sizeSpan.getAttribute('data-bytes') || '0', 10);
      return Number.isNaN(n) ? 0 : n;
    }
    /* Fallback for cached HTML predating the data-bytes attribute. */
    const metaSpans = row.querySelectorAll('.meta span');
    for (const span of metaSpans) {
      const m = span.textContent && span.textContent.match(/^\s*([\d.]+)\s*GB/);
      if (m) return parseFloat(m[1]) * 1073741824;
    }
    return 0;
  }

  function repaintSelectionFooter() {
    const bar = document.getElementById('selection-bar');
    if (!bar) return;
    bar.dataset.active = selection.size > 0 ? 'true' : 'false';
    const countEl = document.getElementById('sel-count');
    const sizeEl = document.getElementById('sel-size');
    if (countEl) countEl.textContent = String(selection.size);
    if (sizeEl) {
      let bytes = 0;
      for (const b of selection.values()) bytes += b;
      sizeEl.textContent = (bytes / 1073741824).toFixed(2);
    }
  }

  /* Event delegation works for SSE-injected rows too. */
  document.body.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement) || !t.classList.contains('row-check')) return;
    const row = t.closest('.torrent-row');
    const h = row?.getAttribute('data-hash');
    if (!h) return;
    if (t.checked) selection.set(h, rowBytes(row));
    else selection.delete(h);
    if (row) row.setAttribute('data-marked', t.checked ? 'true' : 'false');
    repaintSelectionFooter();
  });

  /* =========================================================================
     Bulk delete: buffered undo toast instead of a blocking confirm().
     Industry pattern (Gmail / Linear / Superhuman). The qBit request fires
     after a grace window unless the user clicks Undo or presses `u`. If a
     second delete kicks off during the window, the first one commits
     immediately (FIFO) so we never overlap pending actions.
     ========================================================================= */
  const UNDO_WINDOW_MS = 8000;
  let pending = null; // { hashes, purge, sizeBytes, commitAt, timer, controller }

  async function commitPending(send = true) {
    if (!pending) return;
    /* Undo path: un-dim the soft-hidden rows before we drop the pending
     * reference so they snap back to normal instead of vanishing after
     * the SSE poll catches up. */
    if (!send) restorePendingRows();
    const p = pending;
    pending = null;
    if (p.timer) { clearTimeout(p.timer); p.timer = null; }
    hideUndoToast();
    if (!send) return;
    const body = new URLSearchParams();
    body.set('hashes', p.hashes.join('|'));
    body.set('purge', p.purge ? '1' : '0');
    try {
      const res = await fetch('/torrents/bulk/cleanup', { method: 'POST', body });
      if (!res.ok) alert('Cleanup failed: HTTP ' + res.status);
    } catch (err) {
      alert('Cleanup failed: ' + err);
    }
  }

  function hideUndoToast() {
    const t = document.getElementById('undo-toast');
    if (!t) return;
    t.dataset.active = 'false';
    t.removeAttribute('data-paused');
  }

  function showUndoToast(count, sizeBytes, purge) {
    const t = document.getElementById('undo-toast');
    if (!t) return;
    const countEl = document.getElementById('undo-count');
    const sizeEl = document.getElementById('undo-size');
    const purgeTag = document.getElementById('undo-purge-tag');
    if (countEl) countEl.textContent = String(count);
    if (sizeEl) sizeEl.textContent = (sizeBytes / 1073741824).toFixed(2);
    if (purgeTag) purgeTag.hidden = !purge;
    /* Force the progress-bar animation to restart cleanly. Toggling data-active
       off then on schedules the rule that runs the keyframes without picking
       up the previous run's elapsed time. */
    t.dataset.active = 'false';
    t.removeAttribute('data-paused');
    /* eslint-disable-next-line no-unused-expressions */
    t.offsetWidth; // reflow
    t.dataset.active = 'true';
  }

  function applyDelete(purge) {
    const hashes = Array.from(selection.keys());
    if (hashes.length === 0) return;
    let sizeBytes = 0;
    for (const b of selection.values()) sizeBytes += b;
    /* If a previous delete is still pending, fire it now (FIFO) so we never
       have two outstanding bulk actions. The selection footer was already
       hidden after the previous click. */
    if (pending) commitPending(true);
    /* Soft-hide the marked rows so the toast doesn't compete with them
       visually. Keeping them in the DOM means Undo can restore instantly
       without a 1-3 s SSE round-trip. The reconciler's TORRENT_REMOVED
       event will fully delete them once the commit fires. */
    const dimmed = new Set(hashes);
    document.querySelectorAll('.torrent-row').forEach((row) => {
      const h = row.getAttribute('data-hash');
      if (h && dimmed.has(h)) row.classList.add('qf-pending-delete');
    });
    pending = {
      hashes,
      purge,
      sizeBytes,
      commitAt: Date.now() + UNDO_WINDOW_MS,
      timer: null,
    };
    pending.timer = setTimeout(() => commitPending(true), UNDO_WINDOW_MS);
    selection.clear();
    repaintSelectionFooter();
    showUndoToast(hashes.length, sizeBytes, purge);
  }

  function restorePendingRows() {
    if (!pending) return;
    const restore = new Set(pending.hashes);
    document.querySelectorAll('.torrent-row.qf-pending-delete').forEach((row) => {
      const h = row.getAttribute('data-hash');
      if (h && restore.has(h)) row.classList.remove('qf-pending-delete');
    });
  }

  /* Click handler: dedicated to the Undo button so we don't fight the
     bulk-action delegated click listener below. */
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest('#undo-action')) {
      e.stopPropagation();
      commitPending(false); // abort: do not send
    }
  });

  /* Hovering the toast pauses the countdown so the user has time to read it
     without the action firing under their nose. Leaving resumes. The CSS
     animation handles the visual pause; the JS timer still fires on time,
     so the maximum extra grace is whatever the user does in <a few hundred
     ms after un-hovering. Good enough -- the JS timer + CSS bar can drift
     a frame; we don't try to make them tick-perfect. */
  document.addEventListener('mouseenter', (e) => {
    const t = e.target;
    if (t instanceof Element && t.id === 'undo-toast') {
      t.setAttribute('data-paused', 'true');
    }
  }, true);
  document.addEventListener('mouseleave', (e) => {
    const t = e.target;
    if (t instanceof Element && t.id === 'undo-toast') {
      t.removeAttribute('data-paused');
    }
  }, true);

  /* If the tab is about to be closed mid-window, fire the request via
     sendBeacon so the user doesn't end up with "I clicked delete but it
     never happened" surprise. sendBeacon is the only reliable cross-browser
     way to ship a POST during pagehide. */
  window.addEventListener('pagehide', () => {
    if (!pending) return;
    try {
      const body = new URLSearchParams();
      body.set('hashes', pending.hashes.join('|'));
      body.set('purge', pending.purge ? '1' : '0');
      navigator.sendBeacon('/torrents/bulk/cleanup', body);
    } catch (e) { /* old browser -- best effort */ }
    pending = null;
  });

  function clearSelection() {
    selection.clear();
    document.querySelectorAll('.row-check:checked').forEach((cb) => {
      if (cb instanceof HTMLInputElement) {
        cb.checked = false;
        const row = cb.closest('.torrent-row');
        if (row) row.setAttribute('data-marked', 'false');
      }
    });
    repaintSelectionFooter();
  }

  /* keys.js is loaded inline above #selection-bar so the elements aren't in
     the DOM yet at IIFE-execution time. Use delegation on document so the
     handlers fire regardless of parse order (and would survive a re-render
     of the bar). */
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    const btn = t.closest('#sel-delete, #sel-purge, #sel-clear');
    if (!btn) return;
    if (btn.id === 'sel-delete') applyDelete(false);
    else if (btn.id === 'sel-purge') applyDelete(true);
    else clearSelection();
  });

  /* After a fresh swap into #groups (filter POST, rule preview, SSE RESYNC),
     auto-add every row the server marked as a rule match to the selection
     map and reflect that in checkbox state. The user can deselect mistakes
     before confirming bulk-delete.

     Gated on the swap *target* being #groups or rule-bar-slot specifically:
     per-row OOB swaps from the SSE storm hit this listener dozens of times
     per tick. Without the guard, every tick walked all 1310 rows looking
     for data-marked="true". With the guard, the scan only runs on the
     handful of swaps that actually replace the group payload, and we scope
     querySelectorAll to the swapped subtree instead of the whole document. */
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const target = e.detail && e.detail.target;
    if (!target) return;
    if (target.id !== 'groups' && target.id !== 'rule-bar-slot') return;
    const scope = target.id === 'groups' ? target : document;
    scope.querySelectorAll('.torrent-row[data-marked="true"]').forEach((row) => {
      const h = row.getAttribute('data-hash');
      if (h) selection.set(h, rowBytes(row));
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement) cb.checked = true;
    });
    repaintSelectionFooter();
  });

  /* The `selection` Map is the client-side source of truth. The server
     renders torrent rows without any selection context (render_torrent has
     no way to know what each client has selected), so every SSE row update
     comes back with data-marked="false" and an unchecked checkbox. Without
     this listener, any field change (progress/ratio/state) on a selected
     torrent would visibly unselect it ~1s later when the reconciler ticks.

     Restore selection state from the Map back onto the DOM for any
     .torrent-row that just got OOB-swapped (single row or a whole #groups
     re-render on RESYNC). Hashes not in `selection` are skipped, so user
     deselections persist. */
  function reapplySelectionTo(scope) {
    if (!scope || !(scope instanceof Element)) return;
    const rows = scope.classList.contains('torrent-row')
      ? [scope]
      : scope.querySelectorAll('.torrent-row');
    let dirty = false;
    rows.forEach((row) => {
      const h = row.getAttribute('data-hash');
      if (!h || !selection.has(h)) return;
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement && !cb.checked) cb.checked = true;
      if (row.getAttribute('data-marked') !== 'true') {
        row.setAttribute('data-marked', 'true');
      }
      // Refresh stored size: a row's bytes can change as it downloads, and
      // the footer total should track the latest server-reported value.
      selection.set(h, rowBytes(row));
      dirty = true;
    });
    if (dirty) repaintSelectionFooter();
  }

  document.body.addEventListener('htmx:oobAfterSwap', (e) => {
    reapplySelectionTo(e.target);
  });

  document.addEventListener('DOMContentLoaded', repaintSelectionFooter);

  /* =========================================================================
     Keyboard shortcuts + focused-row cursor + selection helpers.

     The user is triaging 600+ groups -- mouse-only doesn't scale. Bindings
     follow cross-tool conventions (Linear / Gmail / GitHub):
       j/k        next/prev row
       J/K        next/prev group
       x/Space    toggle focused row
       Shift+X    range select from last anchor
       Ctrl+A     select all visible
       a          select rule-flagged rows in focused group
       i          invert selection
       Enter      delete (keep files)
       u          undo pending delete
       1..9       activate Nth rule chip
       ?          toggle cheatsheet
       /          focus search   (already wired)
       Esc        cancel / close (already wired)
     ========================================================================= */

  let focusedHash = null;       // hash of the row currently keyboard-focused
  let rangeAnchorHash = null;   // anchor for Shift+X range select

  function visibleRows() {
    return Array.from(document.querySelectorAll('#groups .torrent-row'));
  }
  function visibleGroups() {
    return Array.from(document.querySelectorAll('#groups .group-card'));
  }

  function setFocusedRow(row) {
    if (!row) return;
    document.querySelectorAll('.torrent-row[data-focused="true"]').forEach((r) => {
      if (r !== row) r.removeAttribute('data-focused');
    });
    row.setAttribute('data-focused', 'true');
    focusedHash = row.getAttribute('data-hash');
    row.scrollIntoView({ block: 'nearest', behavior: 'auto' });
  }

  function focusedRowIndex(rows) {
    if (!focusedHash) return -1;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute('data-hash') === focusedHash) return i;
    }
    return -1;
  }

  function moveFocus(delta) {
    const rows = visibleRows();
    if (rows.length === 0) return;
    const idx = focusedRowIndex(rows);
    const next = idx < 0
      ? (delta > 0 ? 0 : rows.length - 1)
      : Math.max(0, Math.min(rows.length - 1, idx + delta));
    setFocusedRow(rows[next]);
  }

  function moveGroup(delta) {
    const groups = visibleGroups();
    if (groups.length === 0) return;
    const rows = visibleRows();
    const idx = focusedRowIndex(rows);
    /* Find the group containing the currently focused row; if none, jump
       to first/last group. */
    let currentGroup = -1;
    if (idx >= 0) {
      const card = rows[idx].closest('.group-card');
      currentGroup = groups.indexOf(card);
    }
    const target = currentGroup < 0
      ? (delta > 0 ? 0 : groups.length - 1)
      : Math.max(0, Math.min(groups.length - 1, currentGroup + delta));
    const first = groups[target].querySelector('.torrent-row');
    if (first) setFocusedRow(first);
  }

  function toggleRowSelection(row) {
    if (!row) return;
    const cb = row.querySelector('.row-check');
    if (!(cb instanceof HTMLInputElement)) return;
    cb.checked = !cb.checked;
    cb.dispatchEvent(new Event('change', { bubbles: true }));
    rangeAnchorHash = row.getAttribute('data-hash');
  }

  function rangeSelect(toRow) {
    if (!toRow) return;
    const rows = visibleRows();
    if (!rangeAnchorHash) {
      /* No anchor yet -- treat current row as the anchor and select it. */
      toggleRowSelection(toRow);
      return;
    }
    const from = rows.findIndex((r) => r.getAttribute('data-hash') === rangeAnchorHash);
    const to = rows.findIndex((r) => r === toRow);
    if (from < 0 || to < 0) { toggleRowSelection(toRow); return; }
    const [lo, hi] = from < to ? [from, to] : [to, from];
    for (let i = lo; i <= hi; i++) {
      const cb = rows[i].querySelector('.row-check');
      if (cb instanceof HTMLInputElement && !cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  }

  function selectAllVisible() {
    const rows = visibleRows();
    let touched = 0;
    rows.forEach((row) => {
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement && !cb.checked) {
        cb.checked = true;
        const h = row.getAttribute('data-hash');
        if (h) selection.set(h, rowBytes(row));
        row.setAttribute('data-marked', 'true');
        touched++;
      }
    });
    if (touched) repaintSelectionFooter();
  }

  function invertSelection() {
    const rows = visibleRows();
    rows.forEach((row) => {
      /* Skip rule-recommended keepers so an inverted selection doesn't
         accidentally pick the keeper. The keeper is, by design, the row
         the user wants to keep. */
      if (row.getAttribute('data-keeper') === 'true') return;
      const cb = row.querySelector('.row-check');
      if (!(cb instanceof HTMLInputElement)) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  function selectLosersInGroup(group) {
    if (!group) return;
    const rows = group.querySelectorAll('.torrent-row[data-marked="true"]');
    rows.forEach((row) => {
      if (row.getAttribute('data-keeper') === 'true') return;
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement && !cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  }

  function selectAllLosers() {
    const rows = document.querySelectorAll('#groups .torrent-row[data-marked="true"]');
    rows.forEach((row) => {
      if (row.getAttribute('data-keeper') === 'true') return;
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement && !cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  }

  function activateNthRule(n) {
    const chips = document.querySelectorAll('#rule-bar-slot .rule-chip:not([disabled])');
    const chip = chips[n - 1];
    if (chip instanceof HTMLElement) chip.click();
  }

  function toggleCheatsheet(force) {
    const cs = document.getElementById('kbd-cheatsheet');
    if (!cs) return;
    const open = force !== undefined ? force : cs.dataset.active !== 'true';
    cs.dataset.active = open ? 'true' : 'false';
    cs.setAttribute('aria-hidden', open ? 'false' : 'true');
  }

  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest('#kbd-close')) { toggleCheatsheet(false); return; }
    /* Backdrop click closes (target IS the overlay, not the inner card). */
    if (t.id === 'kbd-cheatsheet') { toggleCheatsheet(false); return; }
    if (t.closest('#sel-invert')) { e.stopPropagation(); invertSelection(); return; }
    if (t.closest('#sel-losers')) { e.stopPropagation(); selectAllLosers(); return; }
    const groupBtn = t.closest('.group-select-losers');
    if (groupBtn) {
      e.stopPropagation();
      const card = groupBtn.closest('.group-card');
      if (card) selectLosersInGroup(card);
      return;
    }
    /* Row focus follows mouse click on a row body (but not on checkbox /
       buttons inside the row). */
    const row = t.closest('.torrent-row');
    if (row && !(t.closest('.row-check, button, a, input'))) {
      setFocusedRow(row);
    }
  });

  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable);

    if (e.key === '/' && !typing) {
      const search = document.getElementById('search-input');
      if (search) { e.preventDefault(); search.focus(); search.select(); }
      return;
    }
    if (e.key === 'Escape') {
      const cs = document.getElementById('kbd-cheatsheet');
      if (cs && cs.dataset.active === 'true') { toggleCheatsheet(false); return; }
      const drawer = document.getElementById('filter-drawer');
      if (drawer && drawer.classList.contains('open')) { drawer.classList.remove('open'); return; }
      if (pending) { commitPending(false); return; }
      if (selection.size > 0) { clearSelection(); return; }
      const search = document.getElementById('search-input');
      if (search && document.activeElement === search) { search.blur(); }
      return;
    }
    if (typing) return; // all remaining bindings are single-letter; don't fight inputs

    /* Ctrl/Cmd+A: select all visible. Preventing the default Select-All is
       only safe when there's something to select; otherwise let the browser
       do its thing. */
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      const rows = visibleRows();
      if (rows.length === 0) return;
      e.preventDefault();
      selectAllVisible();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return; // ignore other modifier combos

    switch (e.key) {
      case 'j': case 'ArrowDown':
        e.preventDefault(); moveFocus(1); break;
      case 'k': case 'ArrowUp':
        e.preventDefault(); moveFocus(-1); break;
      case 'J':
        e.preventDefault(); moveGroup(1); break;
      case 'K':
        e.preventDefault(); moveGroup(-1); break;
      case 'x': case ' ': {
        const rows = visibleRows();
        const idx = focusedRowIndex(rows);
        if (idx >= 0) { e.preventDefault(); toggleRowSelection(rows[idx]); }
        break;
      }
      case 'X': {
        const rows = visibleRows();
        const idx = focusedRowIndex(rows);
        if (idx >= 0) { e.preventDefault(); rangeSelect(rows[idx]); }
        break;
      }
      case 'a': {
        const rows = visibleRows();
        const idx = focusedRowIndex(rows);
        if (idx >= 0) {
          const card = rows[idx].closest('.group-card');
          if (card) { e.preventDefault(); selectLosersInGroup(card); }
        }
        break;
      }
      case 'i':
        e.preventDefault(); invertSelection(); break;
      case 'u':
        if (pending) { e.preventDefault(); commitPending(false); }
        break;
      case 'Enter':
        if (selection.size > 0) { e.preventDefault(); applyDelete(false); }
        break;
      case '?':
        e.preventDefault(); toggleCheatsheet(); break;
      default:
        if (e.key >= '1' && e.key <= '9') {
          e.preventDefault();
          activateNthRule(parseInt(e.key, 10));
        }
    }
  });

  /* When the group list re-renders, the previously-focused row may be gone.
     Try to restore focus by hash; if the hash is missing, drop focus. */
  document.body.addEventListener('htmx:afterSwap', (e) => {
    const target = e.detail && e.detail.target;
    if (!target || target.id !== 'groups') return;
    if (!focusedHash) return;
    const row = document.querySelector('.torrent-row[data-hash="' + CSS.escape(focusedHash) + '"]');
    if (row) row.setAttribute('data-focused', 'true');
    else focusedHash = null;
  });

  /* =========================================================================
     SSE connection-state indicator.
     ========================================================================= */
  const live = document.querySelector('.app-bar .live');
  if (live) {
    document.body.addEventListener('htmx:sseOpen',  () => live.classList.remove('disconnected'));
    document.body.addEventListener('htmx:sseClose', () => live.classList.add('disconnected'));
    document.body.addEventListener('htmx:sseError', () => live.classList.add('disconnected'));
  }

  /* Strip the entrance-animation gate once it has fired. The class is set
     server-side only on freshly-inserted SSE nodes (new group cards, new
     torrent rows); removing it after `animationend` keeps the DOM tidy and
     avoids surprising future CSS or JS that might key off the same class. */
  document.body.addEventListener('animationend', (e) => {
    const t = e.target;
    if (t && t.classList && t.classList.contains('qf-enter')) {
      t.classList.remove('qf-enter');
    }
  });

  /* =========================================================================
     Viewport observer.

     Tracks which `.group-card` elements are visible and POSTs the hot-set
     (visible + 5 above + 5 below) to /viewport on a 250 ms debounce. The
     server uses this to drive ~3 s polling on hot keys and ~30 s on cold
     keys, keeping the snapshot CPU bounded regardless of catalogue size.
     ========================================================================= */
  (function viewportObserver() {
    const visible = new Set();
    let timer = null;

    function postViewport() {
      timer = null;
      const all = Array.from(document.querySelectorAll('.group-card'));
      if (all.length === 0) return;
      const idxByEl = new Map(all.map((el, i) => [el, i]));
      const idxs = Array.from(visible)
        .map((el) => idxByEl.get(el))
        .filter((i) => i !== undefined)
        .sort((a, b) => a - b);
      if (idxs.length === 0) return;
      const lo = Math.max(0, idxs[0] - 5);
      const hi = Math.min(all.length - 1, idxs[idxs.length - 1] + 5);
      const slugs = all.slice(lo, hi + 1).map((el) => el.id.replace(/^group-/, '')).join('|');
      const body = new URLSearchParams();
      body.set('keys', slugs);
      try { fetch('/viewport', { method: 'POST', body, keepalive: true }); } catch (e) { /* offline */ }
    }

    const obs = new IntersectionObserver((entries) => {
      for (const ent of entries) {
        if (ent.isIntersecting) visible.add(ent.target);
        else visible.delete(ent.target);
      }
      if (timer) clearTimeout(timer);
      timer = setTimeout(postViewport, 250);
    }, { rootMargin: '200px 0px' });

    /* IntersectionObserver.observe() on an already-observed node is a no-op,
       but the surrounding querySelectorAll('.group-card') still walks all
       622 cards. Under a RESYNC, htmx:oobAfterSwap fires dozens of times per
       second, so the unconditional re-attach burned a significant chunk of
       main-thread time. WeakSet keeps already-observed cards out of the
       observe() loop; the swap-target guard skips the walk entirely when a
       swap didn't touch #groups (most SSE swaps are per-row OOB updates,
       not whole-group rewrites, so the existing observers are still valid). */
    const observed = new WeakSet();
    const attach = (e) => {
      if (e && e.detail && e.detail.target) {
        const tgt = e.detail.target;
        if (tgt.id !== 'groups' && !(tgt.closest && tgt.closest('#groups'))) return;
      }
      document.querySelectorAll('.group-card').forEach((el) => {
        if (observed.has(el)) return;
        observed.add(el);
        obs.observe(el);
      });
    };
    document.addEventListener('DOMContentLoaded', attach);
    document.body.addEventListener('htmx:afterSwap', attach);
    document.body.addEventListener('htmx:oobAfterSwap', attach);
  })();
})();
