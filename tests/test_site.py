from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from op_usage.aggregate import aggregate_commits
from op_usage.pipeline import load_fixture_drives
from op_usage.site import render_site

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drives.json"


def test_demo_html_lists_qualified_commits_newest_first() -> None:
    commits = aggregate_commits(load_fixture_drives(FIXTURE))
    assert [c.short_hash for c in commits] == ["7c3a91b", "1e9d2c4", "0f1e2d3"]
    html = render_site(
        commits,
        generated_at=datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc),
        display_tz="America/Los_Angeles",
    )
    assert "nightly-togo" in html
    assert "experimental-long" in html
    assert "release-c3" in html
    assert "wip-two-drives" not in html
    assert "mixed-filters" not in html
    assert html.index("nightly-togo") < html.index("experimental-long") < html.index("release-c3")
    assert "Engage %" in html
    assert "Weighted engaged" not in html
    assert "Updated 2026-09-11 03:00 PDT" in html
