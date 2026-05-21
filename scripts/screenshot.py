"""Capture screenshots of the running qbit-filter UI for visual review.

Usage:
    .venv/bin/python scripts/screenshot.py [--url URL] [--out DIR]

The script assumes a server is already serving the UI (e.g. ``docker
compose up -d``). It opens Firefox headless via Playwright, captures the
key states, and writes PNGs into ``screenshots/`` so they can be read back
by anyone reviewing the UI (including me).

States captured:
    01_landing_dark.png       initial page, dark mode (default)
    02_landing_light.png      light-mode variant
    03_group_open.png         first group expanded -- shows torrent rows
    04_kebab_open.png         row kebab menu open
    05_bulk_bar.png           three torrents selected, bulk bar visible
    06_filters_sidebar.png    sidebar with chips and search
    07_mobile.png             narrow viewport (640x900) -- drawer collapsed
    08_active_filter.png      one facet applied, active-filter strip visible

Firefox is the target browser per project preference. Some CSS features
(content-visibility, :has) have different perf profiles in Firefox vs
Chromium -- pinning Firefox here keeps the screenshots representative of
the user's actual browser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, Playwright, sync_playwright

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "screenshots"

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 640, "height": 900}


def _wait_for_groups(page: Page) -> None:
    """Wait until the streaming response has flushed at least one group card.

    The page uses StreamingResponse so DOMContentLoaded fires only after the
    last chunk lands. We poll the DOM instead.

    ``state="attached"`` returns as soon as the element exists in the DOM,
    skipping the visibility check (which can stall for ~30s on the first
    card while its htmx-added/qf-enter entrance animation completes).
    """
    page.wait_for_selector("#groups .group-card", state="attached", timeout=30_000)
    # Short settle so a representative slice of cards has arrived. The cap
    # used to be 1500ms but the stream flushes in batches of ~16KB so the
    # first wave lands quickly and additional waiting is wasted clock.
    page.wait_for_timeout(500)


def _set_theme(page: Page, mode: str) -> None:
    """Force light/dark by toggling the same body class keys.js manages.

    Previously this reloaded the page, which re-streamed all 622 group cards
    -- the single biggest cost in a screenshot run. The body class + the
    localStorage entry are everything keys.js applies; beercss/dynamic-colors
    pick up the swap on the next style recalc with no reload."""
    page.evaluate(
        f"""
        localStorage.setItem('qf_theme', '{mode}');
        document.body.classList.remove('dark', 'light');
        document.body.classList.add('{mode}');
        """
    )
    # Style recalc is sync but give the layout one frame to settle.
    page.wait_for_timeout(50)


def _safe(step: str, fn: object) -> None:
    """Run a screenshot step, log + continue on failure. Without this a single
    flaky selector aborts the whole capture run, leaving later states uncovered."""
    try:
        fn()  # type: ignore[operator]
        print(f"   wrote {step}")
    except Exception as exc:
        print(f"   FAIL  {step}: {type(exc).__name__}: {exc}")


def capture(playwright: Playwright, url: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    browser = playwright.firefox.launch(headless=True)
    context = browser.new_context(viewport=DESKTOP, device_scale_factor=1)
    # Tighter default timeout so a hung step fails fast (was 30s).
    context.set_default_timeout(8_000)
    page = context.new_page()

    print(f"-> {url}")
    page.goto(url, wait_until="domcontentloaded")
    _wait_for_groups(page)

    _safe("01_landing_dark.png", lambda: page.screenshot(
        path=str(out_dir / "01_landing_dark.png"), full_page=False))

    _set_theme(page, "light")
    _safe("02_landing_light.png", lambda: page.screenshot(
        path=str(out_dir / "02_landing_light.png"), full_page=False))

    _set_theme(page, "dark")

    # 03: open the first group so subsequent row-targeted shots have a row
    # to aim at. ``force=True`` skips actionability checks which can hang
    # when an offscreen card hasn't been laid out yet.
    def _open_first_group() -> None:
        page.locator(".group-card .group-summary").first.click(force=True)
        page.wait_for_selector(".group-card details[open] .torrent-row", timeout=5_000)
        page.wait_for_timeout(120)
        page.screenshot(path=str(out_dir / "03_group_open.png"), full_page=False)
    _safe("03_group_open.png", _open_first_group)

    # 04: kebab menu on the first row inside the now-open group.
    # SSE updates the row continuously, so scroll_into_view_if_needed's
    # implicit "element stable" check times out. ``click(force=True)`` does
    # its own scroll-then-click without the stability requirement, so we
    # rely on that and skip the explicit pre-scroll.
    def _open_kebab() -> None:
        kebab = page.locator(".group-card details[open] .torrent-row .kebab").first
        kebab.click(force=True, timeout=5_000)
        page.wait_for_selector("#kebab-menu.open", timeout=3_000)
        page.wait_for_timeout(80)
        page.screenshot(path=str(out_dir / "04_kebab_open.png"), full_page=False)
        page.keyboard.press("Escape")
    _safe("04_kebab_open.png", _open_kebab)

    # 05: bulk bar with three rows selected
    def _bulk_bar() -> None:
        checkboxes = page.locator(".group-card details[open] .torrent-row .torrent-select")
        n = min(3, checkboxes.count())
        if n == 0:
            raise RuntimeError("no torrent checkboxes inside open group")
        for i in range(n):
            checkboxes.nth(i).check(force=True)
        page.wait_for_selector("#bulk-bar.visible", timeout=3_000)
        page.wait_for_timeout(80)
        page.screenshot(path=str(out_dir / "05_bulk_bar.png"), full_page=False)
        page.keyboard.press("Escape")
    _safe("05_bulk_bar.png", _bulk_bar)

    # 06: sidebar / filter chips (clipped to the sidebar's box)
    def _sidebar() -> None:
        sidebar = page.locator("nav.filter-sidebar").first
        sidebar.screenshot(path=str(out_dir / "06_filters_sidebar.png"))
    _safe("06_filters_sidebar.png", _sidebar)

    # 08: apply the first facet chip and capture the active strip
    def _active_filter() -> None:
        first_chip = page.locator(".facet-chips .facet-chip").first
        first_chip.click(force=True)
        # The active-filter strip lands via an HTMX OOB swap; the chip's
        # POST takes a beat. 350ms is enough in practice; raise if flaky.
        page.wait_for_timeout(350)
        page.screenshot(path=str(out_dir / "08_active_filter.png"), full_page=False)
        clear = page.locator(".clear-link, .sidebar-header .clear-all").first
        if clear.count():
            clear.click(force=True)
            page.wait_for_timeout(150)
    _safe("08_active_filter.png", _active_filter)

    # 07: mobile / drawer view
    def _mobile() -> None:
        page.set_viewport_size(MOBILE)
        page.wait_for_timeout(120)
        page.screenshot(path=str(out_dir / "07_mobile.png"), full_page=False)
    _safe("07_mobile.png", _mobile)

    context.close()
    browser.close()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"running server URL (default {DEFAULT_URL})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"output directory (default {DEFAULT_OUT})")
    args = parser.parse_args(argv)

    with sync_playwright() as p:
        capture(p, args.url, args.out)

    print(f"done -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
