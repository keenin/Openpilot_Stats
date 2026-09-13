"""Builders shared by cache, aggregate, and pipeline tests."""

from __future__ import annotations

from op_usage.cache import DriveRow


def drive_row(**kwargs) -> DriveRow:
    row = dict(
        route_name="d|r",
        dongle_id="deadbeefcafebabe",
        start_time_utc_ms=1_000,
        end_time_utc_ms=2_000,
        length_miles=10.0,
        total_drive_time_s=3600.0,
        git_commit="abc",
        git_branch="nightly",
        git_remote="",
        maxqlog=1,
        engaged_time_s=1800.0,
        engaged_source="selfdriveState.enabled",
        qlog_parsed=True,
    )
    row.update(kwargs)
    return DriveRow(**row)
