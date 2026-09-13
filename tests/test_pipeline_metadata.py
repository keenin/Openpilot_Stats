from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from helpers import drive_row
from op_usage.cache import Cache
from op_usage.config import Settings
from op_usage.pipeline import LATE_UPLOAD_HOURS, _coalesce_start_windows, run_pipeline
from op_usage.qlog import EnabledSample, GEAR_SOURCE, SELFDRIVE_SOURCE, encode_synthetic_qlog

NS = 1_000_000_000
_SEP1_MS = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)

ROUTE = {
    "fullname": "deadbeefcafebabe|2026-09-01--00-00-00",
    "dongle_id": "deadbeefcafebabe",
    "distance": 12.5,
    "length": 0.0,
    "git_commit": "abcabcabc",
    "git_branch": "nightly",
    "git_remote": "git@github.com:commaai/openpilot.git",
    "maxqlog": 3,
    "segment_start_times": [_SEP1_MS],
    "segment_end_times": [_SEP1_MS + 600_000],
}


def _route_start_ms(route: dict) -> int:
    starts = route.get("segment_start_times") or [0]
    return int(min(starts))


def _in_window(route: dict, start_ms: int, end_ms: int) -> bool:
    start = _route_start_ms(route)
    return start_ms <= start < end_ms


class _FakeClient:
    def __init__(self) -> None:
        self.qlog_calls = 0
        self.windows: list[tuple[int, int]] = []

    def verify_auth(self) -> dict:
        return {}

    def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        self.windows.append((start_ms, end_ms))
        return [ROUTE] if _in_window(ROUTE, start_ms, end_ms) else []

    def route_qlog_urls(self, route_name: str) -> list[str]:
        self.qlog_calls += 1
        raise AssertionError("qlogs must not be fetched in metadata-only mode")

    def download_bytes(self, url: str) -> bytes:
        self.qlog_calls += 1
        raise AssertionError("qlogs must not be fetched in metadata-only mode")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        comma_jwt="jwt",
        dongle_id="deadbeefcafebabe",
        api_base="https://example.invalid",
        cache_path=tmp_path / "c.sqlite",
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
    )


def _seed(cache: Cache, route_name: str, *, engaged: float, not_in_park: float | None = None, **meta) -> None:
    cache.upsert_route_meta(
        drive_row(route_name=route_name, qlog_parsed=False, engaged_time_s=None, **meta)
    )
    cache.save_engaged(route_name, engaged, "selfdriveState.enabled", not_in_park)


def test_coalesce_start_windows_merges_nearby_only() -> None:
    assert _coalesce_start_windows([]) == []
    assert _coalesce_start_windows([1_000], pad_ms=100) == [(1_000, 1_100)]
    assert _coalesce_start_windows([1_000, 1_050], pad_ms=100) == [(1_000, 1_150)]
    assert _coalesce_start_windows([1_000, 5_000], pad_ms=100) == [(1_000, 1_100), (5_000, 5_100)]


def test_metadata_only_refreshes_length_without_qlogs(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    monkeypatch.setattr(
        "op_usage.pipeline.load_event_module",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cereal must not load")),
    )
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            ROUTE["fullname"],
            engaged=12509.5,
            dongle_id=ROUTE["dongle_id"],
            length_miles=0.0,
        )
        cache.commit()

    stats = run_pipeline(settings, backfill=True, metadata_only=True)
    assert stats.routes_listed >= 1
    assert stats.qlogs_parsed == 0
    assert fake.qlog_calls == 0

    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(ROUTE["fullname"])
    assert row is not None
    assert row.length_miles == 12.5
    assert row.engaged_time_s == 12509.5
    assert row.qlog_parsed is True
    assert row.engaged_source == "selfdriveState.enabled"
    assert (settings.site_dir / "index.html").is_file()


def test_metadata_only_rejects_reparse(tmp_path) -> None:
    try:
        run_pipeline(_settings(tmp_path), backfill=True, metadata_only=True, reparse_engaged=True)
    except SystemExit as exc:
        assert "--metadata-only cannot be combined with --reparse-engaged" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def _qlog_blob() -> bytes:
    samples = [
        EnabledSample(0, True, SELFDRIVE_SOURCE),
        EnabledSample(2 * NS, True, SELFDRIVE_SOURCE),
        EnabledSample(0, True, GEAR_SOURCE),
        EnabledSample(2 * NS, True, GEAR_SOURCE),
    ]
    return encode_synthetic_qlog(samples, compress=None)


class _QlogClient:
    def __init__(self, routes: list[dict]) -> None:
        self.routes = routes
        self.qlog_calls = 0
        self.windows: list[tuple[int, int]] = []
        self.blob = _qlog_blob()

    def verify_auth(self) -> dict:
        return {}

    def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        self.windows.append((start_ms, end_ms))
        return [route for route in self.routes if _in_window(route, start_ms, end_ms)]

    def route_qlog_urls(self, route_name: str) -> list[str]:
        self.qlog_calls += 1
        return [f"https://example.invalid/{route_name}.qlog"]

    def download_bytes(self, url: str) -> bytes:
        return self.blob


def test_nightly_reparses_when_maxqlog_grows(tmp_path, monkeypatch) -> None:
    """24h recheck must compare API maxqlog to the cached value, not the post-upsert row."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 3_600_000
    route = {
        **ROUTE,
        "fullname": "deadbeefcafebabe|in-flight",
        "maxqlog": 4,
        "segment_start_times": [start_ms],
        "segment_end_times": [now_ms],
    }
    fake = _QlogClient([route])
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            route["fullname"],
            engaged=99.0,
            not_in_park=80.0,
            dongle_id=route["dongle_id"],
            start_time_utc_ms=start_ms,
            end_time_utc_ms=now_ms,
            maxqlog=1,
        )
        cache.set_watermark_ms(start_ms)
        cache.commit()

    stats = run_pipeline(settings, backfill=False)
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 1
    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(route["fullname"])
    assert row is not None
    assert row.maxqlog == 4
    assert row.engaged_time_s != 99.0
    assert row.not_in_park_time_s is not None


def test_nightly_reparse_does_not_wipe_unlisted_history(tmp_path, monkeypatch) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 3_600_000
    recent = {
        **ROUTE,
        "fullname": "deadbeefcafebabe|recent",
        "segment_start_times": [start_ms],
        "segment_end_times": [now_ms],
    }
    fake = _QlogClient([recent])
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    hist_name = "deadbeefcafebabe|historical"
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            recent["fullname"],
            engaged=1.0,
            not_in_park=2.0,
            dongle_id=recent["dongle_id"],
            start_time_utc_ms=start_ms,
            end_time_utc_ms=now_ms,
        )
        _seed(
            cache,
            hist_name,
            engaged=42.0,
            not_in_park=50.0,
            dongle_id=ROUTE["dongle_id"],
            start_time_utc_ms=1_000,
            end_time_utc_ms=2_000,
        )
        cache.set_watermark_ms(now_ms)
        cache.commit()

    stats = run_pipeline(settings, backfill=False, reparse_engaged=True)
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 1
    with Cache(settings.cache_path) as cache:
        listed = cache.get_drive(recent["fullname"])
        hist = cache.get_drive(hist_name)
    assert hist is not None and hist.qlog_parsed
    assert hist.engaged_time_s == 42.0
    assert hist.not_in_park_time_s == 50.0
    assert listed is not None and listed.qlog_parsed
    assert listed.engaged_time_s != 1.0


def test_nightly_reparses_friday_drive_when_maxqlog_grows(tmp_path, monkeypatch) -> None:
    """Window-respecting API: Friday start is outside watermark−24h; still refresh."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 48 * 3_600_000
    end_ms = start_ms + 3_600_000
    assert end_ms < now_ms - 24 * 3_600_000
    assert end_ms >= now_ms - LATE_UPLOAD_HOURS * 3_600_000
    route = {
        **ROUTE,
        "fullname": "deadbeefcafebabe|friday",
        "maxqlog": 6,
        "segment_start_times": [start_ms],
        "segment_end_times": [end_ms],
    }
    fake = _QlogClient([route])
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            route["fullname"],
            engaged=99.0,
            not_in_park=80.0,
            dongle_id=route["dongle_id"],
            start_time_utc_ms=start_ms,
            end_time_utc_ms=end_ms,
            maxqlog=1,
        )
        cache.set_watermark_ms(now_ms)
        cache.commit()

    stats = run_pipeline(settings, backfill=False)
    assert any(lo <= start_ms < hi for lo, hi in fake.windows), fake.windows
    assert all(lo > 86_400_000 for lo, _hi in fake.windows), fake.windows  # not year-1970/2018
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 1
    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(route["fullname"])
    assert row is not None
    assert row.maxqlog == 6
    assert row.engaged_time_s != 99.0


def test_nightly_does_not_list_from_ancient_unparsed(tmp_path, monkeypatch) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ancient_start = int(datetime(2018, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    fake = _QlogClient([])
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    stranded = "deadbeefcafebabe|stranded-2018"
    with Cache(settings.cache_path) as cache:
        cache.upsert_route_meta(
            drive_row(
                route_name=stranded,
                dongle_id=ROUTE["dongle_id"],
                start_time_utc_ms=ancient_start,
                end_time_utc_ms=ancient_start + 3_600_000,
                qlog_parsed=False,
                engaged_time_s=None,
            )
        )
        cache.set_watermark_ms(now_ms)
        cache.commit()

    stats = run_pipeline(settings, backfill=False)
    assert fake.windows
    assert min(lo for lo, _hi in fake.windows) >= now_ms - 25 * 3_600_000
    assert stats.qlogs_parsed == 1
    assert fake.qlog_calls == 1
    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(stranded)
    assert row is not None and row.qlog_parsed


def test_nightly_does_not_redownload_recent_end_when_maxqlog_unchanged(tmp_path, monkeypatch) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 3_600_000
    route = {
        **ROUTE,
        "fullname": "deadbeefcafebabe|settled",
        "maxqlog": 3,
        "segment_start_times": [start_ms],
        "segment_end_times": [now_ms - 60_000],
    }
    fake = _QlogClient([route])
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            route["fullname"],
            engaged=99.0,
            not_in_park=80.0,
            dongle_id=route["dongle_id"],
            start_time_utc_ms=start_ms,
            end_time_utc_ms=now_ms - 60_000,
            maxqlog=3,
        )
        cache.set_watermark_ms(now_ms)
        cache.commit()

    stats = run_pipeline(settings, backfill=False)
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 0
    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(route["fullname"])
    assert row is not None
    assert row.engaged_time_s == 99.0


def test_empty_qlog_parse_keeps_last_good_engaged(tmp_path, monkeypatch) -> None:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 3_600_000
    route = {
        **ROUTE,
        "fullname": "deadbeefcafebabe|empty-qlog",
        "maxqlog": 5,
        "segment_start_times": [start_ms],
        "segment_end_times": [now_ms],
    }
    fake = _QlogClient([route])
    fake.blob = b""
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            route["fullname"],
            engaged=99.0,
            not_in_park=80.0,
            dongle_id=route["dongle_id"],
            start_time_utc_ms=start_ms,
            end_time_utc_ms=now_ms,
            maxqlog=1,
        )
        cache.set_watermark_ms(start_ms)
        cache.commit()

    stats = run_pipeline(settings, backfill=False)
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 0
    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(route["fullname"])
    assert row is not None
    assert row.qlog_parsed is True
    assert row.engaged_time_s == 99.0
    assert row.not_in_park_time_s == 80.0


def test_metadata_only_does_not_blank_good_git_or_miles(tmp_path, monkeypatch) -> None:
    class _BlankClient(_FakeClient):
        def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict]:
            blank = {
                **ROUTE,
                "distance": 0.0,
                "length": 0.0,
                "git_commit": "",
                "git_branch": "",
                "git_remote": "",
            }
            self.windows.append((start_ms, end_ms))
            return [blank] if _in_window(blank, start_ms, end_ms) else []

    fake = _BlankClient()
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        _seed(
            cache,
            ROUTE["fullname"],
            engaged=12509.5,
            dongle_id=ROUTE["dongle_id"],
            length_miles=18.4,
            git_commit="goodcommit",
            git_branch="nightly",
            git_remote="git@github.com:commaai/openpilot.git",
        )
        cache.commit()

    run_pipeline(settings, backfill=True, metadata_only=True)
    with Cache(settings.cache_path) as cache:
        row = cache.get_drive(ROUTE["fullname"])
    assert row is not None
    assert row.length_miles == 18.4
    assert row.git_commit == "goodcommit"
    assert row.git_branch == "nightly"
    assert row.engaged_time_s == 12509.5
