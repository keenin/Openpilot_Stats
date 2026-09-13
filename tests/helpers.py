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


def seed_parsed(cache, route_name="d|r", *, engaged=12.5, not_in_park=None, source="selfdriveState.enabled", **meta) -> None:
    cache.upsert_route_meta(drive_row(route_name=route_name, qlog_parsed=False, engaged_time_s=None, **meta))
    cache.save_engaged(route_name, engaged, source, not_in_park)
