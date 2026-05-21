"""Entry point: ``python -m qbit_filter`` or the installed ``qbit-filter`` script."""

from __future__ import annotations

import uvicorn

from qbit_filter.config import Settings


def main() -> None:
    settings = Settings()
    # ``dev_mode=true`` enables uvicorn's worker auto-restart on source change
    # plus the per-page livereload polling (see web/static/livereload.js).
    # uvicorn's default reload watcher only fires on ``*.py`` -- without
    # ``reload_includes`` covering templates and static files, editing
    # ``.html`` / ``.css`` / ``.js`` silently fails to swap the worker and
    # the browser stays on the old code. Explicit list keeps the watcher
    # responsive to anything we actually edit during development.
    reload_includes = (
        ["*.py", "*.html", "*.css", "*.js"] if settings.dev_mode else None
    )
    uvicorn.run(
        "qbit_filter.app:create_app",
        host=settings.listen_host,
        port=settings.listen_port,
        factory=True,
        log_level=settings.log_level.lower(),
        reload=settings.dev_mode,
        reload_dirs=["src/qbit_filter"] if settings.dev_mode else None,
        reload_includes=reload_includes,
    )


if __name__ == "__main__":
    main()
