(function () {
  /* =========================================================================
     qfLog -- thin debug logger gated on ``?qf_debug=1`` or
     ``localStorage.qf_debug='1'``. Off by default so production console
     stays quiet; on once flagged, prints every selection change, MO
     pruning decision, batch staging boundary, and auto-select run. Use
     when chasing SSE/selection/streaming races -- this is the kind of
     scope where stepping through with a debugger lies because the bugs
     are micro-task ordered.

     Errors and warnings always go through console directly so flags
     don't suppress real problems. */
  const qfDebug = (() => {
    try {
      const q = new URLSearchParams(window.location.search);
      if (q.get('qf_debug') === '1') {
        localStorage.setItem('qf_debug', '1');
        return true;
      }
      if (q.get('qf_debug') === '0') {
        localStorage.removeItem('qf_debug');
        return false;
      }
      return localStorage.getItem('qf_debug') === '1';
    } catch (e) { return false; }
  })();
  const qfLog = {
    debug: qfDebug
      ? (...a) => console.debug('[qf]', ...a)
      : () => {},
    info: (...a) => console.info('[qf]', ...a),
    warn: (...a) => console.warn('[qf]', ...a),
    error: (...a) => console.error('[qf]', ...a),
    enabled: qfDebug,
  };
  window.qfLog = qfLog;
  if (qfDebug) qfLog.info('debug logging enabled');

  /* Shift-click rewrites the ``facet`` parameter to ``not_<facet>`` so the
     same HTMX hx-vals payload can serve both include and exclude. */
  document.body.addEventListener('htmx:configRequest', (e) => {
    const detail = e.detail;
    const path = detail && detail.path;
    if (typeof path !== 'string' || path.indexOf('/filters') !== 0) return;
    const evt = detail.triggeringEvent;
    const params = detail.parameters;
    if (!evt || !evt.shiftKey || !params) return;
    const f = params.facet;
    if (typeof f === 'string' && !f.startsWith('not_') && f !== 'search' && f !== 'min_torrents') {
      params.facet = 'not_' + f;
    }
  });

  /* =========================================================================
     Batched-RESYNC stream + header activity widget.

     Server emits the initial RESYNC as a sequence of SSE messages, each
     replacing #qf-batch-staging via hx-swap-oob="outerHTML" with a chunk of
     rendered group cards plus a refreshed #qf-activity block. Below we:

     1. Stamp a one-shot "Stream not responding" fallback into the header
        activity widget if no SSE message has arrived in 30 s.
     2. Watch #qf-batch-staging swaps and move its children into #groups
        (replace if id exists, append otherwise). On the final batch, prune
        any DOM cards no longer in the canonical slug list.
     3. After each batch, re-fire htmx:afterSwap + htmx:oobAfterSwap with
        target=#groups so existing listeners (selection re-apply, viewport
        observer, marked-row scan) treat it like a real #groups-targeted
        swap.
     4. Re-apply the activity panel's open/close state after the server's
        outerHTML swap of #qf-activity, so a user who clicked the button
        doesn't see the panel snap shut on every RESYNC.
     ========================================================================= */

  /* Stale-stream guard. The server-rendered chrome ships with the activity
     widget already in "active" state; if no SSE message lands within 30 s,
     surface that in the panel summary so the user has a hint instead of
     staring at an indeterminate bar. Cleared once any SSE message arrives. */
  let stalePaintTimer = setTimeout(() => {
    const summary = document.querySelector('#qf-activity-panel .qf-activity-summary');
    const label = document.querySelector('#qf-activity-btn .qf-activity-button-label');
    const widget = document.getElementById('qf-activity');
    if (widget) widget.setAttribute('data-state', 'stalled');
    if (label) label.textContent = 'Stream not responding';
    if (summary) summary.textContent = 'No SSE message received in 30 s -- check the server';
  }, 30000);
  document.body.addEventListener('htmx:sseMessage', () => {
    if (stalePaintTimer) { clearTimeout(stalePaintTimer); stalePaintTimer = null; }
  });

  /* Activity panel toggle. The server rewrites #qf-activity outerHTML on
     every RESYNC, which drops aria-expanded / hidden / data-open back to
     their defaults. We hold the open state in JS and re-apply it after
     each swap so the panel survives background updates. */
  let activityPanelOpen = false;

  function applyActivityPanelState(opts) {
    const widget = document.getElementById('qf-activity');
    const btn = document.getElementById('qf-activity-btn');
    const panel = document.getElementById('qf-activity-panel');
    if (!widget || !btn || !panel) return;
    /* Visibility is CSS-driven via [data-open] on the widget so the
       panel can animate via transform+opacity. We only manage the
       data attribute + ARIA state here; the ``hidden`` attribute is
       NOT used because it would short-circuit the CSS transition.

       ``animate: false`` is passed by the post-SSE-swap re-apply
       path: the server re-renders the widget with data-open="false",
       so without suppressing transitions the panel would visibly
       re-open on every chunk. We disable the transition for one
       frame, set the state, force reflow, then restore. */
    const animate = !opts || opts.animate !== false;
    if (!animate) {
      panel.style.transition = 'none';
    }
    widget.setAttribute('data-open', activityPanelOpen ? 'true' : 'false');
    btn.setAttribute('aria-expanded', activityPanelOpen ? 'true' : 'false');
    panel.setAttribute('aria-hidden', activityPanelOpen ? 'false' : 'true');
    if (!animate) {
      // Force layout flush so the no-transition style is committed
      // before we restore the original CSS-driven transition.
      void panel.offsetHeight;
      panel.style.transition = '';
    }
  }

  function setActivityPanel(open) {
    activityPanelOpen = !!open;
    applyActivityPanelState({ animate: true });
  }

  /* Click-to-toggle on the button. Anywhere else (or Esc) closes. */
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest('#qf-activity-btn')) {
      e.preventDefault();
      setActivityPanel(!activityPanelOpen);
      return;
    }
    if (activityPanelOpen && !t.closest('#qf-activity-panel') && !t.closest('#qf-activity')) {
      setActivityPanel(false);
    }
  });
  /* Escape closure for the activity panel is folded into the main
     Escape chain below (search for "Escape" in the main keydown
     handler). Keeping a single listener avoids two listeners racing on
     the same key and makes the priority order explicit. */
  /* htmx fires oobAfterSwap with target = the OOB'd element. When that's
     #qf-activity (or a descendant in the new tree), re-apply our open
     state. The swap drops listeners attached to the OLD button -- the
     delegated document-click handler above keeps working because it
     targets the id, not the element identity. */
  document.body.addEventListener('htmx:oobAfterSwap', (e) => {
    const target = e.detail && e.detail.target;
    if (target && (target.id === 'qf-activity' || (target.closest && target.closest('#qf-activity')))) {
      applyActivityPanelState();
      refreshActivityTimestamps();
    }
  });

  /* Relative-time refresher for the service cards inside the activity
     panel. Each .qf-service-ts carries data-ts (unix seconds) and an
     optional data-prefix ("last poll"); the server-rendered text is a
     fallback for the moment before this tick fires. Running every 5 s
     keeps "12s ago" / "2m ago" accurate without churning the DOM. */
  function formatRelative(secAgo) {
    if (!isFinite(secAgo) || secAgo < 0) return 'just now';
    if (secAgo < 2) return 'just now';
    if (secAgo < 60) return Math.floor(secAgo) + 's ago';
    if (secAgo < 3600) return Math.floor(secAgo / 60) + 'm ago';
    if (secAgo < 86400) return Math.floor(secAgo / 3600) + 'h ago';
    return Math.floor(secAgo / 86400) + 'd ago';
  }
  function refreshActivityTimestamps() {
    const now = Date.now() / 1000;
    const nodes = document.querySelectorAll('#qf-activity-panel .qf-service-ts');
    for (const node of nodes) {
      const ts = parseFloat(node.getAttribute('data-ts') || '0');
      if (!ts) { node.textContent = ''; continue; }
      const prefix = node.getAttribute('data-prefix') || '';
      const rel = formatRelative(now - ts);
      node.textContent = prefix ? prefix + ' ' + rel : rel;
    }
  }
  refreshActivityTimestamps();
  setInterval(refreshActivityTimestamps, 5000);

  /* Move the latest batch's cards from #qf-batch-staging into #groups. */
  function applyBatchStaging(staging) {
    const groups = document.getElementById('groups');
    if (!groups || !staging) {
      qfLog.debug('applyBatchStaging skip', { hasGroups: !!groups, hasStaging: !!staging });
      return;
    }
    const isFinal = staging.getAttribute('data-final') === '1';
    const canonical = staging.getAttribute('data-canonical') || '';
    const childCount = staging.childElementCount;
    qfLog.debug('applyBatchStaging', {
      isFinal, childCount,
      loaded: staging.getAttribute('data-loaded'),
      total: staging.getAttribute('data-total'),
    });

    /* Relocate the rule-activation marker before iterating cards. The
       server emits ``<span id="qf-rule-activation">`` inside the staging
       div so that a streamed rule preview can mark "this swap is an
       activation" without an extra OOB target. Without this relocation
       the marker stays in the staging container and gets discarded on the
       next OOB swap, so the streaming-path activation never auto-selects
       flagged rows. The flat (non-streamed) preview already places the
       marker inside #groups via innerHTML so this is a no-op there. */
    const marker = staging.querySelector('#qf-rule-activation');
    if (marker) groups.appendChild(marker);

    /* Children are already-rendered .group-card articles. We pick them off
       one at a time so the swap order is preserved if the server happens
       to send overlapping ids (it doesn't, but be defensive). */
    let card = staging.firstElementChild;
    while (card) {
      const next = card.nextElementSibling;
      const id = card.id;
      if (id === 'qf-rule-activation') {
        card = next;
        continue;
      }
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
      /* Prune stale entries from the selection Map. Hashes whose rows
         vanished between RESYNCs (deleted in another tab, removed via a
         qBit-side action we just committed) would otherwise linger in the
         Map; the footer would advertise rows that no longer exist and a
         subsequent /torrents/bulk/cleanup would 404 silently. */
      pruneStaleSelection();
    }

    /* Fire synthetic events so the existing handlers (selection re-apply,
       viewport observer, marked-row scan, focused-row restore) behave as
       if #groups was the swap target. Dispatched ON #groups so
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
    if (!sizeSpan) return 0;
    const n = parseInt(sizeSpan.getAttribute('data-bytes') || '0', 10);
    return Number.isNaN(n) ? 0 : n;
  }

  /* Drop selection entries whose torrent rows are no longer in the DOM.
     Called after the canonical RESYNC prune and from the MutationObserver
     on #groups; keeps the footer count and the next bulk-action's hash
     list honest. */
  function pruneStaleSelection() {
    let removed = 0;
    for (const h of Array.from(selection.keys())) {
      if (!document.getElementById('torrent-' + h)) {
        selection.delete(h);
        removed++;
      }
    }
    if (removed) repaintSelectionFooter();
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
    repaintMasterSelect();
  }

  /* Master select-all checkbox in the active-filters strip. Reflects the
     selection state for visible non-keeper rows; clicking it either selects
     every visible torrent or clears the selection entirely. */
  function repaintMasterSelect() {
    const cb = document.getElementById('master-select');
    const lbl = document.getElementById('master-select-label');
    if (!cb || !(cb instanceof HTMLInputElement)) return;
    const rows = visibleRows().filter(
      (r) => r.getAttribute('data-keeper') !== 'true',
    );
    const total = rows.length;
    let selected = 0;
    for (const row of rows) {
      const h = row.getAttribute('data-hash');
      if (h && selection.has(h)) selected++;
    }
    if (selected === 0) {
      cb.checked = false;
      cb.indeterminate = false;
      if (lbl) lbl.textContent = 'Select all';
    } else if (selected === total && total > 0) {
      cb.checked = true;
      cb.indeterminate = false;
      if (lbl) lbl.textContent = 'Clear selection (' + selected + ')';
    } else {
      cb.checked = false;
      cb.indeterminate = true;
      if (lbl) lbl.textContent = 'Clear selection (' + selected + ')';
    }
  }

  /* Event delegation works for SSE-injected rows too. */
  document.body.addEventListener('change', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLInputElement)) return;

    /* Master select-all toggle. Drive the action from selection.size, not
       from the post-click `t.checked`: when the checkbox is in the
       indeterminate (partial) state the browser flips checked to true on
       click, which would silently turn a user-intended "Clear selection
       (N)" press into a select-all. The label already reflects intent --
       anything selected -> "Clear selection", nothing selected -> "Select
       all" -- so trust that. repaintMasterSelect() restores the checkbox
       visual state after the action. */
    if (t.id === 'master-select') {
      if (selection.size > 0) {
        clearSelection();
      } else {
        selectAllVisible();
      }
      return;
    }

    if (!t.classList.contains('row-check')) return;
    const row = t.closest('.torrent-row');
    const h = row && row.getAttribute('data-hash');
    if (!h) return;
    if (t.checked) selection.set(h, rowBytes(row));
    else selection.delete(h);
    qfLog.debug('selection change', { hash: h.slice(0, 8), checked: t.checked, size: selection.size });
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
     extends the selection rather than toggling).

     Scope: when both the anchor and the target live inside the same
     compare-strip column (e.g., both in the FLAGGED column of a rule
     preview), restrict the walked rows to that column so the range
     doesn't cross the keeper/flagged divide and accidentally toggle the
     keeper row. Otherwise fall back to the document-order visible row
     list. */
  function rangeSelectTo(toRow, forceChecked) {
    if (!toRow) return;
    if (!rangeAnchorHash) {
      rangeAnchorHash = toRow.getAttribute('data-hash');
      toggleRowSelection(toRow);
      return;
    }
    const anchorRow = document.querySelector(
      '.torrent-row[data-hash="' + CSS.escape(rangeAnchorHash) + '"]',
    );
    let rows = visibleRows();
    if (anchorRow) {
      const anchorCol = anchorRow.closest('.compare-col');
      const targetCol = toRow.closest('.compare-col');
      if (anchorCol && anchorCol === targetCol) {
        rows = Array.from(anchorCol.querySelectorAll('.torrent-row'));
      } else if (anchorCol || targetCol) {
        /* Anchor and target are in different compare columns (or only one
           is). Refuse to bridge them -- the user shift-clicked across
           the keeper/flagged divide, which is almost always accidental.
           Reset the anchor to the new target so the next shift-click
           starts a fresh range inside the new column. */
        toggleRowSelection(toRow);
        rangeAnchorHash = toRow.getAttribute('data-hash');
        return;
      }
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
      /* Skip keeper rows even inside a same-column range -- keepers live
         in the keeper column so this only kicks in when the user
         shift-clicks within the keeper column itself. The rule's
         recommended keeper should never be auto-selected. */
      if (rows[i].getAttribute('data-keeper') === 'true') continue;
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
      /* Never auto-select the rule's recommended keeper -- doing so
         defeats the point of the rule recommendation. The user can still
         shift-click into the keeper column to manually include it. */
      if (row.getAttribute('data-keeper') === 'true') return;
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
      /* Priority order, top-down: arr-history-dialog -> confirm-modal ->
         cheatsheet -> activity-panel -> filter-drawer -> clear-selection
         -> blur-search. Each branch calls preventDefault ONLY when it
         consumes the key, so Escape on an empty page is a no-op. */
      const histDialog = document.getElementById('arr-history-dialog');
      if (histDialog && histDialog.dataset.active === 'true') {
        e.preventDefault();
        histDialog.dataset.active = 'false';
        return;
      }
      if (confirmOpen()) { e.preventDefault(); closeConfirmDialog(); return; }
      const cs = document.getElementById('kbd-cheatsheet');
      if (cs && cs.dataset.active === 'true') {
        e.preventDefault();
        toggleCheatsheet(false);
        return;
      }
      if (activityPanelOpen) {
        e.preventDefault();
        setActivityPanel(false);
        return;
      }
      const drawer = document.getElementById('filter-drawer');
      if (drawer && drawer.classList.contains('open')) {
        e.preventDefault();
        drawer.classList.remove('open');
        return;
      }
      if (selection.size > 0) {
        e.preventDefault();
        clearSelection();
        return;
      }
      const search = document.getElementById('search-input');
      if (search && document.activeElement === search) {
        e.preventDefault();
        search.blur();
      }
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
      /* Bytes were captured at check-time and never change for a given
         torrent; skip the re-read here. The `change` handler is the
         single source-of-truth for selection.set(h, bytes). */
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

    if (target.id === 'groups') {
      /* Invalidate the visible-row cache BEFORE repaint so the master
         select-all checkbox reflects the freshly-rendered row set. */
      invalidateVisibleCache();

      /* Rule-cleanup UX: only auto-add flagged rows to selection on the
         FIRST swap that follows a rule-chip click. Subsequent swaps
         (SSE RESYNC, per-card re-render) carry the same data-marked
         highlight but must NOT re-check rows the user explicitly
         unchecked -- the in-memory selection Map is authoritative.
         The /rules/{slug}/preview response emits a one-shot
         <meta id="qf-rule-activation"> OOB so we know this swap is
         the activation; we consume the flag here.

         For non-activation swaps we still re-apply selection state to
         freshly rendered rows so the checkboxes don't visibly flicker
         off during an RESYNC. */
      const activation = document.getElementById('qf-rule-activation');
      if (activation) {
        let added = 0;
        target.querySelectorAll('.torrent-row[data-marked="true"]').forEach((row) => {
          if (row.getAttribute('data-keeper') === 'true') return;
          const h = row.getAttribute('data-hash');
          if (h) { selection.set(h, rowBytes(row)); added++; }
          const cb = row.querySelector('.row-check');
          if (cb instanceof HTMLInputElement) cb.checked = true;
        });
        qfLog.debug('rule activation', { slug: activation.getAttribute('data-slug'), added, total: selection.size });
        activation.remove();
      } else {
        /* Re-apply existing selection state to freshly rendered rows. */
        target.querySelectorAll('.torrent-row').forEach((row) => {
          const h = row.getAttribute('data-hash');
          if (!h || !selection.has(h)) return;
          const cb = row.querySelector('.row-check');
          if (cb instanceof HTMLInputElement && !cb.checked) cb.checked = true;
        });
      }
      repaintSelectionFooter();

      if (focusedHash) {
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
       the flow (selection re-apply, viewport observer) still fires.

       Run synchronously inside the handler -- htmx's outerHTML OOB swap
       replaces #qf-batch-staging, so a rAF deferral that runs after the
       next batch arrives finds itself holding a reference to the detached
       previous staging div. Two staging divs can collide in one frame
       under cold-boot load and the rAF closure can never recover the
       children of the now-orphaned node. Moving N detached cards into
       #groups is microseconds; rAF buys nothing here. */
    if (target.id === 'qf-batch-staging') {
      applyBatchStaging(target);
      return;
    }

    /* Single-row or partial OOB swap: re-apply selection state from the
       in-memory Map so SSE row updates don't visibly unselect rows. */
    reapplySelectionTo(e.target);

    if (touchedGroups(target)) invalidateVisibleCache();
  });

  document.addEventListener('DOMContentLoaded', repaintSelectionFooter);

  /* =========================================================================
     Session-state persistence: replay filter + rule selection after the
     server forgets.

     Subscription state (active filter chips, active rule slug) lives in
     server-side process memory. Any uvicorn --reload (or crash) drops it.
     The user's open tab still shows chips as "active" until SSE delivers
     the freshly-empty chrome OOB swap -- at which point the chips redraw
     inactive and the user sees the unfiltered 622-group list and asks
     "why are filters off when the chips look on?".

     Fix: persist the user's selection to localStorage on every successful
     mutation. On every SSE open, compare the saved selection against the
     DOM's current pressed chips; if the saved set is non-empty but the
     DOM shows nothing pressed, the server forgot -- replay each POST so
     the server's subscription catches up. The next SSE RESYNC then
     re-renders #groups with the correct subset.
     ========================================================================= */
  const SESSION_KEY = 'qf_session_v1';

  function readActiveSelection() {
    /* Read the server-rendered canonical state from
       ``.active-filters-strip[data-qf-state]``. The strip is OOB-swapped
       on every filter mutation, so the JSON it carries always reflects
       what the server's Subscription thinks is active. Scraping
       individual chip nodes (multiple classes, hx-vals JSON attrs, etc.)
       was fragile and missed cases like ``min_torrents``. */
    const strip = document.querySelector('.active-filters-strip[data-qf-state]');
    let server = { filters: [], search: '', min_torrents: 1, arr_monitored: 'any', arr_cutoff: 'any' };
    if (strip) {
      try { server = JSON.parse(strip.getAttribute('data-qf-state')); } catch (e) {}
    }
    const rule = document.querySelector('.rule-chip[aria-pressed="true"]')?.getAttribute('data-slug') || '';
    return { ...server, rule };
  }

  function stateIsEmpty(s) {
    if (!s) return true;
    return (s.filters || []).length === 0
      && !(s.search)
      && (s.min_torrents == null || s.min_torrents <= 1)
      && (!s.arr_monitored || s.arr_monitored === 'any')
      && (!s.arr_cutoff || s.arr_cutoff === 'any')
      && !s.rule;
  }

  function saveSession() {
    try {
      const state = readActiveSelection();
      if (stateIsEmpty(state)) localStorage.removeItem(SESSION_KEY);
      else localStorage.setItem(SESSION_KEY, JSON.stringify(state));
      qfLog.debug('session saved', state);
    } catch (e) { qfLog.warn('session save failed', e); }
  }

  /* Persist after every successful filter / rule POST. ``htmx:afterSettle``
     fires when the response DOM has been swapped + animated, so reading
     the new pressed-chip state at this point gives us the canonical
     post-mutation snapshot. */
  document.body.addEventListener('htmx:afterSettle', (e) => {
    const url = e.detail && e.detail.requestConfig && e.detail.requestConfig.path;
    if (!url) return;
    if (url.startsWith('/filters') || url.startsWith('/rules/')) {
      saveSession();
    }
  });

  function htmxAjax(method, path, values) {
    /* Promise wrapper around htmx.ajax so we can await each step. The
       target+swap mirror what the corresponding hx-* attributes on the
       original buttons use: #groups innerHTML for both /filters and the
       rule-preview endpoint. htmx runs OOB swaps for active-filters,
       filter-facets, and rule-bar-slot automatically from the response,
       so the chrome chips redraw correctly. */
    return new Promise((resolve) => {
      if (typeof window.htmx === 'undefined') { resolve(); return; }
      try {
        window.htmx.ajax(method, path, {
          target: '#groups',
          swap: 'innerHTML',
          values: values || {},
        }).then(resolve, () => resolve());
      } catch (e) { resolve(); }
    });
  }

  async function replaySession(state) {
    qfLog.info('replaying session after server restart', state);
    /* Run through htmx.ajax so the server's HTML response is swapped
       into the page (chrome OOBs + target swaps fire). Plain fetch
       wouldn't update the DOM, which is what produced the "POST went
       through but cards still wrong" symptom during testing. */
    try {
      for (const f of (state.filters || [])) {
        await htmxAjax('POST', '/filters', { facet: f.facet, value: f.value });
      }
      if (state.min_torrents && state.min_torrents > 1) {
        await htmxAjax('POST', '/filters', { facet: 'min_torrents', value: String(state.min_torrents) });
      }
      if (state.arr_monitored && state.arr_monitored !== 'any') {
        await htmxAjax('POST', '/filters', { facet: 'arr_monitored', value: state.arr_monitored });
      }
      if (state.arr_cutoff && state.arr_cutoff !== 'any') {
        await htmxAjax('POST', '/filters', { facet: 'arr_cutoff', value: state.arr_cutoff });
      }
      if (state.search) {
        await htmxAjax('POST', '/filters/search', { search: state.search });
      }
      if (state.rule) {
        await htmxAjax('POST', '/rules/' + state.rule + '/preview', {});
      }
    } catch (e) {
      qfLog.warn('session replay failed', e);
    }
  }

  function maybeReplaySession() {
    let saved;
    try {
      const raw = localStorage.getItem(SESSION_KEY);
      if (!raw) return;
      saved = JSON.parse(raw);
    } catch (e) { return; }
    if (!saved || stateIsEmpty(saved)) return;
    /* If the server's view already matches what we saved, skip replay. */
    const current = readActiveSelection();
    if (stateIsEmpty(current) && !stateIsEmpty(saved)) {
      qfLog.info('server forgot session, replaying', saved);
      replaySession(saved);
    } else {
      qfLog.debug('session in sync, no replay needed', { current, saved });
    }
  }

  /* Wait for the first RESYNC's chrome OOB swap to settle before deciding
     whether to replay. The synthetic RESYNC fires inside ~100 ms of SSE
     open; 1500 ms is comfortably past that even on a slow cold-boot. */
  let sessionSyncTimer = null;
  document.body.addEventListener('htmx:sseOpen', () => {
    if (sessionSyncTimer) clearTimeout(sessionSyncTimer);
    sessionSyncTimer = setTimeout(maybeReplaySession, 1500);
  });
  /* Intentionally NOT saving on DOMContentLoaded: a "stale tab" reload
     after server restart paints the chrome with the EMPTY server state.
     If we saved here, we'd overwrite the user's previously-persisted
     filter set with empty, destroying the very state we want to replay.
     The afterSettle hook on /filters and /rules POSTs already captures
     every legitimate mutation, so on-load seeding adds nothing useful. */

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

  /* Selection hygiene: when SSE / HTMX removes a torrent row (or a whole
     group card containing rows), drop the corresponding entries from the
     in-memory selection Map. Without this, the footer keeps counting
     deleted torrents and the next /torrents/bulk/cleanup ships stale
     infohashes to qBit (which silently 404s).

     A MutationObserver is more reliable than relying on the post-swap
     event listeners: HTMX OOB removes can fire from many paths (per-row
     delete, group-card delete, RESYNC prune) and they don't all
     consistently target #groups. Watching the subtree catches every one
     in a single place at near-zero cost (microtask after mutation).

     This script is inlined BEFORE the streamed ``#groups`` markup, so we
     can't bind synchronously: at IIFE-eval time ``document.getElementById
     ('groups')`` is null. Retry on DOMContentLoaded and again on
     ``htmx:afterSwap`` so the observer attaches as soon as ``#groups``
     materialises -- whichever event happens first wins. */
  (function selectionRemovalObserver() {
    let bound = false;
    function bind() {
      if (bound) return;
      const groups = document.getElementById('groups');
      if (!groups) return;
      bound = true;
      const obs = new MutationObserver((records) => {
        /* Collect candidate hashes from removed nodes, then verify each
           one is actually gone from the document before deleting from
           selection. htmx's innerHTML swap removes the old subtree and
           inserts a new one in the SAME tick: the MO callback (microtask)
           sees BOTH the removals (full old tree) AND the additions, but
           checking ``document.getElementById('torrent-' + h)`` resolves
           against the current DOM, which by callback-run time already
           reflects the new tree. So a hash that survived the swap shows
           up in removedNodes but ALSO has a live element in the DOM --
           skipping those avoids racing the rule-chip auto-select handler
           that runs synchronously inside afterSwap before this microtask. */
        const candidates = new Set();
        for (const rec of records) {
          if (rec.type !== 'childList') continue;
          for (const node of rec.removedNodes) {
            if (!(node instanceof Element)) continue;
            if (node.classList && node.classList.contains('torrent-row')) {
              const h = node.getAttribute('data-hash');
              if (h) candidates.add(h);
            } else if (node.querySelectorAll) {
              node.querySelectorAll('.torrent-row[data-hash]').forEach((r) => {
                const h = r.getAttribute('data-hash');
                if (h) candidates.add(h);
              });
            }
          }
        }
        let removed = 0;
        let stillInDom = 0;
        for (const h of candidates) {
          if (!selection.has(h)) continue;
          if (document.getElementById('torrent-' + h)) { stillInDom++; continue; }
          selection.delete(h);
          removed++;
        }
        if (qfLog.enabled && (candidates.size || removed)) {
          qfLog.debug('MO prune', {
            candidates: candidates.size,
            stillInDom,
            removed,
            selectionAfter: selection.size,
          });
        }
        if (removed) {
          invalidateVisibleCache();
          repaintSelectionFooter();
        }
      });
      obs.observe(groups, { childList: true, subtree: true });
    }
    bind();
    document.addEventListener('DOMContentLoaded', bind);
    document.body.addEventListener('htmx:afterSwap', bind);
  })();

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

    /* IntersectionObserver.observe() on an already-observed node is a
       no-op, but the surrounding querySelectorAll('.group-card') still
       walks all 622 cards. Under a per-row TORRENT_CHANGED storm
       htmx:oobAfterSwap fires hundreds of times per second; the listener
       on that event was the dominant warm-state cost. New `.group-card`
       elements only arrive in two paths:
         1. applyBatchStaging() dispatches a synthetic `htmx:afterSwap`
            on `#groups` after relaying batched RESYNC cards.
         2. GROUP_ADDED uses `hx-swap-oob="afterbegin:#groups"`, which
            HTMX fires with `target === #groups`.
       Both hit `htmx:afterSwap` with target.id === 'groups', so listening
       only there covers every real card-insert case. Per-row OOB swaps
       don't add cards, so attach can skip them entirely. */
    const observed = new WeakSet();
    const attach = (e) => {
      if (e && e.detail && e.detail.target) {
        if (e.detail.target.id !== 'groups') return;
      }
      document.querySelectorAll('.group-card').forEach((el) => {
        if (observed.has(el)) return;
        observed.add(el);
        obs.observe(el);
      });
    };
    document.addEventListener('DOMContentLoaded', attach);
    document.body.addEventListener('htmx:afterSwap', attach);
  })();
})();
