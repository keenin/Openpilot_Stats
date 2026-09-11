"""Filters, commit grouping, and sort.

A drive is included only if length >= 1 mile AND engaged time > 0.
A commit appears only if it has >= 3 qualifying drives.
Commits sort by last qualifying drive (newest first) — never by engage %.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from op_usage import MIN_DRIVES_PER_COMMIT, MIN_MILES
from op_usage.cache import DriveRow


@dataclass
class DriveView:
    route_name: str
    start_time_utc_ms: int
    length_miles: float
    engaged_time_s: float
    total_drive_time_s: float
    git_branch: str
    git_commit: str

    @property
    def engage_pct(self) -> float:
        if self.total_drive_time_s <= 0:
            return 0.0
        return 100.0 * self.engaged_time_s / self.total_drive_time_s


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
    total_drive_time_s: float
    drives: list[DriveView] = field(default_factory=list)

    @property
    def engage_pct(self) -> float:
        if self.total_drive_time_s <= 0:
            return 0.0
        return 100.0 * self.engaged_time_s / self.total_drive_time_s

    @property
    def short_hash(self) -> str:
        return self.git_commit[:7] if self.git_commit else "unknown"


def qualifies(drive: DriveRow, min_miles: float = MIN_MILES) -> bool:
    if not drive.git_commit:
        return False
    if drive.length_miles < min_miles:
        return False
    engaged = drive.engaged_time_s or 0.0
    return engaged > 0


def to_drive_view(drive: DriveRow) -> DriveView:
    return DriveView(
        route_name=drive.route_name,
        start_time_utc_ms=drive.start_time_utc_ms,
        length_miles=drive.length_miles,
        engaged_time_s=float(drive.engaged_time_s or 0.0),
        total_drive_time_s=drive.total_drive_time_s,
        git_branch=drive.git_branch,
        git_commit=drive.git_commit,
    )


def aggregate_commits(
    drives: list[DriveRow],
    min_miles: float = MIN_MILES,
    min_drives: int = MIN_DRIVES_PER_COMMIT,
) -> list[CommitRow]:
    qualifying = [d for d in drives if qualifies(d, min_miles=min_miles)]
    by_commit: dict[str, list[DriveRow]] = {}
    for drive in qualifying:
        by_commit.setdefault(drive.git_commit.lower(), []).append(drive)

    rows: list[CommitRow] = []
    for commit, group in by_commit.items():
        if len(group) < min_drives:
            continue
        group.sort(key=lambda d: d.start_time_utc_ms)
        last = group[-1]
        rows.append(
            CommitRow(
                git_commit=group[0].git_commit or commit,
                git_branch=last.git_branch or "(unknown)",
                git_remote=last.git_remote or "",
                first_drive_ms=group[0].start_time_utc_ms,
                last_drive_ms=last.start_time_utc_ms,
                drive_count=len(group),
                total_miles=sum(d.length_miles for d in group),
                engaged_time_s=sum(float(d.engaged_time_s or 0.0) for d in group),
                total_drive_time_s=sum(d.total_drive_time_s for d in group),
                drives=[to_drive_view(d) for d in group],
            )
        )

    rows.sort(key=lambda r: r.last_drive_ms, reverse=True)
    return rows
