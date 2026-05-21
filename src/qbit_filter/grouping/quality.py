"""Parse release quality (resolution / source / codec / HDR) from a torrent name.

Lives next to ``parser.py`` because it operates on the same input string. Kept
separate so guessit isn't in the hot path -- this is regex-only, ~1us per call,
suitable for running on every torrent on every reconciler tick.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

from qbit_filter.domain import Quality, QualityTier

logger = logging.getLogger(__name__)

_RES_2160 = re.compile(r"(?<![A-Za-z0-9])(?:2160p|4k|uhd)(?![A-Za-z0-9])", re.IGNORECASE)
_RES_1080 = re.compile(r"(?<![A-Za-z0-9])1080p(?![A-Za-z0-9])", re.IGNORECASE)
_RES_720 = re.compile(r"(?<![A-Za-z0-9])720p(?![A-Za-z0-9])", re.IGNORECASE)
_RES_SD = re.compile(
    r"(?<![A-Za-z0-9])(?:480p|576p|dvdrip|sdtv)(?![A-Za-z0-9])", re.IGNORECASE
)

# Source patterns ordered by specificity -- "BluRay REMUX" must match before "BluRay".
_SOURCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z])remux(?![A-Za-z])", re.IGNORECASE), "Remux"),
    (
        re.compile(r"(?<![A-Za-z])(?:bluray|bdrip|brrip)(?![A-Za-z])", re.IGNORECASE),
        "BluRay",
    ),
    (re.compile(r"(?<![A-Za-z])web[. _-]?dl(?![A-Za-z])", re.IGNORECASE), "WEB-DL"),
    (re.compile(r"(?<![A-Za-z])webrip(?![A-Za-z])", re.IGNORECASE), "WEBRip"),
    (re.compile(r"(?<![A-Za-z])hdtv(?![A-Za-z])", re.IGNORECASE), "HDTV"),
    (re.compile(r"(?<![A-Za-z])dvdrip(?![A-Za-z])", re.IGNORECASE), "DVDRip"),
)

_CODEC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:x265|h[. _-]?265|hevc)(?![A-Za-z0-9])", re.IGNORECASE
        ),
        "x265",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9])(?:x264|h[. _-]?264|avc)(?![A-Za-z0-9])", re.IGNORECASE
        ),
        "x264",
    ),
    (re.compile(r"(?<![A-Za-z0-9])av1(?![A-Za-z0-9])", re.IGNORECASE), "AV1"),
    (re.compile(r"(?<![A-Za-z0-9])xvid(?![A-Za-z0-9])", re.IGNORECASE), "XviD"),
)

_HDR_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:hdr10\+?|hdr|dolby[. _-]?vision|dv|hlg)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _detect_tier(name: str) -> QualityTier:
    if _RES_2160.search(name):
        return QualityTier.UHD_2160
    if _RES_1080.search(name):
        return QualityTier.HD_1080
    if _RES_720.search(name):
        return QualityTier.HD_720
    if _RES_SD.search(name):
        return QualityTier.SD
    return QualityTier.UNKNOWN


def _detect_source(name: str) -> str:
    for pattern, label in _SOURCE_PATTERNS:
        if pattern.search(name):
            return label
    return ""


def _detect_codec(name: str) -> str:
    for pattern, label in _CODEC_PATTERNS:
        if pattern.search(name):
            return label
    return ""


@lru_cache(maxsize=8192)
def parse_quality(name: str) -> Quality:
    """Best-effort quality extraction. Cached for the lifetime of a name --
    names are immutable in qBit so the cache is safe."""
    return Quality(
        tier=_detect_tier(name),
        source=_detect_source(name),
        codec=_detect_codec(name),
        hdr=bool(_HDR_PATTERN.search(name)),
    )
