from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from op_usage.aggregate import aggregate_commits
from op_usage.pipeline import load_fixture_drives
from op_usage.site import commit_url, format_duration, format_pct, render_site

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drives.json"

CHROME = (
    "Personal engaged-time",
    "Private drives only",
    "Demo data",
    "Qualifying",
    "static page",
    "community data",
    "openpilot usage",
    "Include a drive",
)


def test_demo_html_is_table_only_qualified_commits_newest_first() -> None:
    commits = aggregate_commits(load_fixture_drives(FIXTURE))
    assert [c.short_hash for c in commits] == ["7c3a91b", "1e9d2c4", "0f1e2d3"]
    html = render_site(
        commits,
        owner_name="fixture driver",
        generated_at=datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc),
        display_tz="America/Los_Angeles",
        mode="demo",
    )
    assert "nightly-togo" in html
    assert "experimental-long" in html
    assert "release-c3" in html
    assert "wip-two-drives" not in html
    assert "mixed-filters" not in html
    assert html.index("nightly-togo") < html.index("experimental-long") < html.index("release-c3")
    assert 'title="7c3a91b0f2e44a1b9c0d1e2f3a4b5c6d7e8f9012"' in html
    assert "button" in html and "expand" in html
    assert html.count("deadbeefcafebabe|") == 0
    assert "18.4" in html
    for header in ("Branch", "Commit", "Date range", "Drives", "Miles", "Engaged time"):
        assert f"<th>{header}</th>" in html
    assert "Engage %" in html
    assert "Updated 2026-09-11 03:00 PDT" in html
    assert "<table class=\"nested\">" in html
    assert "2026-09-10" in html
    assert 'colspan="7"' in html
    for blob in CHROME:
        assert blob not in html
    assert "<title>Openpilot Stats</title>" in html
    assert "<h1" not in html
    assert "banner" not in html
    assert "<header" not in html


def test_helpers() -> None:
    assert format_duration(3900) == "1h 05m"
    assert format_pct(81.234) == "81.2%"
    assert commit_url("git@github.com:commaai/openpilot.git", "abc") == (
        "https://github.com/commaai/openpilot/commit/abc"
    )
    assert commit_url("https://github.com/foo/bar.git", "deadbeef") == (
        "https://github.com/foo/bar/commit/deadbeef"
    )
