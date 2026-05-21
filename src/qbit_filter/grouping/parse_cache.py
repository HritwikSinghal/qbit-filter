"""Persistent on-disk cache for guessit parse results.

guessit costs ~1-3 ms per torrent name; on a 1300-torrent library that
adds ~3-5 s of blocking work on every cold boot. The names rarely change
once a torrent is added, so persisting the parse output across restarts
turns the steady-state warm boot into a single ``json.loads`` (~10 ms)
plus a dict lookup per name.

The cache file lives under ``$XDG_CACHE_HOME`` (or ``~/.cache``) so it
survives ``--reload`` restarts and process replacements without leaking
into the source tree. Writes are atomic via tmp + rename.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

logger = logging.getLogger(__name__)

# Soft cap so a long-running instance that has ingested a lot of unique
# names over time doesn't grow the cache indefinitely. Eviction is
# insertion-order (older entries dropped first) which is a good-enough
# approximation of LRU for boot-priming purposes -- the very recent
# additions are the ones most likely to be referenced on next boot.
_MAX_ENTRIES = 16384


def _cache_dir() -> pathlib.Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return pathlib.Path(base) / "qbit-filter"
    return pathlib.Path.home() / ".cache" / "qbit-filter"


def _path() -> pathlib.Path:
    return _cache_dir() / "parse_cache.json"


def load() -> dict[str, dict[str, Any]]:
    """Return the on-disk cache contents as ``{name: serialised ParsedName}``.

    Returns an empty dict on any IO / decode failure so a corrupt file
    degrades to "behaves like a cold boot" instead of crashing the app.
    """
    path = _path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("parse_cache: load failed (%s); ignoring file", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, payload in raw.items():
        if isinstance(name, str) and isinstance(payload, dict):
            out[name] = payload
    return out


def save(entries: dict[str, dict[str, Any]]) -> None:
    """Atomically write ``entries`` to disk. Best-effort: IO failure logs
    and returns -- a missed save just means next boot pays full guessit
    cost again, which is graceful degradation."""
    if len(entries) > _MAX_ENTRIES:
        # Drop oldest insertion-order entries. dict preserves insertion
        # order in CPython 3.7+, so this is well-defined.
        keys = list(entries.keys())[-_MAX_ENTRIES:]
        entries = {k: entries[k] for k in keys}
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(entries, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning("parse_cache: save failed (%s); cache not persisted", exc)
