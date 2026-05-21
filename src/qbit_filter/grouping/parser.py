"""guessit wrapper + title normalisation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

import guessit  # type: ignore[import-untyped]

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


@lru_cache(maxsize=8192)
def parse(name: str) -> ParsedName:
    """Best-effort parse of a torrent name into title/year/kind/season/episode.

    ``guessit`` is the per-tick CPU hot path (~1-3ms per name x 1310 names on
    a full rebuild). Names rarely change, and ``ParsedName`` is frozen, so the
    function is safe to memoise. The cache is sized for libraries up to ~8k
    torrents; beyond that the LRU eviction stays bounded.
    """
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
