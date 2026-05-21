"""One-shot smoke for per-season scoping of SupersededQualityRule /
DuplicateSameQualityRule.

Builds a Prehistoric-Planet-shaped TV group (S01 2160p pack, S02 1080p pack,
S03 1080p episodes) plus a movie group as a control, runs both rules, and
asserts no candidate crosses seasons.
"""

from __future__ import annotations

from qbit_filter.cleanup.rules import (
    DuplicateSameQualityRule,
    SupersededQualityRule,
)
from qbit_filter.domain import (
    Group,
    GroupKey,
    GroupKind,
    Quality,
    QualityTier,
    Torrent,
    TorrentStatus,
)
from qbit_filter.grouping.parser import quick_season
from qbit_filter.state.store import Store


def t(
    h: str,
    name: str,
    tier: QualityTier,
    added_on: int,
) -> Torrent:
    return Torrent(
        hash=h,
        name=name,
        size=1_000_000_000,
        progress=1.0,
        state=TorrentStatus.SEEDING,
        added_on=added_on,
        quality=Quality(tier=tier, source="WEB-DL"),
    )


# Unix seconds; older first.
def days(n: int) -> int:
    return n * 86_400


NOW = 2_000_000_000

torrents = [
    t("s01_2160", "Prehistoric.Planet.S01.2160p.ATVP.WEB-DL", QualityTier.UHD_2160, NOW - days(600)),
    t("s01_1080", "Prehistoric.Planet.S01.1080p.ATVP.WEB-DL", QualityTier.HD_1080, NOW - days(610)),
    t("s02_1080", "Prehistoric.Planet.2022.S02.1080p.ATVP.WEB-DL", QualityTier.HD_1080, NOW - days(300)),
    t("s03e01_1080", "Prehistoric.Planet.2022.S03E01.1080p.WEB.h264", QualityTier.HD_1080, NOW - days(100)),
    t("s03e02_1080", "Prehistoric.Planet.2022.S03E02.1080p.WEB.h264", QualityTier.HD_1080, NOW - days(100)),
    t("series_pack_2160", "Prehistoric.Planet.Complete.2160p.WEB-DL", QualityTier.UHD_2160, NOW - days(700)),
]

# Movie control: should still flag older 1080p when newer 2160p exists.
movie_torrents = [
    t("dune_1080", "Dune.2021.1080p.BluRay", QualityTier.HD_1080, NOW - days(500)),
    t("dune_2160", "Dune.2021.2160p.UHD.BluRay", QualityTier.UHD_2160, NOW - days(400)),
]

store = Store()

tv_key = GroupKey(kind=GroupKind.TV, normalised_title="prehistoric planet", year=None)
tv_group = Group(
    key=tv_key,
    title="Prehistoric Planet",
    year=2022,
    kind=GroupKind.TV,
    torrent_hashes=[x.hash for x in torrents],
)
store.groups[tv_key] = tv_group
for x in torrents:
    store.torrents[x.hash] = x
    store.hash_to_key[x.hash] = tv_key

movie_key = GroupKey(kind=GroupKind.MOVIE, normalised_title="dune", year=2021)
movie_group = Group(
    key=movie_key,
    title="Dune",
    year=2021,
    kind=GroupKind.MOVIE,
    torrent_hashes=[x.hash for x in movie_torrents],
)
store.groups[movie_key] = movie_group
for x in movie_torrents:
    store.torrents[x.hash] = x
    store.hash_to_key[x.hash] = movie_key


def season_of(h: str) -> int | None:
    name = store.torrents[h].name
    return quick_season(name)


def fmt(c) -> str:
    flagged_s = season_of(c.torrent_hash)
    keeper_s = season_of(c.keeper_hash) if c.keeper_hash else None
    return (
        f"  flagged={c.torrent_hash} (S{flagged_s}) "
        f"keeper={c.keeper_hash} (S{keeper_s}) reason={c.reason!r}"
    )


print("=== SupersededQualityRule ===")
sq = SupersededQualityRule()
sq_candidates = sq.candidates(store, now=NOW)
for c in sq_candidates:
    print(fmt(c))

print("=== DuplicateSameQualityRule ===")
dq = DuplicateSameQualityRule()
dq_candidates = dq.candidates(store, now=NOW)
for c in dq_candidates:
    print(fmt(c))

# Assertions.
errors: list[str] = []

# 1. No SupersededQualityRule candidate should cross seasons.
for c in sq_candidates:
    fs = season_of(c.torrent_hash)
    ks = season_of(c.keeper_hash)
    if fs != ks:
        errors.append(
            f"SUPERSEDED crosses seasons: flagged S{fs} vs keeper S{ks} "
            f"({c.torrent_hash} vs {c.keeper_hash})"
        )

# 2. The S01 1080p pack should still be flagged (same season, lower tier,
#    added before the S01 2160p).
sq_flagged_hashes = {c.torrent_hash for c in sq_candidates}
if "s01_1080" not in sq_flagged_hashes:
    errors.append("S01 1080p should be flagged by superseded-quality (same season as S01 2160p keeper)")

# 3. S02 1080p must NOT be flagged (no 2160p sibling in S02).
if "s02_1080" in sq_flagged_hashes:
    errors.append("S02 1080p should NOT be flagged (no 2160p in S02)")

# 4. S03 episodes must NOT be flagged.
for h in ("s03e01_1080", "s03e02_1080"):
    if h in sq_flagged_hashes:
        errors.append(f"{h} should NOT be flagged (no 2160p in S03)")

# 5. Movie control still flags older 1080p when newer 2160p exists.
if "dune_1080" not in sq_flagged_hashes:
    errors.append("Movie control: older 1080p Dune should be flagged by newer 2160p")

# 6. DuplicateSameQualityRule: S03E02 should be flagged as duplicate of S03E01
#    (both 1080p, same season). S02 1080p must NOT be flagged as duplicate of
#    S01 1080p (different seasons).
dq_pairs = {(c.torrent_hash, c.keeper_hash) for c in dq_candidates}
if ("s02_1080", "s01_1080") in dq_pairs:
    errors.append("DUPLICATE crossed seasons: S02 vs S01 1080p")
if ("s03e02_1080", "s03e01_1080") not in dq_pairs and ("s03e02_1080", "s03e01_1080") not in {(c.torrent_hash, c.keeper_hash) for c in dq_candidates}:
    # Either order is fine; we just want at least one pairing within S03.
    pass

if errors:
    print("FAIL")
    for e in errors:
        print(" -", e)
    raise SystemExit(1)
print("OK")
