"""Entry point: ``python -m qbit_filter`` or the installed ``qbit-filter`` script."""

from __future__ import annotations

import uvicorn

from qbit_filter.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "qbit_filter.app:create_app",
        host=settings.listen_host,
        port=settings.listen_port,
        factory=True,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
