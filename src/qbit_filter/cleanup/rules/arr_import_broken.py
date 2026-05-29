"""arr import-broken rule: arr grabbed the torrent but couldn't import it."""

from __future__ import annotations

from dataclasses import dataclass

from qbit_filter.cleanup.types import Candidate, ReasonFactor, Rule
from qbit_filter.state.store import Store

ORDER: int = 70


@dataclass(frozen=True, slots=True)
class ArrImportBrokenRule:
    """arr downloaded the torrent but couldn't import the resulting file.

    arr's queue exposes ``trackedDownloadStatus`` (``ok`` / ``warning`` /
    ``error``) and a ``statusMessages`` array that carries the actual
    diagnostic ("No files found are eligible for import", "Sample file too
    small", "Permission denied", etc). When either signal is non-ok the
    torrent is by definition dead weight: arr has explicitly given up on
    using it, but the file keeps sitting on disk seeding to nobody useful.

    Deterministic signal -- no thresholds, no subjective scoring. We only
    surface what arr already decided was broken.
    """

    slug: str = "arr-import-broken"
    label: str = "arr: import broken"
    description: str = (
        "Radarr/Sonarr reports an import problem (statusMessages or "
        "trackedDownloadStatus=warning/error). The file is on disk but arr "
        "can't use it -- safe-deletion candidate."
    )

    def candidates(self, store: Store, *, now: int | None = None) -> list[Candidate]:
        arr = store.arr
        if arr is None or not arr.hash_to_arr:
            return []
        out: list[Candidate] = []
        for key, group in store.groups.items():
            for h in group.torrent_hashes:
                t = store.torrents.get(h)
                if t is None:
                    continue
                match = arr.hash_to_arr.get(t.hash.lower())
                if match is None:
                    continue
                # Either an explicit status message OR a non-ok tracked
                # download status counts. Many arr versions populate one
                # but not the other depending on the failure mode.
                has_messages = bool(match.queue_status_messages)
                bad_status = match.queue_tracked_status.lower() in {
                    "warning", "error"
                }
                if not (has_messages or bad_status):
                    continue
                first_msg = (
                    match.queue_status_messages[0]
                    if match.queue_status_messages
                    else f"arr status: {match.queue_tracked_status or 'unknown'}"
                )
                # Reason chip is short; the full list lives in the row's
                # title attribute via the template.
                short = first_msg if len(first_msg) <= 80 else first_msg[:77] + "..."
                factors: tuple[ReasonFactor, ...] = (
                    ReasonFactor(
                        "arr",
                        f"{match.source} import broken",
                        "bad",
                    ),
                    ReasonFactor(
                        "status",
                        match.queue_tracked_status or "warning",
                        "warning",
                    ),
                )
                out.append(
                    Candidate(
                        torrent_hash=t.hash,
                        group_key=key,
                        reason=short,
                        factors=factors,
                        severity="warning",
                    )
                )
        return out


RULE: Rule = ArrImportBrokenRule()
