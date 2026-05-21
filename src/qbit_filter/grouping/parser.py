"""guessit wrapper + title normalisation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

import guessit  # type: ignore[import-untyped]

from qbit_filter.grouping import parse_cache

_LEADING_ARTICLES = {"the", "a", "an"}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Fast pre-check for a torrent name carrying season info: ``S01``, ``S1``,
# ``season 4``, etc. We only fall through to ``parse(name)`` when this
# matches, since the parser hits guessit (~1ms) where this regex is ~1us.
_SEASON_HINT = re.compile(
    # The digits are bound on the right by a negative lookahead for another
    # digit -- ``S01E03`` must match ``01``, not extend into ``01E03`` and not
    # consume the ``E``. ``[^A-Za-z0-9]`` on the left guards against false
    # positives like ``... HEVCs02 ...``.
    r"(?:^|[^A-Za-z0-9])(?:s|season[ ._-]?)(\d{1,3})(?![0-9])",
    re.IGNORECASE,
)


def quick_season(name: str) -> int | None:
    """Return a season number if it's syntactically present in ``name``.

    Used by the UI to render season chips on TV group rows without paying the
    full ``parse()`` cost when ``S01`` is plainly visible. Falls back to
    ``None`` if the name doesn't include a clear ``Sxx`` / ``Season N`` token.
    """
    m = _SEASON_HINT.search(name)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 0 <= n <= 99 else None


@dataclass(frozen=True, slots=True)
class ParsedName:
    title: str
    year: int | None
    kind: Literal["movie", "episode", "other"]
    season: int | None = None
    episode: int | None = None


def normalise_title(raw: str) -> str:
    """Lowercase, strip diacritics, collapse non-alnum, drop a single leading article."""
    folded = unicodedata.normalize("NFKD", raw)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    cleaned = _NON_ALNUM.sub(" ", ascii_only.lower()).strip()
    parts = cleaned.split()
    if parts and parts[0] in _LEADING_ARTICLES:
        parts = parts[1:]
    return " ".join(parts)


# Process-local memoisation cache. Hydrated from disk on import via
# :func:`parse_cache.load` so warm boots skip guessit entirely for any
# name we've seen before. Workers in a ``ProcessPoolExecutor`` start with
# an empty cache; the parent reseeds them via the persisted snapshot.
_CACHE: dict[str, ParsedName] = {}


def _serialise(p: ParsedName) -> dict[str, Any]:
    return {
        "title": p.title,
        "year": p.year,
        "kind": p.kind,
        "season": p.season,
        "episode": p.episode,
    }


def _deserialise(raw: dict[str, Any]) -> ParsedName | None:
    kind_raw = raw.get("kind")
    if kind_raw not in ("movie", "episode", "other"):
        return None
    year = raw.get("year")
    season = raw.get("season")
    episode = raw.get("episode")
    return ParsedName(
        title=str(raw.get("title") or ""),
        year=int(year) if isinstance(year, int) else None,
        kind=kind_raw,
        season=int(season) if isinstance(season, int) else None,
        episode=int(episode) if isinstance(episode, int) else None,
    )


def _hydrate_from_disk() -> None:
    raw = parse_cache.load()
    for name, payload in raw.items():
        parsed = _deserialise(payload)
        if parsed is not None:
            _CACHE[name] = parsed


_hydrate_from_disk()


def dump_to_disk() -> None:
    """Persist the in-memory parse cache. Called on app shutdown so the
    next boot can skip guessit for already-seen names."""
    parse_cache.save({name: _serialise(p) for name, p in _CACHE.items()})


def parse_uncached(name: str) -> ParsedName:
    """Guessit pass with no caching. Public so a ``ProcessPoolExecutor``
    worker can call it without needing to import private names."""
    guess: dict[str, Any] = dict(guessit.guessit(name))
    guess_type = guess.get("type")
    title = str(guess.get("title") or name).strip()
    year_raw = guess.get("year")
    year_int = int(year_raw) if isinstance(year_raw, int) else None

    if guess_type == "movie":
        return ParsedName(title=title, year=year_int, kind="movie")
    if guess_type == "episode":
        season_raw = guess.get("season")
        episode_raw = guess.get("episode")
        return ParsedName(
            title=title,
            year=year_int,
            kind="episode",
            season=int(season_raw) if isinstance(season_raw, int) else None,
            episode=int(episode_raw) if isinstance(episode_raw, int) else None,
        )
    return ParsedName(title=title, year=year_int, kind="other")


def prime(name: str, parsed: ParsedName) -> None:
    """Populate the cache without invoking guessit. Used by the cold-boot
    warmer to fold in results computed in subprocess workers."""
    _CACHE[name] = parsed


def is_cached(name: str) -> bool:
    return name in _CACHE


def parse(name: str) -> ParsedName:
    """Best-effort parse of a torrent name into title/year/kind/season/episode.

    Memoised by the in-memory ``_CACHE`` (hydrated from disk on import,
    primed by :func:`prime` after a process-pool warm). On a miss the
    name goes through guessit (~1-3 ms) and the result is cached for the
    rest of the process lifetime.
    """
    cached = _CACHE.get(name)
    if cached is not None:
        return cached
    result = parse_uncached(name)
    _CACHE[name] = result
    return result
