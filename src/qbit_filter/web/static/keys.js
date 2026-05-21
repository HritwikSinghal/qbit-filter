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

  async function applyDelete(purge) {
    const hashes = Array.from(selection.keys());
    if (hashes.length === 0) return;
    const ok = confirm(`Delete ${hashes.length} torrent(s)${purge ? ' AND their files on disk' : ''}?`);
    if (!ok) return;
    const body = new URLSearchParams();
    body.set('hashes', hashes.join('|'));
    body.set('purge', purge ? '1' : '0');
    let res;
    try {
      res = await fetch('/torrents/bulk/cleanup', { method: 'POST', body });
    } catch (err) {
      alert('Cleanup failed: ' + err);
      return;
    }
    if (!res.ok) { alert('Cleanup failed: HTTP ' + res.status); return; }
    selection.clear();
    repaintSelectionFooter();
  }

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

  document.addEventListener('DOMContentLoaded', repaintSelectionFooter);

  /* =========================================================================
     Keyboard shortcuts.
     - `/` focuses the search input.
     - Escape closes the drawer, clears selection, or blurs search.
     ========================================================================= */
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable);

    if (e.key === '/' && !typing) {
      const search = document.getElementById('search-input');
      if (search) { e.preventDefault(); search.focus(); search.select(); }
      return;
    }
    if (e.key === 'Escape') {
      const drawer = document.getElementById('filter-drawer');
      if (drawer && drawer.classList.contains('open')) { drawer.classList.remove('open'); return; }
      if (selection.size > 0) { clearSelection(); return; }
      const search = document.getElementById('search-input');
      if (search && document.activeElement === search) { search.blur(); }
    }
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
