from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from helpers import drive_row, seed_parsed
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


def _in_window(route: dict, start_ms: int, end_ms: int) -> bool:
    starts = route.get("segment_start_times") or [0]
    start = int(min(starts))
    return start_ms <= start < end_ms


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


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _route(**kwargs) -> dict:
    return {**ROUTE, **kwargs}


class _QlogClient:
    def __init__(self, routes: list[dict], *, refuse_qlogs: bool = False) -> None:
        self.routes = routes
        self.qlog_calls = 0
        self.windows: list[tuple[int, int]] = []
        self.refuse_qlogs = refuse_qlogs
        self.blob = encode_synthetic_qlog(
            [
                EnabledSample(0, True, SELFDRIVE_SOURCE),
                EnabledSample(2 * NS, True, SELFDRIVE_SOURCE),
                EnabledSample(0, True, GEAR_SOURCE),
                EnabledSample(2 * NS, True, GEAR_SOURCE),
            ],
            compress=None,
        )

    def verify_auth(self) -> dict:
        return {}

    def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        self.windows.append((start_ms, end_ms))
        return [route for route in self.routes if _in_window(route, start_ms, end_ms)]

    def route_qlog_urls(self, route_name: str) -> list[str]:
        self.qlog_calls += 1
        if self.refuse_qlogs:
            raise AssertionError("qlogs must not be fetched in metadata-only mode")
        return [f"https://example.invalid/{route_name}.qlog"]

    def download_bytes(self, url: str) -> bytes:
        if self.refuse_qlogs:
            self.qlog_calls += 1
            raise AssertionError("qlogs must not be fetched in metadata-only mode")
        return self.blob


def _run(tmp_path, monkeypatch, fake, seeds, watermark, **flags):
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        for seed in seeds:
            seed(cache)
        if watermark is not None:
            cache.set_watermark_ms(watermark)
        cache.commit()
    return run_pipeline(settings, **flags), settings


def _get(settings: Settings, name: str):
    with Cache(settings.cache_path) as cache:
        return cache.get_drive(name)


def test_coalesce_start_windows_merges_nearby_only() -> None:
    assert _coalesce_start_windows([]) == []
    assert _coalesce_start_windows([1_000], pad_ms=100) == [(1_000, 1_100)]
    assert _coalesce_start_windows([1_000, 1_050], pad_ms=100) == [(1_000, 1_150)]
    assert _coalesce_start_windows([1_000, 5_000], pad_ms=100) == [(1_000, 1_100), (5_000, 5_100)]


def test_metadata_only_upserts_without_qlogs_or_blanking(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "op_usage.pipeline.load_event_module",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cereal must not load")),
    )
    fake = _QlogClient([ROUTE], refuse_qlogs=True)
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [
            lambda c: seed_parsed(
                c,
                ROUTE["fullname"],
                engaged=12509.5,
                dongle_id=ROUTE["dongle_id"],
                length_miles=0.0,
                git_commit="goodcommit",
                git_branch="nightly",
                git_remote=ROUTE["git_remote"],
            )
        ],
        None,
        backfill=True,
        metadata_only=True,
    )
    assert stats.routes_listed >= 1
    assert stats.qlogs_parsed == 0
    assert fake.qlog_calls == 0
    row = _get(settings, ROUTE["fullname"])
    assert row is not None
    assert row.length_miles == 12.5
    assert row.engaged_time_s == 12509.5
    assert row.qlog_parsed is True
    assert row.engaged_source == "selfdriveState.enabled"
    assert row.git_remote == ROUTE["git_remote"]
    assert (settings.site_dir / "index.html").is_file()

    blank = {
        **ROUTE,
        "distance": 0.0,
        "length": 0.0,
        "git_commit": "",
        "git_branch": "",
        "git_remote": "",
    }
    fake.routes = [blank]
    run_pipeline(settings, backfill=True, metadata_only=True)
    row = _get(settings, ROUTE["fullname"])
    assert row is not None
    assert row.length_miles == 12.5
    assert row.git_commit == "abcabcabc"
    assert row.git_branch == "nightly"
    assert row.git_remote == ROUTE["git_remote"]
    assert row.engaged_time_s == 12509.5


def test_metadata_only_rejects_reparse(tmp_path) -> None:
    try:
        run_pipeline(_settings(tmp_path), backfill=True, metadata_only=True, reparse_engaged=True)
    except SystemExit as exc:
        assert "--metadata-only cannot be combined with --reparse-engaged" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_nightly_reparse_does_not_wipe_unlisted_history(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    start_ms = now_ms - 3_600_000
    recent = _route(fullname="deadbeefcafebabe|recent", segment_start_times=[start_ms], segment_end_times=[now_ms])
    hist = "deadbeefcafebabe|historical"
    fake = _QlogClient([recent])
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [
            lambda c: seed_parsed(
                c,
                recent["fullname"],
                engaged=1.0,
                not_in_park=2.0,
                dongle_id=recent["dongle_id"],
                start_time_utc_ms=start_ms,
                end_time_utc_ms=now_ms,
            ),
            lambda c: seed_parsed(
                c,
                hist,
                engaged=42.0,
                not_in_park=50.0,
                dongle_id=ROUTE["dongle_id"],
                start_time_utc_ms=1_000,
                end_time_utc_ms=2_000,
            ),
        ],
        now_ms,
        backfill=False,
        reparse_engaged=True,
    )
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 1
    listed, history = _get(settings, recent["fullname"]), _get(settings, hist)
    assert history is not None and history.qlog_parsed
    assert history.engaged_time_s == 42.0
    assert history.not_in_park_time_s == 50.0
    assert listed is not None and listed.qlog_parsed
    assert listed.engaged_time_s != 1.0


def test_nightly_reparses_friday_drive_when_maxqlog_grows(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    start_ms = now_ms - 48 * 3_600_000
    end_ms = start_ms + 3_600_000
    assert end_ms < now_ms - 24 * 3_600_000
    assert end_ms >= now_ms - LATE_UPLOAD_HOURS * 3_600_000
    route = _route(
        fullname="deadbeefcafebabe|friday",
        maxqlog=6,
        segment_start_times=[start_ms],
        segment_end_times=[end_ms],
    )
    fake = _QlogClient([route])
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [
            lambda c: seed_parsed(
                c,
                route["fullname"],
                engaged=99.0,
                not_in_park=80.0,
                dongle_id=route["dongle_id"],
                start_time_utc_ms=start_ms,
                end_time_utc_ms=end_ms,
                maxqlog=1,
            )
        ],
        now_ms,
        backfill=False,
    )
    assert any(lo <= start_ms < hi for lo, hi in fake.windows), fake.windows
    assert all(lo > 86_400_000 for lo, _hi in fake.windows), fake.windows
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 1
    row = _get(settings, route["fullname"])
    assert row is not None
    assert row.maxqlog == 6
    assert row.engaged_time_s != 99.0


def test_nightly_does_not_list_from_ancient_unparsed(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    ancient_start = int(datetime(2018, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    stranded = "deadbeefcafebabe|stranded-2018"
    fake = _QlogClient([])
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [
            lambda c: c.upsert_route_meta(
                drive_row(
                    route_name=stranded,
                    dongle_id=ROUTE["dongle_id"],
                    start_time_utc_ms=ancient_start,
                    end_time_utc_ms=ancient_start + 3_600_000,
                    qlog_parsed=False,
                    engaged_time_s=None,
                )
            )
        ],
        now_ms,
        backfill=False,
    )
    assert fake.windows
    assert min(lo for lo, _hi in fake.windows) >= now_ms - 25 * 3_600_000
    assert stats.qlogs_parsed == 1
    assert fake.qlog_calls == 1
    row = _get(settings, stranded)
    assert row is not None and row.qlog_parsed


def test_nightly_does_not_redownload_recent_end_when_maxqlog_unchanged(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    start_ms = now_ms - 3_600_000
    route = _route(
        fullname="deadbeefcafebabe|settled",
        maxqlog=3,
        segment_start_times=[start_ms],
        segment_end_times=[now_ms - 60_000],
    )
    fake = _QlogClient([route])
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [
            lambda c: seed_parsed(
                c,
                route["fullname"],
                engaged=99.0,
                not_in_park=80.0,
                dongle_id=route["dongle_id"],
                start_time_utc_ms=start_ms,
                end_time_utc_ms=now_ms - 60_000,
                maxqlog=3,
            )
        ],
        now_ms,
        backfill=False,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 0
    row = _get(settings, route["fullname"])
    assert row is not None
    assert row.engaged_time_s == 99.0


def test_empty_qlog_parse_keeps_last_good_engaged(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    start_ms = now_ms - 3_600_000
    route = _route(
        fullname="deadbeefcafebabe|empty-qlog",
        maxqlog=5,
        segment_start_times=[start_ms],
        segment_end_times=[now_ms],
    )
    fake = _QlogClient([route])
    fake.blob = b""
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [
            lambda c: seed_parsed(
                c,
                route["fullname"],
                engaged=99.0,
                not_in_park=80.0,
                dongle_id=route["dongle_id"],
                start_time_utc_ms=start_ms,
                end_time_utc_ms=now_ms,
                maxqlog=1,
            )
        ],
        start_ms,
        backfill=False,
    )
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 0
    row = _get(settings, route["fullname"])
    assert row is not None
    assert row.qlog_parsed is True
    assert row.engaged_time_s == 99.0
    assert row.not_in_park_time_s == 80.0
