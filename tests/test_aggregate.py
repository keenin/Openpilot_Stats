from __future__ import annotations

from helpers import drive_row
from op_usage.aggregate import aggregate_commits, denominator_s, qualifies


def test_excludes_short_and_zero_engaged_and_missing_commit() -> None:
    assert not qualifies(drive_row(length_miles=0.9, engaged_time_s=100))
    assert not qualifies(drive_row(length_miles=5, engaged_time_s=0))
    assert not qualifies(drive_row(git_commit="", engaged_time_s=100))
    assert qualifies(drive_row(length_miles=1.0, engaged_time_s=0.1))


def test_commit_requires_three_qualifying_drives() -> None:
    a = [drive_row(route_name=f"a{i}", git_commit="aaa", start_time_utc_ms=1000 + i) for i in range(2)]
    b = [drive_row(route_name=f"b{i}", git_commit="bbb", start_time_utc_ms=2000 + i) for i in range(3)]
    rows = aggregate_commits(a + b)
    assert [r.short_hash for r in rows] == ["bbb"]
    assert rows[0].drive_count == 3


def test_sort_by_last_qualifying_drive_not_engage_pct() -> None:
    low = [
        drive_row(
            route_name=f"low{i}",
            git_commit="ccccccc111111111111111111111111111111111",
            start_time_utc_ms=9_000 + i,
            engaged_time_s=100,
            total_drive_time_s=3600,
        )
        for i in range(3)
    ]
    high_old = [
        drive_row(
            route_name=f"hi{i}",
            git_commit="ddddddd222222222222222222222222222222222",
            start_time_utc_ms=100 + i,
            engaged_time_s=3500,
            total_drive_time_s=3600,
        )
        for i in range(3)
    ]
    rows = aggregate_commits(low + high_old)
    assert rows[0].git_commit.startswith("ccccccc")
    assert rows[0].engage_pct < rows[1].engage_pct


def test_date_range_totals_and_branch_from_last_drive() -> None:
    group = [
        drive_row(route_name="1", git_commit="eeee", start_time_utc_ms=1000, length_miles=2, engaged_time_s=10, total_drive_time_s=100, git_branch="old"),
        drive_row(route_name="2", git_commit="eeee", start_time_utc_ms=3000, length_miles=4, engaged_time_s=30, total_drive_time_s=100, git_branch="old"),
        drive_row(route_name="3", git_commit="eeee", start_time_utc_ms=5000, length_miles=3, engaged_time_s=20, total_drive_time_s=100, git_branch="renamed-nightly"),
    ]
    row = aggregate_commits(group)[0]
    assert row.first_drive_ms == 1000
    assert row.last_drive_ms == 5000
    assert row.total_miles == 9
    assert row.engaged_time_s == 60
    assert row.engage_pct == 20.0
    assert row.git_branch == "renamed-nightly"


def test_engage_pct_uses_not_in_park_not_wall_clock() -> None:
    group = [
        drive_row(
            route_name=f"p{i}",
            git_commit="npnp",
            start_time_utc_ms=1000 + i,
            engaged_time_s=1800,
            total_drive_time_s=3600,
            not_in_park_time_s=2000,
        )
        for i in range(3)
    ]
    row = aggregate_commits(group)[0]
    assert row.not_in_park_time_s == 6000
    assert abs(row.engage_pct - 90.0) < 1e-9
    assert row.drives[0].engage_pct == 90.0


def test_denominator_falls_back_to_wall_clock_before_reparse() -> None:
    d = drive_row(not_in_park_time_s=None, total_drive_time_s=3600, engaged_time_s=1800)
    assert denominator_s(d) == 3600.0
    assert denominator_s(drive_row(not_in_park_time_s=2000, total_drive_time_s=3600)) == 2000.0
