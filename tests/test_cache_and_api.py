from __future__ import annotations

import pytest

from helpers import drive_row
from op_usage.cache import SCHEMA_VERSION, Cache
from op_usage.comma_api import CommaApiError, CommaClient, iter_time_chunks, normalize_routes


def _seg(**kwargs) -> dict:
    row = {
        "fullname": "d|r",
        "dongle_id": "d",
        "git_commit": "abc",
        "git_branch": "n",
        "git_remote": "",
        "maxqlog": 1,
        "segment_start_times": [1],
        "segment_end_times": [2],
    }
    row.update(kwargs)
    return row


class _Resp:
    def __init__(self, status: int, content: bytes = b"", text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.headers: dict = {}
        self.ok = 200 <= status < 300
        self.content = content


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


def test_clear_engaged_parses_can_scope_to_listed_routes(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(drive_row(route_name="keep", qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("keep", 11.0, "selfdriveState.enabled", 20.0)
        cache.upsert_route_meta(drive_row(route_name="wipe", qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("wipe", 9.0, "selfdriveState.enabled", 15.0)
        n = cache.clear_engaged_parses(route_names=["wipe"])
        assert n == 1
        kept = cache.get_drive("keep")
        wiped = cache.get_drive("wipe")
        assert kept and kept.qlog_parsed and kept.engaged_time_s == 11.0
        assert wiped and not wiped.qlog_parsed
        assert wiped.engaged_time_s is None
        assert wiped.not_in_park_time_s is None


def test_clear_engaged_parses_allows_reparse(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(drive_row(qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("d|r", 0.0, "controlsState.enabled", 50.0)
        existing = cache.get_drive("d|r")
        assert existing and existing.qlog_parsed
        assert existing.engaged_time_s == 0.0
        assert existing.not_in_park_time_s == 50.0
        assert cache.needs_qlog_parse(drive_row(maxqlog=1)) is False
        n = cache.clear_engaged_parses()
        assert n == 1
        cleared = cache.get_drive("d|r")
        assert cleared and not cleared.qlog_parsed
        assert cleared.engaged_time_s is None
        assert cleared.engaged_source is None
        assert cleared.not_in_park_time_s is None
        assert cache.needs_qlog_parse(drive_row(maxqlog=1)) is True


def test_parsed_qlog_not_redone_outside_recheck(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(drive_row(qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("d|r", 12.5, "selfdriveState.enabled")
        existing = cache.get_drive("d|r")
        assert existing and existing.qlog_parsed
        assert cache.needs_qlog_parse(drive_row(maxqlog=1)) is False
        in_window = drive_row(start_time_utc_ms=20_000, maxqlog=4)
        cache.upsert_route_meta(drive_row(start_time_utc_ms=20_000, maxqlog=1, qlog_parsed=True))
        cache.save_engaged("d|r", 12.5, "selfdriveState.enabled")
        assert cache.needs_qlog_parse(in_window) is True


def test_needs_qlog_parse_when_maxqlog_grows_even_if_start_is_old(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(drive_row(start_time_utc_ms=1_000, end_time_utc_ms=2_000, maxqlog=1))
        cache.save_engaged("d|r", 12.5, "selfdriveState.enabled")
        grown = drive_row(start_time_utc_ms=1_000, end_time_utc_ms=2_000, maxqlog=4)
        assert cache.needs_qlog_parse(grown) is True
        same = drive_row(start_time_utc_ms=1_000, end_time_utc_ms=2_000, maxqlog=1)
        assert cache.needs_qlog_parse(same) is False


def test_needs_qlog_parse_does_not_reparse_when_maxqlog_unchanged(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(drive_row(start_time_utc_ms=1_000, end_time_utc_ms=20_000, maxqlog=1))
        cache.save_engaged("d|r", 12.5, "selfdriveState.enabled")
        recent = drive_row(start_time_utc_ms=1_000, end_time_utc_ms=20_000, maxqlog=1)
        assert cache.needs_qlog_parse(recent) is False


def test_upsert_does_not_blank_git_or_zero_length(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(
            drive_row(
                length_miles=12.5,
                git_commit="abcabcabc",
                git_branch="nightly",
                git_remote="git@github.com:commaai/openpilot.git",
            )
        )
        cache.upsert_route_meta(
            drive_row(
                length_miles=0.001,
                git_commit="",
                git_branch="",
                git_remote="",
                maxqlog=9,
            )
        )
        row = cache.get_drive("d|r")
        assert row is not None
        assert row.length_miles == 12.5
        assert row.git_commit == "abcabcabc"
        assert row.git_branch == "nightly"
        assert row.git_remote == "git@github.com:commaai/openpilot.git"
        assert row.maxqlog == 9


def test_settling_start_ignores_ancient_unparsed(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(
            drive_row(route_name="done", start_time_utc_ms=1, end_time_utc_ms=2, maxqlog=1)
        )
        cache.save_engaged("done", 1.0, "selfdriveState.enabled")
        cache.upsert_route_meta(
            drive_row(
                route_name="unparsed-2018",
                start_time_utc_ms=50,
                end_time_utc_ms=60,
                qlog_parsed=False,
                engaged_time_s=None,
            )
        )
        cache.upsert_route_meta(
            drive_row(route_name="inflight", start_time_utc_ms=80, end_time_utc_ms=20_000, maxqlog=1)
        )
        cache.save_engaged("inflight", 2.0, "selfdriveState.enabled")
        assert cache.earliest_settling_start_ms(15_000) == 80
        assert cache.late_upload_starts(since_ms=0, recheck_after_ms=15_000) == [1, 50]


def test_replace_all_refuses_live_cache(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    path = tmp_path / "live.sqlite"
    monkeypatch.setattr(
        "op_usage.cache.is_live_cache_path",
        lambda p: Path(p).resolve() == path.resolve(),
    )
    with Cache(path) as cache:
        cache.upsert_route_meta(drive_row())
        cache.commit()
        try:
            cache.replace_all([])
        except RuntimeError as exc:
            assert "live cache" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
        assert cache.get_drive("d|r") is not None


def test_normalize_route_times_and_git() -> None:
    payload = [
        _seg(
            fullname="deadbeefcafebabe|2026-01-01--00-00-00",
            dongle_id="deadbeefcafebabe",
            length=12.5,
            git_branch="nightly",
            git_remote="git@github.com:commaai/openpilot.git",
            maxqlog=8,
            segment_start_times=[1000, 2000],
            segment_end_times=[2000, 3000],
        )
    ]
    routes = normalize_routes(payload, "deadbeefcafebabe")
    assert len(routes) == 1
    assert routes[0].length_miles == 12.5
    assert routes[0].start_time_utc_ms == 1000
    assert routes[0].end_time_utc_ms == 3000
    assert routes[0].git_commit == "abc"


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"length": 12.5}, 12.5),
        ({"distance": 18.4, "length": 0.00179515}, 18.4),
        ({"distance": 9.25}, 9.25),
        ({}, 0.0),
        ({"distance": 0, "length": 12.5}, 0.0),
    ],
    ids=["length-only", "prefer-distance", "distance-only", "missing", "explicit-zero-distance"],
)
def test_normalize_length_miles(fields, expected) -> None:
    # Live routes_segments uses `distance` (miles). OpenAPI still says `length`.
    # An explicit distance=0 must not fall back to length.
    assert normalize_routes([_seg(**fields)], "d")[0].length_miles == expected


def test_upsert_route_meta_refreshes_length_without_clearing_engaged(tmp_path) -> None:
    with Cache(tmp_path / "c.sqlite") as cache:
        cache.upsert_route_meta(drive_row(length_miles=0.0, qlog_parsed=False, engaged_time_s=None))
        cache.save_engaged("d|r", 42.0, "selfdriveState.enabled", 80.0)
        cache.upsert_route_meta(drive_row(length_miles=12.5, qlog_parsed=False, engaged_time_s=None))
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


def test_normalize_skips_empty_route_name() -> None:
    payload = [
        {
            "fullname": "",
            "dongle_id": "d",
            "distance": 3.0,
            "git_commit": "abc",
            "segment_start_times": [1],
            "segment_end_times": [2],
        },
        {
            "fullname": "d|ok",
            "dongle_id": "d",
            "distance": 4.0,
            "git_commit": "abc",
            "segment_start_times": [1],
            "segment_end_times": [2],
        },
    ]
    routes = normalize_routes(payload, "d")
    assert [r.route_name for r in routes] == ["d|ok"]


def test_verify_auth_401() -> None:
    class _Sess:
        def get(self, *args, **kwargs):
            return _Resp(401, text="unauthorized")

    client = CommaClient("jwt", "dongle", session=_Sess(), sleeper=lambda _s: None)
    try:
        client.verify_auth()
    except CommaApiError as exc:
        assert exc.status == 401
        assert "jwt.comma.ai" in str(exc)
    else:
        raise AssertionError("expected 401")


def test_download_bytes_retries_server_error() -> None:
    class _Sess:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                return _Resp(503)
            return _Resp(200, b"qlog")

    sess = _Sess()
    client = CommaClient("jwt", "dongle", session=sess, sleeper=lambda _s: None)
    assert client.download_bytes("https://example.invalid/qlog") == b"qlog"
    assert sess.calls == 3


def test_time_chunks() -> None:
    day = 24 * 3600 * 1000
    chunks = iter_time_chunks(0, 3 * day, chunk_days=1)
    assert chunks == [(0, day), (day, 2 * day), (2 * day, 3 * day)]
