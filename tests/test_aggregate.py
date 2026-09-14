from __future__ import annotations

from helpers import drive_row
from op_usage.aggregate import (
    aggregate_commits,
    denominator_s,
    is_master_branch,
    park_time_usable,
    qualifies,
)

REMOTE = "git@github.com:commaai/openpilot.git"


def _master(commit: str, n: int, t0: int, **kwargs):
    prefix = kwargs.pop("prefix", commit)
    branch = kwargs.pop("git_branch", "master")
    return [
        drive_row(
            route_name=f"{prefix}-{i}",
            git_commit=commit,
            git_branch=branch,
            git_remote=REMOTE,
            start_time_utc_ms=t0 + i,
            **kwargs,
        )
        for i in range(n)
    ]


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


def test_denominator_park_zero_with_engaged_falls_back() -> None:
    missing = drive_row(not_in_park_time_s=None, total_drive_time_s=3600, engaged_time_s=1800)
    assert denominator_s(missing) == 3600.0
    assert denominator_s(drive_row(not_in_park_time_s=2000, total_drive_time_s=3600)) == 2000.0
    zero = drive_row(not_in_park_time_s=0.0, total_drive_time_s=3600, engaged_time_s=1800)
    assert park_time_usable(zero) is False
    assert denominator_s(zero) == 3600.0
    parked = drive_row(not_in_park_time_s=0.0, total_drive_time_s=3600, engaged_time_s=0.0)
    assert park_time_usable(parked) is True
    assert denominator_s(parked) == 0.0
    group = [
        drive_row(
            route_name=f"z{i}",
            git_commit="zero-park",
            start_time_utc_ms=1000 + i,
            engaged_time_s=1800,
            total_drive_time_s=3600,
            not_in_park_time_s=0.0,
        )
        for i in range(3)
    ]
    row = aggregate_commits(group)[0]
    assert row.not_in_park_time_s == 10800
    assert abs(row.engage_pct - 50.0) < 1e-9


def test_engage_pct_ignores_weighted_sqlite_fields() -> None:
    group = [
        drive_row(
            route_name=f"w{i}",
            git_commit="wwww",
            start_time_utc_ms=1000 + i,
            engaged_time_s=1800,
            total_drive_time_s=3600,
            not_in_park_time_s=2000,
            weighted_engaged_time_s=200,
        )
        for i in range(3)
    ]
    row = aggregate_commits(group)[0]
    assert abs(row.engage_pct - 90.0) < 1e-9
    assert row.drives[0].engage_pct == 90.0
    assert not hasattr(row, "weighted_engaged_time_s")
    assert not hasattr(row, "weight_pct")


def test_non_master_stays_one_row_per_sha_even_with_lookup() -> None:
    a = _master("aaa1111111111111111111111111111111111111", 3, 1000, git_branch="nightly")
    b = _master("bbb2222222222222222222222222222222222222", 3, 2000, git_branch="nightly")
    rows = aggregate_commits(a + b, weights_lookup=lambda c, r: "same")
    assert {r.git_commit for r in rows} == {a[0].git_commit, b[0].git_commit}


def test_master_same_weights_merge_and_three_rule_uses_merged_count() -> None:
    a = _master("aaaaaaa111111111111111111111111111111111", 2, 1000)
    b = _master("bbbbbbb222222222222222222222222222222222", 1, 2000)
    rows = aggregate_commits(a + b, weights_lookup=lambda c, r: "era-1")
    assert len(rows) == 1
    assert rows[0].drive_count == 3
    assert rows[0].git_branch == "master"
    assert rows[0].git_commit.startswith("bbbbbbb")
    assert rows[0].era_first_commit.startswith("aaaaaaa")
    assert rows[0].short_hash == "aaaaaaa…bbbbbbb"
    assert rows[0].last_drive_ms == 2000
    thin = _master("eeeeeee111111111111111111111111111111111", 1, 1000)
    thin += _master("fffffff222222222222222222222222222222222", 1, 2000)
    assert aggregate_commits(thin, weights_lookup=lambda c, r: "same") == []


def test_master_unknown_fingerprint_does_not_merge() -> None:
    a = _master("1111111111111111111111111111111111111111", 2, 1000)
    b = _master("2222222222222222222222222222222222222222", 1, 2000)
    assert aggregate_commits(a + b, weights_lookup=lambda c, r: None) == []
    listed = _master("1111111111111111111111111111111111111111", 3, 1000)
    listed += _master("2222222222222222222222222222222222222222", 3, 2000)
    rows = aggregate_commits(listed, weights_lookup=lambda c, r: None)
    assert {r.git_commit for r in rows} == {listed[0].git_commit, listed[3].git_commit}


def test_master_rollback_sha_is_new_interval_not_wrapped() -> None:
    # Walk start-time order: a SHA after a different fingerprint is a new interval.
    old = "ccccccc111111111111111111111111111111111"
    new = "ddddddd222222222222222222222222222222222"
    later = "eeeeeee333333333333333333333333333333333"
    drives = (
        _master(old, 2, 1000, prefix="early")
        + _master(new, 3, 2000)
        + _master(old, 3, 4000, prefix="rollback")
        + _master(later, 2, 5000)
    )
    fps = {old: "era-old", new: "era-new", later: "era-old"}
    rows = aggregate_commits(drives, weights_lookup=lambda c, r: fps[c])
    assert len(rows) == 2
    assert rows[0].drive_count == 5
    assert rows[0].git_commit.startswith("eeeeeee")
    assert rows[0].era_first_commit.startswith("ccccccc")
    assert {d.route_name for d in rows[0].drives} == {
        "rollback-0",
        "rollback-1",
        "rollback-2",
        f"{later}-0",
        f"{later}-1",
    }
    assert rows[1].drive_count == 3
    assert rows[1].git_commit.startswith("ddddddd")


def test_master_branch_aliases_merge_like_master() -> None:
    assert is_master_branch("master")
    assert is_master_branch("origin/master")
    assert is_master_branch("refs/heads/master")
    assert is_master_branch("Master")
    assert not is_master_branch("nightly")
    assert not is_master_branch("my-master")
    a = _master("aaaaaaa111111111111111111111111111111111", 2, 1000, git_branch="origin/master")
    b = _master("bbbbbbb222222222222222222222222222222222", 1, 2000, git_branch="refs/heads/master")
    rows = aggregate_commits(a + b, weights_lookup=lambda c, r: "era")
    assert len(rows) == 1
    assert rows[0].drive_count == 3
