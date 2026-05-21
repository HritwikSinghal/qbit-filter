"""Pure grouping logic. No I/O."""

from __future__ import annotations

from qbit_filter.domain import GroupKey, GroupKind, Torrent
from qbit_filter.grouping.parser import ParsedName, normalise_title

_TAG_PREFIXES = ("tmdb:", "imdb:tt", "imdb:")


def _explicit_tag(tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        low = tag.lower()
        if any(low.startswith(p) for p in _TAG_PREFIXES):
            return low
    return None


def assign(
    torrent: Torrent,
    parsed: ParsedName,
    *,
    movie_categories: frozenset[str],
    tv_categories: frozenset[str],
) -> GroupKey:
    """Map a torrent + parsed name to a stable GroupKey.

    Precedence (high to low):
      1. explicit tag (``tmdb:<id>`` / ``imdb:tt<id>``)
      2. qBit category override
      3. guessit verdict
      4. fall back to ``GroupKind.OTHER`` keyed by normalised raw title
    """
    tag = _explicit_tag(torrent.tags)
    if tag:
        return GroupKey(
            kind=GroupKind.MOVIE if "movie" in tag else GroupKind.OTHER,
            normalised_title=tag,
            year=None,
            source="tag",
            tag_id=tag,
        )

    category = torrent.category.lower() if torrent.category else ""
    forced_kind: GroupKind | None = None
    if category in tv_categories:
        forced_kind = GroupKind.TV
    elif category in movie_categories:
        forced_kind = GroupKind.MOVIE

    title_norm = normalise_title(parsed.title or torrent.name)

    if forced_kind == GroupKind.TV:
        return GroupKey(
            kind=GroupKind.TV,
            normalised_title=title_norm,
            year=None,
            source="category",
        )
    if forced_kind == GroupKind.MOVIE:
        return GroupKey(
            kind=GroupKind.MOVIE,
            normalised_title=title_norm,
            year=parsed.year,
            source="category",
        )

    if parsed.kind == "episode":
        return GroupKey(kind=GroupKind.TV, normalised_title=title_norm, year=None)
    if parsed.kind == "movie":
        return GroupKey(kind=GroupKind.MOVIE, normalised_title=title_norm, year=parsed.year)

    return GroupKey(kind=GroupKind.OTHER, normalised_title=title_norm, year=None)
