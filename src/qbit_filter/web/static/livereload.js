// Dev-only browser livereload. Polls /dev/version; when the boot id changes
// (uvicorn --reload restarted the worker), it triggers a full page reload.
// In prod the /dev/version route 404s and this script becomes a no-op.
//
// Intervals are intentionally long and visibility-aware: with 1310 torrents
// the dev server is already busy enough; a 500ms heartbeat from every open
// tab compounds into noticeable CPU and a sluggish browser.
(() => {
  const IDLE_DELAY_MS = 5000;
  const MAX_BACKOFF_MS = 30000;
  let bootId = null;
  let failures = 0;
  let timer = null;
  let stopped = false;

  async function tick() {
    timer = null;
    if (stopped) return;
    if (document.hidden) {
      // Pause while the tab is backgrounded; resume via visibilitychange.
      return;
    }
    try {
      const r = await fetch("/dev/version", { cache: "no-store" });
      if (r.status === 404) {
        // Prod -- shut the loop down permanently.
        stopped = true;
        return;
      }
      if (!r.ok) {
        failures += 1;
      } else {
        const id = (await r.text()).trim();
        if (bootId === null) {
          bootId = id;
        } else if (id !== bootId) {
          location.reload();
          return; // page is going away
        }
        failures = 0;
      }
    } catch {
      // Server is restarting; back off so we don't spam during downtime.
      failures += 1;
    }
    scheduleNext();
  }

  function scheduleNext() {
    if (stopped || timer !== null) return;
    const delay =
      failures > 0
        ? Math.min(MAX_BACKOFF_MS, IDLE_DELAY_MS * 2 ** Math.min(failures, 6))
        : IDLE_DELAY_MS;
    timer = setTimeout(tick, delay);
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !stopped && timer === null) {
      tick();
    }
  });

  tick();
})();
