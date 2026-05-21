"""Shared data types -- every layer trades in these."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Literal


class GroupKind(StrEnum):
    MOVIE = "movie"
    TV = "tv"
    OTHER = "other"


class TorrentStatus(StrEnum):
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    STALLED = "stalled"
    ERRORED = "errored"
    QUEUED = "queued"
    CHECKING = "checking"
    COMPLETED = "completed"


class QualityTier(StrEnum):
    """Comparable resolution tier. Higher value = better; use ordering on the
    enum's index, not on the string."""

    SD = "SD"  # < 720p
    HD_720 = "720p"
    HD_1080 = "1080p"
    UHD_2160 = "2160p"
    UNKNOWN = "unknown"


_TIER_ORDER = (
    QualityTier.UNKNOWN,
    QualityTier.SD,
    QualityTier.HD_720,
    QualityTier.HD_1080,
    QualityTier.UHD_2160,
)


def tier_rank(t: QualityTier) -> int:
    """Index into the canonical tier ordering. Higher = better quality."""
    return _TIER_ORDER.index(t)


@dataclass(frozen=True, slots=True)
class Quality:
    """Parsed release quality. All fields are best-effort; missing data is
    represented by ``UNKNOWN`` / ``None`` rather than raising."""

    tier: QualityTier = QualityTier.UNKNOWN
    source: str = ""  # "BluRay" | "WEB-DL" | "WEBRip" | "HDTV" | "DVDRip" | "Remux" | ""
    codec: str = ""  # "x264" | "x265" | "AV1" | "XviD" | ""
    hdr: bool = False  # HDR / HDR10 / DV present in name


@dataclass(frozen=True, slots=True)
class GroupKey:
    """Identity of a group of torrents.

    `source="tag"` is reserved for future TMDB enrichment (tmdb:<id> / imdb:tt<id>).
    """

    kind: GroupKind
    normalised_title: str
    year: int | None = None
    source: str = "guessit"  # or "category" / "tag"
    tag_id: str | None = None

    def slug(self) -> str:
        parts = [
            self.kind.value,
            *self.normalised_title.split(),
            str(self.year) if self.year else "",
        ]
        return "-".join(p for p in parts if p).lower()


@dataclass(slots=True)
class Torrent:
    hash: str
    name: str
    size: int
    progress: float  # 0.0 - 1.0
    state: TorrentStatus
    category: str = ""
    tags: tuple[str, ...] = ()
    trackers: tuple[str, ...] = ()
    dlspeed: int = 0  # bytes/sec
    upspeed: int = 0
    eta: int = -1  # seconds; -1 = N/A
    added_on: int = 0  # unix seconds
    last_activity: int = 0  # unix seconds; qBit's per-torrent last_activity
    ratio: float = 0.0  # share ratio (uploaded / downloaded)
    raw_state: str = ""  # qBit's raw state string for debugging
    quality: Quality = field(default_factory=Quality)

    @property
    def is_no_peers(self) -> bool:
        """True when qBit reports the torrent as having no peer activity --
        any of the stalled/paused/stopped variants. The reconciler folds
        ``stalledUP`` into ``SEEDING`` so the UI keeps showing it as seeding,
        but cleanup rules need to see it as cleanup-eligible. Read raw_state
        directly here rather than introducing a new enum value, since this
        is the only semantic that needs the distinction."""
        return self.raw_state in (
            "stalledUP", "stalledDL",
            "pausedUP", "pausedDL",
            "stoppedUP", "stoppedDL",
        )


@dataclass(slots=True)
class Group:
    key: GroupKey
    title: str  # display title (cased)
    year: int | None
    kind: GroupKind
    torrent_hashes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FilterState:
    statuses: frozenset[TorrentStatus] = frozenset()
    categories: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    trackers: frozenset[str] = frozenset()
    # Negative sets: a torrent matching any value here is excluded. A value
    # cannot be in both the positive and negative set simultaneously --
    # ``filter_parse.toggle`` enforces mutual exclusion.
    not_statuses: frozenset[TorrentStatus] = frozenset()
    not_categories: frozenset[str] = frozenset()
    not_tags: frozenset[str] = frozenset()
    not_trackers: frozenset[str] = frozenset()
    search: str = ""
    # Minimum torrents required for a group to be visible. 1 means "no filter";
    # values >=2 restrict the view to groups with multiple torrents (e.g. shows
    # with multiple seasons, or duplicates worth pruning).
    min_torrents: int = 1
    # *arr-derived filters. "any" = no filter, "monitored"/"unmonitored" check
    # the matched arr entity's monitored flag, "orphan" = no arr knows about
    # this torrent. Tri-state for monitored; ``arr_cutoff`` is met/unmet/any.
    arr_monitored: Literal["any", "monitored", "unmonitored", "orphan"] = "any"
    arr_cutoff: Literal["any", "met", "unmet"] = "any"
    # Exclusion set of arr-defined tag labels. A torrent whose matched arr
    # entity carries any of these labels is filtered out. The label set is
    # populated by clicking sidebar chips that mirror arr's own tag list,
    # turning arr's retention labels (e.g. "permanent", "archive") into a
    # one-shot exclusion filter without duplicating the policy here.
    not_arr_tags: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return (
            not self.statuses
            and not self.categories
            and not self.tags
            and not self.trackers
            and not self.not_statuses
            and not self.not_categories
            and not self.not_tags
            and not self.not_trackers
            and not self.search
            and self.min_torrents <= 1
            and self.arr_monitored == "any"
            and self.arr_cutoff == "any"
            and not self.not_arr_tags
        )


@dataclass(slots=True)
class MainDataDelta:
    full_update: bool
    rid: int
    added: dict[str, dict[str, object]] = field(default_factory=dict)
    changed: dict[str, dict[str, object]] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    categories_added: set[str] = field(default_factory=set)
    categories_removed: set[str] = field(default_factory=set)
    tags_added: set[str] = field(default_factory=set)
    tags_removed: set[str] = field(default_factory=set)
    trackers_added: set[str] = field(default_factory=set)
    trackers_removed: set[str] = field(default_factory=set)


class EventKind(Enum):
    TORRENT_ADDED = "torrent_added"
    TORRENT_CHANGED = "torrent_changed"
    TORRENT_REMOVED = "torrent_removed"
    GROUP_ADDED = "group_added"
    GROUP_REMOVED = "group_removed"
    RESYNC = "resync"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    kind: EventKind
    group_key: GroupKey | None = None
    torrent_hash: str | None = None
