"""Commute route file: missing, empty, and a real split.

Combined stats are the unsplit groups (what the single table shows), not the
sum of the two sections. A SHA with two commute and two other drives still
counts in Combined and in neither section, because each subset is under the
3-drive minimum.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from helpers import drive_row
from op_usage.aggregate import aggregate_commits
from op_usage.cache import Cache
from op_usage.commute import is_commute_route, load_commute_route_ids, partition_drives
from op_usage.config import Settings, default_commute_routes_path, load_settings
from op_usage.pipeline import generate_from_cache
from op_usage.site import format_section_stats, render_drives, section_stats

WHEN = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
TZ = "America/Los_Angeles"

COMMIT_C = "c" * 40
COMMIT_N = "n" * 40
COMMIT_M = "m" * 40
COMMIT_SOLO = "s" * 40

ROUTE_FILE = """\
# route ids only — no coordinates
2024-01-15--08-30-00
2024-01-16--08-31-00
BBBBBBBB--CCCCCCCCCC
chain-b
2024-02-01--07-00-00
2024-05-01--08-00-00
2024-01-17--08-30-00

"""

COMMUTE_LINE = "Commute · 3 drives · 30.0 mi · 2m 30s engaged · 15s override · 50.0%"
OTHER_LINE = "Non-commute · 3 drives · 6.0 mi · 30s engaged · 3s override · 25.0%"
COMBINED_LINE = "Combined · 10 drives · 40.0 mi · 3m 40s engaged · 26s override · 26.8%"
EMPTY_COMMUTE = "Commute · 0 drives · 0.0 mi · 0s engaged · 0s override · 0.0%"


def _drive(name, commit, branch, miles, engaged, nip, override, start):
    return drive_row(
        route_name=name,
        git_commit=commit,
        git_branch=branch,
        start_time_utc_ms=start,
        length_miles=miles,
        engaged_time_s=engaged,
        not_in_park_time_s=nip,
        override_time_s=override,
        total_drive_time_s=nip,
    )


def _sample_drives():
    rows = []
    # Match the file as a route-name part, not as the full stored name.
    for name, start in (
        ("dongle|2024-01-15--08-30-00", 1_000),
        ("dongle|2024-01-16--08-31-00", 2_000),
        ("dongle|bbbbbbbb--cccccccccc", 3_000),
    ):
        rows.append(_drive(name, COMMIT_C, "commute-nightly", 10, 50, 100, 5, start))
    for i, start in enumerate((10_000, 11_000, 12_000)):
        rows.append(
            _drive(
                f"dongle|2024-03-0{i + 1}--12-00-00",
                COMMIT_N,
                "weekend",
                2,
                10,
                40,
                1,
                start,
            )
        )
    # 2 commute + 2 other on one SHA. Unsplit this is a group of 4.
    rows.append(_drive("dongle|2024-02-01--07-00-00", COMMIT_M, "mixed-split", 1, 10, 100, 2, 20_000))
    rows.append(_drive("chain-a|chain-b", COMMIT_M, "mixed-split", 1, 10, 100, 2, 21_000))
    rows.append(_drive("dongle|2024-04-01--12-00-00", COMMIT_M, "mixed-split", 1, 10, 100, 2, 22_000))
    rows.append(_drive("dongle|2024-04-02--12-00-00", COMMIT_M, "mixed-split", 1, 10, 100, 2, 23_000))
    # Qualifies, but the SHA has one drive, so the ≥3 rule drops it.
    rows.append(_drive("dongle|2024-05-01--08-00-00", COMMIT_SOLO, "solo-commute", 100, 10, 10, 0, 30_000))
    # Commute id, under 1 mile.
    rows.append(_drive("dongle|2024-01-17--08-30-00", COMMIT_C, "commute-nightly", 0.4, 10, 10, 9, 500))
    return rows


def _render(drives, route_ids):
    return render_drives(
        drives,
        generated_at=WHEN,
        display_tz=TZ,
        commute_route_ids=route_ids,
    )


def _settings(tmp_path: Path, commute_path: Path | None) -> Settings:
    return Settings(
        comma_jwt=None,
        dongle_id=None,
        api_base="https://example.invalid",
        cache_path=tmp_path / "c.sqlite",
        qlog_dir=tmp_path / "qlogs",
        site_dir=tmp_path / "site",
        display_tz="UTC",
        backfill_start="2026-09-01",
        openpilot_path=None,
        cereal_path=None,
        cf_pages_project="op-usage",
        chunk_days=30,
        recheck_hours=24,
        files_min_interval_s=13,
        request_timeout_s=60,
        commute_routes_path=commute_path,
    )


def test_load_commute_route_ids_missing_empty_and_comments(tmp_path: Path) -> None:
    assert load_commute_route_ids(tmp_path / "missing.txt") is None
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing here\n\n   \n", encoding="utf-8")
    assert load_commute_route_ids(empty) == set()
    present = tmp_path / "ids.txt"
    present.write_text(ROUTE_FILE, encoding="utf-8")
    assert load_commute_route_ids(present) == {
        "2024-01-15--08-30-00",
        "2024-01-16--08-31-00",
        "bbbbbbbb--cccccccccc",
        "chain-b",
        "2024-02-01--07-00-00",
        "2024-05-01--08-00-00",
        "2024-01-17--08-30-00",
    }


def test_route_matches_any_name_part() -> None:
    ids = {"2024-01-15--08-30-00", "Chain-B", "bbbbbbbb--cccccccccc"}
    assert is_commute_route("dongle|2024-01-15--08-30-00", ids)
    assert is_commute_route("chain-a|chain-b", ids)
    assert is_commute_route("BBBBBBBB--CCCCCCCCCC", ids)
    assert is_commute_route("id-1, chain-b", ids)
    assert not is_commute_route("dongle|2024-03-01--12-00-00", ids)
    assert not is_commute_route("dongle", ids)
    assert not is_commute_route("", ids)
    assert not is_commute_route("chain-a|chain-b", set())

    drives = [
        drive_row(route_name="dongle|2024-01-15--08-30-00"),
        drive_row(route_name="dongle|other"),
    ]
    commute, other = partition_drives(drives, ids)
    assert [d.route_name for d in commute] == ["dongle|2024-01-15--08-30-00"]
    assert [d.route_name for d in other] == ["dongle|other"]


def test_missing_commute_file_renders_one_table() -> None:
    drives = _sample_drives()
    html = _render(drives, None)
    assert html.count('<table class="main">') == 1
    assert '<p class="section">' not in html
    assert "Combined ·" not in html
    assert "Commute ·" not in html
    assert "commute-nightly" in html
    assert "weekend" in html
    # Unsplit, the 2+2 SHA still has four drives, so it is on the page.
    assert "mixed-split" in html
    assert "solo-commute" not in html
    assert "Updated 2026-09-11 03:00 PDT" in html
    assert html.count("Updated ") == 1
    assert html.count("Override is Connect gray") == 1
    assert "<h1" not in html
    assert "2024-01-15--08-30-00" not in html


def test_commute_file_splits_and_combined_equals_full_set(tmp_path: Path) -> None:
    drives = _sample_drives()
    route_file = tmp_path / "commute_routes.txt"
    route_file.write_text(ROUTE_FILE, encoding="utf-8")
    route_ids = load_commute_route_ids(route_file)
    assert route_ids is not None

    html = _render(drives, route_ids)
    full = section_stats(aggregate_commits(drives))
    assert f'<p class="section">{COMMUTE_LINE}</p>' in html
    assert f'<p class="section">{OTHER_LINE}</p>' in html
    assert f'<p class="totals">{COMBINED_LINE}</p>' in html
    assert COMBINED_LINE.endswith(format_section_stats(full))
    assert full.drive_count == 10
    assert full.total_miles == 40
    assert full.engaged_time_s == 220
    assert full.override_time_s == 26
    assert abs(full.engage_pct - (100.0 * 220 / 820)) < 1e-9

    assert html.index('<p class="section">Commute') < html.index('<p class="section">Non-commute')
    assert html.index('<p class="section">Non-commute') < html.index('<p class="totals">Combined')
    assert html.index("Combined ·") < html.index("Updated ")
    commute_html, rest = html.split('<p class="section">Non-commute', 1)
    other_html = rest.split('<p class="totals">', 1)[0]
    assert "commute-nightly" in commute_html
    assert "weekend" not in commute_html
    assert "weekend" in other_html
    assert "commute-nightly" not in other_html
    # Subsets of the mixed SHA are each under 3 drives, so the branch is gone.
    assert "mixed-split" not in html
    assert "solo-commute" not in html
    assert html.count('<table class="main">') == 2
    assert html.count("Updated ") == 1
    assert html.count("Override is Connect gray") == 1
    assert "2024-01-15--08-30-00" not in html
    assert "<h1" not in html


def test_empty_commute_set_shows_empty_section() -> None:
    drives = _sample_drives()
    html = _render(drives, set())
    full = format_section_stats(section_stats(aggregate_commits(drives)))
    assert f'<p class="section">{EMPTY_COMMUTE}</p>' in html
    assert f'<p class="section">Non-commute · {full}</p>' in html
    assert f'<p class="totals">Combined · {full}</p>' in html
    assert html.index('<p class="section">Commute') < html.index('<p class="section">Non-commute')
    commute_html, rest = html.split('<p class="section">Non-commute', 1)
    assert "commute-nightly" not in commute_html
    assert "weekend" not in commute_html
    assert "commute-nightly" in rest
    assert "weekend" in rest
    assert "mixed-split" in rest
    assert html.count("Updated ") == 1


def test_generate_missing_file_is_one_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("op_usage.pipeline.make_weights_lookup", lambda cache, **k: None)
    drives = [
        drive_row(
            route_name=f"dongle|r{i}",
            git_commit="e" * 40,
            git_branch="only-branch",
            start_time_utc_ms=1_000 + i,
        )
        for i in range(3)
    ]
    settings = _settings(tmp_path, tmp_path / "absent.txt")
    with Cache(settings.cache_path) as cache:
        cache.replace_all(drives)
        path = generate_from_cache(settings, cache)
    html = Path(path).read_text(encoding="utf-8")
    assert html.count('<table class="main">') == 1
    assert '<p class="section">' not in html
    assert "Combined ·" not in html
    assert "only-branch" in html


def test_generate_from_cache_splits_and_keeps_master_eras(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "op_usage.pipeline.make_weights_lookup",
        lambda cache, **k: (lambda commit, remote: "era"),
    )
    routes = tmp_path / "commute_routes.txt"
    routes.write_text("route-a\nroute-b\nroute-c\n", encoding="utf-8")
    drives = []
    for i, commit in enumerate(("a" * 40, "a" * 40, "b" * 40)):
        drives.append(
            drive_row(
                route_name=f"dongle|route-{chr(ord('a') + i)}",
                git_commit=commit,
                git_branch="master",
                git_remote="git@github.com:commaai/openpilot.git",
                start_time_utc_ms=1_000 + i,
            )
        )
    for i in range(3):
        drives.append(
            drive_row(
                route_name=f"dongle|other-{i}",
                git_commit="d" * 40,
                git_branch="side",
                start_time_utc_ms=5_000 + i,
            )
        )
    settings = _settings(tmp_path, routes)
    with Cache(settings.cache_path) as cache:
        cache.replace_all(drives)
        path = generate_from_cache(settings, cache)
    html = Path(path).read_text(encoding="utf-8")
    assert html.index('<p class="section">Commute') < html.index('<p class="section">Non-commute')
    commute_html = html.split('<p class="section">Non-commute', 1)[0]
    assert "aaaaaaa…bbbbbbb" in commute_html
    assert "side" not in commute_html
    assert "side" in html
    assert html.count("Updated ") == 1


def test_commute_routes_path_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("op_usage.config.load_credential_files", lambda: [])
    monkeypatch.delenv("OP_USAGE_COMMUTE_ROUTES", raising=False)
    assert load_settings().commute_routes_path == default_commute_routes_path()
    custom = tmp_path / "mine.txt"
    monkeypatch.setenv("OP_USAGE_COMMUTE_ROUTES", str(custom))
    assert load_settings().commute_routes_path == custom.resolve()
    monkeypatch.setenv("OP_USAGE_COMMUTE_ROUTES", "   ")
    assert load_settings().commute_routes_path == default_commute_routes_path()
