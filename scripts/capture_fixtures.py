"""One-time script: capture sync/maindata fixtures for tests.

Run: ``python scripts/capture_fixtures.py``
Writes anonymised JSON to ``tests/fixtures/local_maindata_{full,delta}.json``.

The anonymiser strips filesystem paths (save_path, content_path,
download_path) but keeps tracker URLs, magnet URIs, comments, and
torrent names so that grouping / guessit tests have real-shaped input.
**These payloads contain private-tracker passkeys** -- the default
filenames carry a ``local_`` prefix so they are caught by
``.gitignore`` and cannot be committed by accident. Review before
publishing anywhere; for a public-safe fixture, craft a synthetic one
under a different filename.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from qbit_filter.config import Settings
from qbit_filter.qbit.client import connect

PATHS_TO_STRIP = ("save_path", "content_path", "download_path")


def anonymise(data: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied payload with filesystem paths removed."""
    out: dict[str, Any] = json.loads(json.dumps(data, default=str))
    for _hash, torrent in out.get("torrents", {}).items():
        for key in PATHS_TO_STRIP:
            torrent.pop(key, None)
    return out


def main() -> None:
    settings = Settings()
    client = connect(settings)

    fixtures_dir = Path("tests/fixtures")
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    full_payload: dict[str, Any] = dict(client.sync_maindata(rid=0))
    (fixtures_dir / "local_maindata_full.json").write_text(
        json.dumps(anonymise(full_payload), indent=2, sort_keys=True, default=str)
    )

    time.sleep(2)
    next_rid = int(full_payload.get("rid", 0))
    delta_payload: dict[str, Any] = dict(client.sync_maindata(rid=next_rid))
    (fixtures_dir / "local_maindata_delta.json").write_text(
        json.dumps(anonymise(delta_payload), indent=2, sort_keys=True, default=str)
    )

    full_torrents = full_payload.get("torrents", {})
    delta_torrents = delta_payload.get("torrents", {})
    print(
        f"Wrote {len(full_torrents)} torrents (full), {len(delta_torrents)} torrents (delta)"
    )


if __name__ == "__main__":
    main()
