from __future__ import annotations

from pathlib import Path

from op_usage.cache import DriveRow
from op_usage.qlog import encode_synthetic_qlog
from op_usage.qlog_store import segment_path, split_route_name, write_qlog_atomic


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


def seed_local_qlogs(
    qlog_dir: Path,
    route_name: str,
    blob: bytes,
    *,
    maxqlog: int = 0,
    dongle_id: str = "",
) -> None:
    """Write `{0..maxqlog}.qlog`. Segment 0 gets `blob`; the rest are empty valid qlogs."""
    empty = encode_synthetic_qlog([], compress="bz2")
    dongle, route_id = split_route_name(route_name, dongle_id)
    for seg in range(int(maxqlog) + 1):
        write_qlog_atomic(
            segment_path(qlog_dir, dongle, route_id, seg),
            blob if seg == 0 else empty,
        )
