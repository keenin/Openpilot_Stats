"""Filters, commit grouping, and sort.

A drive is included only if length >= 1 mile AND engaged time > 0.
A group appears only if it has >= 3 qualifying drives.
Groups sort by last qualifying drive (newest first) — never by engage %.

Non-master branches: one row per SHA.
master: walk qualifying drives in start_time order and split when the
driving-weights fingerprint changes. A SHA that reappears after a
different fingerprint is a new interval (no first-seen SHA bucket).
Unknown fingerprints stay fail-closed (never merge two unknown SHAs;
consecutive drives of one unknown SHA stay one per-SHA interval).
`origin/master` and `refs/heads/master` count as master.
Engage % = engaged / not_in_park (wall-clock fallback).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from op_usage import MIN_DRIVES_PER_COMMIT, MIN_MILES
from op_usage.cache import DriveRow

WeightsLookup = Callable[[str, str], str | None]


@dataclass
class DriveView:
    route_name: str
    start_time_utc_ms: int
    length_miles: float
    engaged_time_s: float
    not_in_park_time_s: float
    git_branch: str
    git_commit: str
    weighted_engaged_time_s: float | None = None

    @property
    def engage_pct(self) -> float:
        return _engage_pct(self.engaged_time_s, self.not_in_park_time_s)

    @property
    def weight_pct(self) -> float | None:
        return _weight_pct(self.weighted_engaged_time_s, self.engaged_time_s)


@dataclass
class CommitRow:
    git_commit: str
    git_branch: str
    git_remote: str
    first_drive_ms: int
    last_drive_ms: int
    drive_count: int
    total_miles: float
    engaged_time_s: float
    not_in_park_time_s: float
    drives: list[DriveView] = field(default_factory=list)
    era_first_commit: str = ""
    weighted_engaged_time_s: float | None = None
    weighted_raw_engaged_s: float = 0.0

    @property
    def engage_pct(self) -> float:
        return _engage_pct(self.engaged_time_s, self.not_in_park_time_s)

    @property
    def weight_pct(self) -> float | None:
        return _weight_pct(self.weighted_engaged_time_s, self.weighted_raw_engaged_s)

    @property
    def short_hash(self) -> str:
        last = (self.git_commit or "")[:7] or "unknown"
        first = (self.era_first_commit or self.git_commit or "")[:7]
        if first and first != last:
            return f"{first}…{last}"
        return last


def _engage_pct(engaged: float, denom: float) -> float:
    if denom <= 0:
        return 0.0
    return 100.0 * engaged / denom


def _weight_pct(weighted: float | None, raw: float) -> float | None:
    if weighted is None or raw <= 0:
        return None
    return 100.0 * weighted / raw


def qualifies(drive: DriveRow, min_miles: float = MIN_MILES) -> bool:
    if not drive.git_commit or drive.length_miles < min_miles:
        return False
    return (drive.engaged_time_s or 0.0) > 0


def park_time_usable(drive: DriveRow) -> bool:
    """False when the gear integral is missing or unusable.

    A stored 0.0 with engaged time > 0 is treated as missing (too few
    gear samples, or an all-park integral that cannot explain engagement).
    """
    raw = drive.not_in_park_time_s
    if raw is None:
        return False
    if float(raw) == 0.0 and (drive.engaged_time_s or 0.0) > 0:
        return False
    return True


def denominator_s(drive: DriveRow) -> float:
    """Engage-% denominator: usable not-in-park seconds, else API wall-clock."""
    if park_time_usable(drive):
        return float(drive.not_in_park_time_s or 0.0)
    return float(drive.total_drive_time_s)


def is_master_branch(branch: str) -> bool:
    raw = (branch or "").strip().lower()
    return raw in {"master", "origin/master", "refs/heads/master"}


def to_drive_view(drive: DriveRow) -> DriveView:
    return DriveView(
        route_name=drive.route_name,
        start_time_utc_ms=drive.start_time_utc_ms,
        length_miles=drive.length_miles,
        engaged_time_s=float(drive.engaged_time_s or 0.0),
        not_in_park_time_s=denominator_s(drive),
        git_branch=drive.git_branch,
        git_commit=drive.git_commit,
        weighted_engaged_time_s=drive.weighted_engaged_time_s,
    )


def aggregate_commits(
    drives: list[DriveRow],
    min_miles: float = MIN_MILES,
    min_drives: int = MIN_DRIVES_PER_COMMIT,
    weights_lookup: WeightsLookup | None = None,
) -> list[CommitRow]:
    qualifying = [d for d in drives if qualifies(d, min_miles=min_miles)]
    groups = _sha_groups(d for d in qualifying if not is_master_branch(d.git_branch))
    groups.extend(_master_eras(
        [d for d in qualifying if is_master_branch(d.git_branch)],
        weights_lookup,
    ))
    rows = [_commit_row(group) for group in groups if len(group) >= min_drives]
    rows.sort(key=lambda r: r.last_drive_ms, reverse=True)
    return rows


def _sha_groups(drives) -> list[list[DriveRow]]:
    by_commit: dict[str, list[DriveRow]] = {}
    for drive in drives:
        by_commit.setdefault(drive.git_commit.lower(), []).append(drive)
    return list(by_commit.values())


def _master_eras(
    drives: list[DriveRow],
    weights_lookup: WeightsLookup | None,
) -> list[list[DriveRow]]:
    """Split master drives on the drive timeline when fingerprint changes.

    Bucket-by-SHA-then-sort-by-first-seen wraps rollback drives of an old
    SHA across a newer weights era, and splits a later same-weight run
    away from those rollback drives. Walking start_time order keeps each
    contiguous fingerprint interval separate. Unknowns never merge.
    """
    if not drives:
        return []
    ordered = sorted(drives, key=lambda d: (d.start_time_utc_ms, d.route_name))
    fps: dict[tuple[str, str], str | None] = {}
    eras: list[list[DriveRow]] = []
    prev_fp: str | None = None
    prev_sha = ""
    for drive in ordered:
        sha = drive.git_commit.lower()
        key = (sha, drive.git_remote)
        if key not in fps:
            fps[key] = (
                weights_lookup(drive.git_commit, drive.git_remote)
                if weights_lookup
                else None
            )
        fp = fps[key]
        # Known equal fingerprints merge across SHAs. Unknowns stay fail-closed
        # (never merge two unknown SHAs) but consecutive drives of one unknown
        # SHA still form one per-SHA interval.
        same_known = fp is not None and fp == prev_fp
        same_unknown_sha = fp is None and prev_fp is None and sha == prev_sha
        if eras and (same_known or same_unknown_sha):
            eras[-1].append(drive)
        else:
            eras.append([drive])
        prev_fp = fp
        prev_sha = sha
    return eras


def _commit_row(group: list[DriveRow]) -> CommitRow:
    group = sorted(group, key=lambda d: d.start_time_utc_ms)
    last = group[-1]
    seen: list[str] = []
    seen_l: set[str] = set()
    for drive in group:
        sha = drive.git_commit
        key = sha.lower()
        if sha and key not in seen_l:
            seen.append(sha)
            seen_l.add(key)
    first_sha = seen[0] if seen else last.git_commit
    last_sha = seen[-1] if seen else last.git_commit
    weighted_sum = 0.0
    weighted_raw = 0.0
    any_weighted = False
    for drive in group:
        w = drive.weighted_engaged_time_s
        if w is None:
            continue
        any_weighted = True
        weighted_sum += float(w)
        weighted_raw += float(drive.engaged_time_s or 0.0)
    return CommitRow(
        git_commit=last_sha or first_sha,
        git_branch=last.git_branch or "(unknown)",
        git_remote=last.git_remote or "",
        first_drive_ms=group[0].start_time_utc_ms,
        last_drive_ms=last.start_time_utc_ms,
        drive_count=len(group),
        total_miles=sum(d.length_miles for d in group),
        engaged_time_s=sum(float(d.engaged_time_s or 0.0) for d in group),
        not_in_park_time_s=sum(denominator_s(d) for d in group),
        drives=[to_drive_view(d) for d in group],
        era_first_commit=first_sha,
        weighted_engaged_time_s=weighted_sum if any_weighted else None,
        weighted_raw_engaged_s=weighted_raw,
    )
