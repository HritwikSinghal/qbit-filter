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
     Batched-RESYNC load progress.

     Server emits the initial RESYNC as a sequence of SSE messages, each
     replacing #qf-batch-staging via hx-swap-oob="outerHTML" with a chunk of
     rendered group cards plus a refreshed #qf-load-progress block. Below we:

     1. Set a "Connecting..." placeholder if there's no card yet, so the user
        sees feedback while the first batch is in flight.
     2. Watch #qf-batch-staging swaps and move its children into #groups
        (replace if id exists, append otherwise). On the final batch, prune
        any DOM cards no longer in the canonical slug list.
     3. After each batch, re-fire htmx:afterSwap + htmx:oobAfterSwap with
        target=#groups so existing listeners (selection re-apply, viewport
        observer, marked-row scan, cache save) treat it like a real
        #groups-targeted swap.
     ========================================================================= */
  function buildFlCard(titleText, fillPct) {
    /* DOM construction (no innerHTML) for the "Connecting..." placeholder
       so the static-analysis hook doesn't flag this site. The server-side
       progress block uses identical class names and is rendered server-side
       in Jinja; this matches that markup. */
    const card = document.createElement('div');
    card.className = 'fl-card';
    card.setAttribute('role', 'status');
    card.setAttribute('aria-live', 'polite');
    const title = document.createElement('div');
    title.className = 'fl-title';
    title.textContent = titleText;
    const bar = document.createElement('div');
    bar.className = 'fl-bar';
    const fill = document.createElement('div');
    fill.className = 'fl-bar-fill';
    fill.style.width = fillPct + '%';
    bar.appendChild(fill);
    card.append(title, bar);
    return card;
  }

  function setupLoadProgress() {
    const groups = document.getElementById('groups');
    const progress = document.getElementById('qf-load-progress');
    if (!groups || !progress) {
      if (setupLoadProgress._r === undefined) setupLoadProgress._r = 0;
      if (setupLoadProgress._r++ > 200) return;
      requestAnimationFrame(setupLoadProgress);
      return;
    }
    /* Cache paint already inserts cards synchronously before this IIFE
       returns. If a card is present, the first batch will only swap in
       updates -- skip the "Connecting" placeholder so the user isn't
       shown a spinner over content they already have. */
    if (groups.querySelector('.group-card')) return;
    progress.replaceChildren(buildFlCard('Loading torrent list...', 5));

    /* Safety: if nothing arrives in 30 s, surface a hint instead of
       leaving the bar pinned at 5%. The first server batch will overwrite
       this. */
    setTimeout(() => {
      if (!groups || groups.querySelector('.group-card')) return;
      const card = document.createElement('div');
      card.className = 'fl-card';
      const t = document.createElement('div');
      t.className = 'fl-title';
      t.textContent = 'Stream not responding -- check server';
      card.appendChild(t);
      progress.replaceChildren(card);
    }, 30000);
  }
  setupLoadProgress();

  /* Move the latest batch's cards from #qf-batch-staging into #groups. */
  function applyBatchStaging(staging) {
    const groups = document.getElementById('groups');
    if (!groups || !staging) return;
    const isFinal = staging.getAttribute('data-final') === '1';
    const canonical = staging.getAttribute('data-canonical') || '';

    /* Children are already-rendered .group-card articles. We pick them off
       one at a time so the swap order is preserved if the server happens
       to send overlapping ids (it doesn't, but be defensive). */
    let card = staging.firstElementChild;
    while (card) {
      const next = card.nextElementSibling;
      const id = card.id;
      if (id) {
        const existing = document.getElementById(id);
        if (existing && existing !== card) {
          /* Same group as one we already have -- replace in place so the
             card keeps its DOM position and the progress block below
             doesn't jump. */
          existing.replaceWith(card);
        } else {
          /* New group -- append at the end of #groups. Batches arrive in
             canonical sort order so successive appends preserve order. */
          groups.appendChild(card);
        }
      }
      card = next;
    }

    if (isFinal && canonical) {
      /* Final batch: remove any DOM cards whose slug isn't in the canonical
         set. Covers groups that were deleted between RESYNCs. */
      const keep = new Set();
      for (const s of canonical.split('|')) {
        if (s) keep.add('group-' + s);
      }
      const toRemove = [];
      for (const child of groups.children) {
        if (child.id && child.id.startsWith('group-') && !keep.has(child.id)) {
          toRemove.push(child);
        }
      }
      for (const c of toRemove) c.remove();
    }

    /* Fire synthetic events so the existing handlers (selection re-apply,
       viewport observer, marked-row scan, focused-row restore, cache save)
       behave as if #groups was the swap target. Dispatched ON #groups so
       both e.target and e.detail.target resolve to it -- the existing
       handlers split on both. Otherwise none of them would see the new
       cards (htmx fired its event with the staging div as the target,
       which they all filter out). */
    const detail = { target: groups, elt: groups };
    groups.dispatchEvent(
      new CustomEvent('htmx:oobAfterSwap', { bubbles: true, detail }),
    );
    groups.dispatchEvent(
      new CustomEvent('htmx:afterSwap', { bubbles: true, detail }),
    );
  }

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
     Selection model + visible-row caching.

     The DOM is rerendered piece-by-piece via SSE/HTMX. The source of truth is
     the in-memory `selection` map; after every relevant swap we re-paint
     checkboxes & data-marked attributes from it so selections survive
     partial re-renders.

     Map<hash, bytes>. Bytes are captured at check-time from the row's
     data-bytes attribute so the footer can sum them without re-querying the
     DOM per selected hash. Avoids an O(N) `document.querySelector` storm on
     every selection change when 100+ rows are selected.
     ========================================================================= */
  const selection = new Map();
  let rangeAnchorHash = null;
  let focusedRowEl = null;
  let focusedHash = null;

  /* Cached visible-row / -group arrays. Keyboard navigation (j/k/J/K/x/X/a)
     used to walk all 1310 rows per keypress; one cache + targeted
     invalidation drops that to one walk per real swap. */
  let _rowsCache = null;
  let _groupsCache = null;
  function invalidateVisibleCache() { _rowsCache = null; _groupsCache = null; }
  function visibleRows() {
    if (_rowsCache) return _rowsCache;
    _rowsCache = Array.from(document.querySelectorAll('#groups .torrent-row'));
    return _rowsCache;
  }
  function visibleGroups() {
    if (_groupsCache) return _groupsCache;
    _groupsCache = Array.from(document.querySelectorAll('#groups .group-card'));
    return _groupsCache;
  }

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
    const h = row && row.getAttribute('data-hash');
    if (!h) return;
    if (t.checked) selection.set(h, rowBytes(row));
    else selection.delete(h);
    if (row) row.setAttribute('data-marked', t.checked ? 'true' : 'false');
    repaintSelectionFooter();
  });

  /* =========================================================================
     Confirm-delete dialog. Opens from the selection bar's Delete / Delete +
     purge buttons; shows a grouped summary of what is about to be removed
     before firing POST /torrents/bulk/cleanup.
     ========================================================================= */
  let confirmPurge = false;
  let confirmTrigger = null;

  function confirmOpen() {
    const modal = document.getElementById('confirm-delete');
    return !!(modal && modal.dataset.active === 'true');
  }

  function groupTitleOf(card) {
    if (!card) return 'Other';
    const titleEl = card.querySelector('.group-meta .title');
    return titleEl ? titleEl.textContent.trim() : (card.id || 'Group');
  }

  function buildConfirmRowItem(row) {
    const li = document.createElement('li');

    /* Quality badge: clone existing styled node so the modal item matches
       the row's tier color without duplicating CSS. */
    const badge = row.querySelector('.q-badge');
    if (badge) li.appendChild(badge.cloneNode(true));

    /* Name: text only -- the row's .name may contain a keeper badge span
       we don't need in the summary. */
    const name = document.createElement('span');
    name.className = 'confirm-name';
    const nameEl = row.querySelector('.name');
    name.textContent = nameEl ? nameEl.textContent.trim() : (row.getAttribute('data-hash') || '');
    name.title = name.textContent;
    li.appendChild(name);

    /* Size from cached bytes (no DOM walk). */
    const size = document.createElement('span');
    size.className = 'confirm-size-cell';
    const bytes = rowBytes(row);
    size.textContent = bytes ? (bytes / 1073741824).toFixed(2) + ' GB' : '';
    li.appendChild(size);

    /* Reason factors: clone existing pill structure. Falls back to the
       row's title attribute as a single chip when no factor pills exist. */
    const factors = row.querySelector('.reason-factors');
    if (factors) {
      li.appendChild(factors.cloneNode(true));
    } else {
      const reason = row.getAttribute('title');
      if (reason) {
        const r = document.createElement('span');
        r.className = 'reason-chip';
        r.textContent = reason;
        li.appendChild(r);
      } else {
        /* Keep grid columns aligned even when no reason -- empty span. */
        li.appendChild(document.createElement('span'));
      }
    }
    return li;
  }

  function openConfirmDialog(purge, triggerBtn) {
    if (selection.size === 0) return;
    const modal = document.getElementById('confirm-delete');
    if (!modal) return;

    confirmPurge = !!purge;
    confirmTrigger = triggerBtn instanceof HTMLElement ? triggerBtn : null;

    /* Group selected hashes by their owning .group-card via O(1) lookups. */
    const groups = new Map();
    let totalBytes = 0;
    let rowsCount = 0;
    for (const h of selection.keys()) {
      const row = document.getElementById('torrent-' + h);
      if (!row) continue;
      const card = row.closest('.group-card');
      if (!groups.has(card)) groups.set(card, []);
      groups.get(card).push(row);
      totalBytes += rowBytes(row);
      rowsCount++;
    }

    const countEl = document.getElementById('confirm-count');
    if (countEl) countEl.textContent = String(rowsCount);
    const sizeEl = document.getElementById('confirm-size');
    if (sizeEl) sizeEl.textContent = (totalBytes / 1073741824).toFixed(2);
    const purgeTag = document.getElementById('confirm-purge-tag');
    if (purgeTag) purgeTag.hidden = !confirmPurge;
    const goBtn = document.getElementById('confirm-go');
    if (goBtn) goBtn.textContent = confirmPurge ? 'Delete + purge files' : 'Delete';

    const body = document.getElementById('confirm-body');
    if (body) {
      body.replaceChildren();
      for (const [card, rows] of groups) {
        const section = document.createElement('section');
        const h3 = document.createElement('h3');
        const titleText = document.createTextNode(groupTitleOf(card));
        h3.appendChild(titleText);
        if (rows.length > 1) {
          const cnt = document.createElement('span');
          cnt.className = 'group-count';
          cnt.textContent = '(' + rows.length + ')';
          h3.appendChild(cnt);
        }
        section.appendChild(h3);
        const ul = document.createElement('ul');
        for (const r of rows) ul.appendChild(buildConfirmRowItem(r));
        section.appendChild(ul);
        body.appendChild(section);
      }
    }

    modal.dataset.active = 'true';
    modal.setAttribute('aria-hidden', 'false');
    /* Focus the action button on the next tick so Tab/Shift+Tab focus-trap
       starts from a known anchor and Enter fires the button immediately. */
    requestAnimationFrame(() => {
      const f = document.getElementById('confirm-go');
      if (f instanceof HTMLElement) f.focus();
    });
  }

  function closeConfirmDialog() {
    const modal = document.getElementById('confirm-delete');
    if (!modal) return;
    modal.dataset.active = 'false';
    modal.setAttribute('aria-hidden', 'true');
    if (confirmTrigger && confirmTrigger.isConnected) {
      confirmTrigger.focus();
    }
    confirmTrigger = null;
  }

  async function commitDelete() {
    const hashes = Array.from(selection.keys());
    if (hashes.length === 0) { closeConfirmDialog(); return; }
    const purge = confirmPurge;
    closeConfirmDialog();
    /* Clear selection immediately so the footer collapses -- SSE will
       retire the rows shortly. Direct getElementById lookups skip the
       O(N) `.row-check:checked` walk. */
    for (const h of hashes) {
      const row = document.getElementById('torrent-' + h);
      if (!row) continue;
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement) cb.checked = false;
      row.setAttribute('data-marked', 'false');
    }
    selection.clear();
    repaintSelectionFooter();
    const body = new URLSearchParams();
    body.set('hashes', hashes.join('|'));
    body.set('purge', purge ? '1' : '0');
    try {
      const res = await fetch('/torrents/bulk/cleanup', { method: 'POST', body });
      if (!res.ok) alert('Cleanup failed: HTTP ' + res.status);
    } catch (err) {
      alert('Cleanup failed: ' + err);
    }
  }

  /* clearSelection: O(k) direct lookups instead of a doc-wide
     `.row-check:checked` walk. */
  function clearSelection() {
    for (const h of selection.keys()) {
      const row = document.getElementById('torrent-' + h);
      if (!row) continue;
      const cb = row.querySelector('.row-check');
      if (cb instanceof HTMLInputElement) cb.checked = false;
      row.setAttribute('data-marked', 'false');
    }
    selection.clear();
    repaintSelectionFooter();
  }

  /* =========================================================================
     Selection helpers (keyboard + multi-select).
     ========================================================================= */
  function setFocusedRow(row) {
    if (!row || row === focusedRowEl) {
      if (row) row.scrollIntoView({ block: 'nearest', behavior: 'auto' });
      return;
    }
    if (focusedRowEl && focusedRowEl.isConnected) {
      focusedRowEl.removeAttribute('data-focused');
    }
    focusedRowEl = row;
    focusedHash = row.getAttribute('data-hash');
    row.setAttribute('data-focused', 'true');
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

  /* Range selection -- shift-click / Shift+X. With `forceChecked = true`
     all in-range rows are set TRUE (Finder / Gmail semantics: shift-click
     extends the selection rather than toggling). */
  function rangeSelectTo(toRow, forceChecked) {
    if (!toRow) return;
    const rows = visibleRows();
    if (!rangeAnchorHash) {
      rangeAnchorHash = toRow.getAttribute('data-hash');
      toggleRowSelection(toRow);
      return;
    }
    let from = -1, to = -1;
    for (let i = 0; i < rows.length; i++) {
      const h = rows[i].getAttribute('data-hash');
      if (h === rangeAnchorHash) from = i;
      if (rows[i] === toRow) to = i;
      if (from >= 0 && to >= 0) break;
    }
    if (from < 0 || to < 0) { toggleRowSelection(toRow); return; }
    const [lo, hi] = from < to ? [from, to] : [to, from];
    for (let i = lo; i <= hi; i++) {
      const cb = rows[i].querySelector('.row-check');
      if (cb instanceof HTMLInputElement && cb.checked !== forceChecked) {
        cb.checked = forceChecked;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    rangeAnchorHash = toRow.getAttribute('data-hash');
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
         accidentally pick the keeper. */
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

  /* =========================================================================
     Unified click dispatch -- replaces three former document-level click
     handlers (undo button, bulk-action buttons, cheatsheet + row focus +
     per-group buttons). Branches in cheapest-check-first order.
     ========================================================================= */
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;

    /* 1. Row checkbox: native toggle, but record anchor and intercept
       Shift+click to do a range-extend instead of a single toggle.
       `preventDefault` on the click event cancels the browser's own
       toggle of the checkbox state. */
    if (t instanceof HTMLInputElement && t.classList.contains('row-check')) {
      const row = t.closest('.torrent-row');
      if (!row) return;
      if (e.shiftKey) {
        e.preventDefault();
        rangeSelectTo(row, true);
        return;
      }
      rangeAnchorHash = row.getAttribute('data-hash');
      return;
    }

    /* 2. Confirm-delete modal: backdrop click, action buttons. */
    if (t.id === 'confirm-delete') { closeConfirmDialog(); return; }
    if (t.closest('#confirm-cancel')) { e.stopPropagation(); closeConfirmDialog(); return; }
    if (t.closest('#confirm-go')) { e.stopPropagation(); commitDelete(); return; }

    /* 3. Cheatsheet overlay: backdrop or close button. */
    if (t.closest('#kbd-close') || t.id === 'kbd-cheatsheet') {
      toggleCheatsheet(false);
      return;
    }

    /* 4. Selection-bar action buttons. */
    const sbBtn = t.closest('#sel-delete, #sel-purge, #sel-clear, #sel-invert, #sel-losers');
    if (sbBtn) {
      e.stopPropagation();
      switch (sbBtn.id) {
        case 'sel-delete': openConfirmDialog(false, sbBtn); break;
        case 'sel-purge':  openConfirmDialog(true, sbBtn);  break;
        case 'sel-clear':  clearSelection();                break;
        case 'sel-invert': invertSelection();               break;
        case 'sel-losers': selectAllLosers();               break;
      }
      return;
    }

    /* 5. Per-group "select losers" button. */
    const groupBtn = t.closest('.group-select-losers');
    if (groupBtn) {
      e.stopPropagation();
      const card = groupBtn.closest('.group-card');
      if (card) selectLosersInGroup(card);
      return;
    }

    /* 6. Row body: shift-click range, ctrl/cmd-click toggle, plain click
       just focuses. Skip clicks on interactive descendants (buttons,
       links, checkbox -- handled above, anchors). */
    if (t.closest('button, a, input')) return;
    const row = t.closest('.torrent-row');
    if (!row) return;
    if (e.shiftKey) {
      e.preventDefault();
      rangeSelectTo(row, true);
      setFocusedRow(row);
      return;
    }
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      toggleRowSelection(row);
      setFocusedRow(row);
      return;
    }
    setFocusedRow(row);
  });

  /* =========================================================================
     Keyboard shortcuts + focused-row cursor + selection helpers.

     j/k        next/prev row
     J/K        next/prev group
     x/Space    toggle focused row
     Shift+X    range select from last anchor
     Ctrl+A     select all visible
     a          select rule-flagged rows in focused group
     i          invert selection
     Enter      open confirm-delete (or commit when modal is open & focused)
     1..9       activate Nth rule chip
     ?          toggle cheatsheet
     /          focus search
     Esc        close modal / cheatsheet / drawer / clear selection
     ========================================================================= */
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable);

    /* Focus trap for the confirm-delete modal: cycle Tab / Shift+Tab between
       Cancel and Delete. Two-element trap -- no generic focusable scan. */
    if (e.key === 'Tab' && confirmOpen()) {
      const cancel = document.getElementById('confirm-cancel');
      const go = document.getElementById('confirm-go');
      if (cancel && go) {
        const active = document.activeElement;
        if (e.shiftKey) {
          if (active === cancel) { e.preventDefault(); go.focus(); }
        } else {
          if (active === go) { e.preventDefault(); cancel.focus(); }
        }
      }
      return;
    }

    if (e.key === '/' && !typing) {
      const search = document.getElementById('search-input');
      if (search) { e.preventDefault(); search.focus(); search.select(); }
      return;
    }

    if (e.key === 'Escape') {
      if (confirmOpen()) { e.preventDefault(); closeConfirmDialog(); return; }
      const cs = document.getElementById('kbd-cheatsheet');
      if (cs && cs.dataset.active === 'true') { toggleCheatsheet(false); return; }
      const drawer = document.getElementById('filter-drawer');
      if (drawer && drawer.classList.contains('open')) { drawer.classList.remove('open'); return; }
      if (selection.size > 0) { clearSelection(); return; }
      const search = document.getElementById('search-input');
      if (search && document.activeElement === search) { search.blur(); }
      return;
    }
    if (typing) return; // all remaining bindings are single-letter; don't fight inputs

    /* Ctrl/Cmd+A: select all visible. */
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
        if (idx >= 0) { e.preventDefault(); rangeSelectTo(rows[idx], true); }
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
      case 'Enter':
        if (selection.size > 0 && !confirmOpen()) {
          e.preventDefault();
          openConfirmDialog(false, document.getElementById('sel-delete'));
        }
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

  /* =========================================================================
     Post-swap restoration: selection re-apply + focused-row restore +
     auto-select rule-flagged rows + visible-cache invalidation.
     One handler each for afterSwap and oobAfterSwap (down from two each).
     ========================================================================= */
  function reapplySelectionTo(scope) {
    if (!scope || !(scope instanceof Element)) return;
    const rows = scope.classList && scope.classList.contains('torrent-row')
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
      selection.set(h, rowBytes(row));
      dirty = true;
    });
    if (dirty) repaintSelectionFooter();
  }

  function touchedGroups(target) {
    if (!target || !(target instanceof Element)) return false;
    if (target.id === 'groups') return true;
    return !!(target.closest && target.closest('#groups'));
  }

  document.body.addEventListener('htmx:afterSwap', (e) => {
    const target = e.detail && e.detail.target;
    if (!target) return;

    if (target.id === 'groups' || target.id === 'rule-bar-slot') {
      /* On a full #groups / rule-bar replacement, auto-add rule-flagged
         rows to selection (rule-cleanup UX) and restore focused-row
         outline by hash. */
      const scope = target.id === 'groups' ? target : document;
      scope.querySelectorAll('.torrent-row[data-marked="true"]').forEach((row) => {
        const h = row.getAttribute('data-hash');
        if (h) selection.set(h, rowBytes(row));
        const cb = row.querySelector('.row-check');
        if (cb instanceof HTMLInputElement) cb.checked = true;
      });
      repaintSelectionFooter();

      if (target.id === 'groups' && focusedHash) {
        const row = document.querySelector(
          '.torrent-row[data-hash="' + CSS.escape(focusedHash) + '"]',
        );
        if (row) {
          focusedRowEl = row;
          row.setAttribute('data-focused', 'true');
        } else {
          focusedRowEl = null;
          focusedHash = null;
        }
      }
    }

    if (touchedGroups(target)) invalidateVisibleCache();
  });

  document.body.addEventListener('htmx:oobAfterSwap', (e) => {
    const target = e.detail && e.detail.target;
    if (!target) return;

    /* Batch-staging OOB swap: relay children into #groups. Synthetic
       afterSwap/oobAfterSwap events dispatched inside applyBatchStaging
       re-enter this listener with target.id === 'groups', so the rest of
       the flow (selection re-apply, viewport observer) still fires. */
    if (target.id === 'qf-batch-staging') {
      requestAnimationFrame(() => applyBatchStaging(target));
      return;
    }

    /* Single-row or partial OOB swap: re-apply selection state from the
       in-memory Map so SSE row updates don't visibly unselect rows. */
    reapplySelectionTo(e.target);

    if (touchedGroups(target)) invalidateVisibleCache();
  });

  document.addEventListener('DOMContentLoaded', repaintSelectionFooter);

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
