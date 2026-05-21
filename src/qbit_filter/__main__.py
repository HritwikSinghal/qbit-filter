"""Entry point: ``python -m qbit_filter`` or the installed ``qbit-filter`` script."""

from __future__ import annotations

import uvicorn

from qbit_filter.config import Settings


def main() -> None:
    settings = Settings()
    # ``dev_mode=true`` enables uvicorn's worker auto-restart on source change
    # plus the per-page livereload polling (see web/static/livereload.js). The
    # template/static reload watches the package directory by default; that's
    # enough because both ``templates/`` and ``static/`` live under it.
    uvicorn.run(
        "qbit_filter.app:create_app",
        host=settings.listen_host,
        port=settings.listen_port,
        factory=True,
        log_level=settings.log_level.lower(),
        reload=settings.dev_mode,
        reload_dirs=["src/qbit_filter"] if settings.dev_mode else None,
    )


if __name__ == "__main__":
    main()
