from __future__ import annotations

from op_usage.cache import SCHEMA_VERSION, Cache, DriveRow
from op_usage.comma_api import iter_time_chunks, normalize_routes


def _row(**kwargs) -> DriveRow:
    base = dict(
        route_name="d|r",
        dongle_id="deadbeefcafebabe",
        start_time_utc_ms=1_000,
        end_time_utc_ms=2_000,
        length_miles=3.0,
        total_drive_time_s=100.0,
        git_commit="abc",
        git_branch="n",
        git_remote="",
        maxqlog=1,
        engaged_time_s=10.0,
        engaged_source="selfdriveState.enabled",
        qlog_parsed=True,
    )
    base.update(kwargs)
    return DriveRow(**base)


def test_migrates_not_in_park_column_on_old_sqlite(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE drives (
          route_name TEXT PRIMARY KEY,
          dongle_id TEXT NOT NULL,
          start_time_utc_ms INTEGER NOT NULL,
          end_time_utc_ms INTEGER NOT NULL,
          length_miles REAL NOT NULL,
          total_drive_time_s REAL NOT NULL,
          git_commit TEXT,
          git_branch TEXT,
          git_remote TEXT,
          maxqlog INTEGER,
          engaged_time_s REAL,
          engaged_source TEXT,
          qlog_parsed INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        );
        INSERT INTO meta(key, value) VALUES ('schema_version', '2');
        INSERT INTO drives VALUES (
          'd|r', 'd', 1, 2, 3.0, 100.0, 'abc', 'n', '', 1,
          10.0, 'selfdriveState.enabled', 1, 't'
        );
        """
    )
    conn.commit()
    conn.close()
    with Cache(path) as cache:
        assert cache.schema_upgraded_from == "2"
        assert cache.get_meta("schema_version") == SCHEMA_VERSION
        row = cache.get_drive("d|r")
        assert row is not None
        assert row.engaged_time_s == 10.0
        assert row.not_in_park_time_s is None


def test_watermark_only_moves_forward(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.set_watermark_ms(50)
        cache.set_watermark_ms(40)
        assert cache.watermark_ms() == 50
        cache.set_watermark_ms(80)
        assert cache.watermark_ms() == 80


def test_clear_engaged_parses_allows_reparse(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(_row(qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("d|r", 0.0, "controlsState.enabled", 50.0)
        existing = cache.get_drive("d|r")
        assert existing and existing.qlog_parsed
        assert existing.engaged_time_s == 0.0
        assert existing.not_in_park_time_s == 50.0
        assert cache.needs_qlog_parse(_row(maxqlog=1), recheck_after_ms=10_000) is False
        n = cache.clear_engaged_parses()
        assert n == 1
        cleared = cache.get_drive("d|r")
        assert cleared and not cleared.qlog_parsed
        assert cleared.engaged_time_s is None
        assert cleared.engaged_source is None
        assert cleared.not_in_park_time_s is None
        assert cache.needs_qlog_parse(_row(maxqlog=1), recheck_after_ms=10_000) is True


def test_parsed_qlog_not_redone_outside_recheck(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(_row(qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("d|r", 12.5, "selfdriveState.enabled")
        existing = cache.get_drive("d|r")
        assert existing and existing.qlog_parsed
        newer_meta = _row(maxqlog=1)
        assert cache.needs_qlog_parse(newer_meta, recheck_after_ms=10_000) is False
        in_window = _row(start_time_utc_ms=20_000, maxqlog=4)
        cache.upsert_route_meta(_row(start_time_utc_ms=20_000, maxqlog=1, qlog_parsed=True))
        cache.save_engaged("d|r", 12.5, "selfdriveState.enabled")
        assert cache.needs_qlog_parse(in_window, recheck_after_ms=15_000) is True


def test_normalize_route_segments() -> None:
    payload = [
        {
            "fullname": "deadbeefcafebabe|2026-01-01--00-00-00",
            "dongle_id": "deadbeefcafebabe",
            "length": 12.5,
            "git_commit": "abc",
            "git_branch": "nightly",
            "git_remote": "git@github.com:commaai/openpilot.git",
            "maxqlog": 8,
            "segment_start_times": [1000, 2000],
            "segment_end_times": [2000, 3000],
        }
    ]
    routes = normalize_routes(payload, "deadbeefcafebabe")
    assert len(routes) == 1
    assert routes[0].length_miles == 12.5
    assert routes[0].start_time_utc_ms == 1000
    assert routes[0].end_time_utc_ms == 3000
    assert routes[0].git_commit == "abc"


def test_normalize_prefers_distance_over_length() -> None:
    """Live routes_segments uses `distance` (miles). OpenAPI still says `length`.

    commaai/connect copies length → distance only when distance is absent.
    A leftover tiny `length` must not hide a real `distance`.
    """
    payload = [
        {
            "fullname": "d|r",
            "dongle_id": "d",
            "distance": 18.4,
            "length": 0.00179515,
            "git_commit": "abc",
            "git_branch": "nightly",
            "git_remote": "",
            "maxqlog": 2,
            "segment_start_times": [1000],
            "segment_end_times": [61000],
        }
    ]
    routes = normalize_routes(payload, "d")
    assert routes[0].length_miles == 18.4


def test_normalize_distance_only() -> None:
    payload = [
        {
            "fullname": "d|r",
            "dongle_id": "d",
            "distance": 9.25,
            "git_commit": "abc",
            "git_branch": "n",
            "git_remote": "",
            "maxqlog": 1,
            "segment_start_times": [1],
            "segment_end_times": [2],
        }
    ]
    routes = normalize_routes(payload, "d")
    assert routes[0].length_miles == 9.25


def test_normalize_missing_length_is_zero() -> None:
    payload = [
        {
            "fullname": "d|r",
            "dongle_id": "d",
            "git_commit": "abc",
            "git_branch": "n",
            "git_remote": "",
            "maxqlog": 1,
            "segment_start_times": [1],
            "segment_end_times": [2],
        }
    ]
    routes = normalize_routes(payload, "d")
    assert routes[0].length_miles == 0.0


def test_normalize_explicit_zero_distance_not_overridden_by_length() -> None:
    payload = [
        {
            "fullname": "d|r",
            "dongle_id": "d",
            "distance": 0,
            "length": 12.5,
            "git_commit": "abc",
            "git_branch": "n",
            "git_remote": "",
            "maxqlog": 1,
            "segment_start_times": [1],
            "segment_end_times": [2],
        }
    ]
    routes = normalize_routes(payload, "d")
    assert routes[0].length_miles == 0.0


def test_upsert_route_meta_refreshes_length_without_clearing_engaged(tmp_path) -> None:
    """Metadata backfill rewrites length_miles; cached qlog parses stay put."""
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(_row(length_miles=0.0, qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("d|r", 42.0, "selfdriveState.enabled", 80.0)
        cache.upsert_route_meta(_row(length_miles=12.5, qlog_parsed=False, engaged_time_s=None))
        row = cache.get_drive("d|r")
        assert row is not None
        assert row.length_miles == 12.5
        assert row.engaged_time_s == 42.0
        assert row.qlog_parsed is True
        assert row.engaged_source == "selfdriveState.enabled"
        assert row.not_in_park_time_s == 80.0


def test_normalize_groups_segments() -> None:
    payload = [
        {
            "canonical_route_name": "d|r1",
            "dongle_id": "d",
            "number": 0,
            "length": 0.4,
            "git_commit": "fff",
            "git_branch": "x",
            "git_remote": "",
            "start_time_utc_millis": 10,
            "end_time_utc_millis": 20,
        },
        {
            "canonical_route_name": "d|r1",
            "dongle_id": "d",
            "number": 1,
            "distance": 0.7,
            "git_commit": "fff",
            "git_branch": "x",
            "git_remote": "",
            "start_time_utc_millis": 20,
            "end_time_utc_millis": 30,
        },
    ]
    routes = normalize_routes(payload, "d")
    assert len(routes) == 1
    assert abs(routes[0].length_miles - 1.1) < 1e-9
    assert routes[0].start_time_utc_ms == 10
    assert routes[0].end_time_utc_ms == 30


def test_time_chunks() -> None:
    day = 24 * 3600 * 1000
    chunks = iter_time_chunks(0, 3 * day, chunk_days=1)
    assert chunks == [(0, day), (day, 2 * day), (2 * day, 3 * day)]
