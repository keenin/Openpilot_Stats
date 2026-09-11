from __future__ import annotations

from op_usage.cache import Cache, DriveRow
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
        cache.save_engaged("d|r", 0.0, "controlsState.enabled")
        existing = cache.get_drive("d|r")
        assert existing and existing.qlog_parsed
        assert existing.engaged_time_s == 0.0
        assert cache.needs_qlog_parse(_row(maxqlog=1), recheck_after_ms=10_000) is False
        n = cache.clear_engaged_parses()
        assert n == 1
        cleared = cache.get_drive("d|r")
        assert cleared and not cleared.qlog_parsed
        assert cleared.engaged_time_s is None
        assert cleared.engaged_source is None
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
            "length": 0.7,
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
