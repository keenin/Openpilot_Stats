from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from op_usage.aggregate import aggregate_commits
from op_usage.pipeline import load_fixture_drives
from op_usage.site import commit_url, format_duration, format_pct, render_site

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drives.json"


def test_demo_html_lists_only_qualified_commits_newest_first() -> None:
    drives = load_fixture_drives(FIXTURE)
    commits = aggregate_commits(drives)
    hashes = [c.short_hash for c in commits]
    assert hashes == ["7c3a91b", "1e9d2c4", "0f1e2d3"]
    html = render_site(
        commits,
        owner_name="fixture driver",
        generated_at=datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc),
        display_tz="America/Los_Angeles",
        mode="demo",
    )
    assert "fixture driver · openpilot usage" in html
    assert "nightly-togo" in html
    assert "experimental-long" in html
    assert "release-c3" in html
    assert "wip-two-drives" not in html
    assert "mixed-filters" not in html
    assert "Demo data" in html
    assert 'title="7c3a91b0f2e44a1b9c0d1e2f3a4b5c6d7e8f9012"' in html
    assert "button" in html and "expand" in html
    assert html.index("nightly-togo") < html.index("experimental-long") < html.index("release-c3")
    # drill-down present but not as the main list of every drive
    assert html.count("deadbeefcafebabe|") == 0
    assert "18.4" in html


def test_expand_rows_include_drive_date_miles_pct() -> None:
    commits = aggregate_commits(load_fixture_drives(FIXTURE))
    html = render_site(
        commits,
        owner_name="x",
        generated_at=datetime.now(timezone.utc),
        display_tz="America/Los_Angeles",
        mode="demo",
    )
    assert "<table class=\"nested\">" in html
    assert "Engage %" in html
    assert "2026-09-10" in html


def test_helpers() -> None:
    assert format_duration(3900) == "1h 05m"
    assert format_pct(81.234) == "81.2%"
    assert commit_url("git@github.com:commaai/openpilot.git", "abc") == (
        "https://github.com/commaai/openpilot/commit/abc"
    )
    assert commit_url("https://github.com/foo/bar.git", "deadbeef") == (
        "https://github.com/foo/bar/commit/deadbeef"
    )
