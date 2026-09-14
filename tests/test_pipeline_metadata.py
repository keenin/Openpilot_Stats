from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from helpers import drive_row, seed_local_qlogs, seed_parsed
from op_usage.cache import Cache
from op_usage.config import Settings
from op_usage.pipeline import LATE_UPLOAD_HOURS, _coalesce_start_windows, run_pipeline
from op_usage.qlog import (
    EnabledSample,
    GEAR_SOURCE,
    PARSER_VERSION,
    SELFDRIVE_SOURCE,
    encode_synthetic_qlog,
)
from op_usage.steady import MS_TO_MPH

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


def _settings(tmp_path: Path, parse_jobs: int = 1) -> Settings:
    return Settings(
        comma_jwt="jwt",
        dongle_id="deadbeefcafebabe",
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
        parse_jobs=parse_jobs,
    )


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _route(**kwargs) -> dict:
    return {**ROUTE, **kwargs}


class _QlogClient:
    """Listing stub. Parse must never call /files or CDN — those raise."""

    def __init__(self, routes: list[dict]) -> None:
        self.routes = routes
        self.qlog_calls = 0
        self.windows: list[tuple[int, int]] = []
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
        raise AssertionError("parse/backfill must not call route_qlog_urls")

    def download_bytes(self, url: str) -> bytes:
        self.qlog_calls += 1
        raise AssertionError("parse/backfill must not call download_bytes")


def _seed_client_routes(settings: Settings, fake: _QlogClient, blob: bytes | None = None) -> None:
    data = fake.blob if blob is None else blob
    for route in fake.routes:
        seed_local_qlogs(
            settings.qlog_dir,
            route["fullname"],
            data,
            maxqlog=int(route.get("maxqlog") or 0),
            dongle_id=str(route.get("dongle_id") or ""),
        )


def _run(tmp_path, monkeypatch, fake, seeds, watermark, *, seed_qlogs: bool = False, parse_jobs: int = 1, **flags):
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path, parse_jobs=parse_jobs)
    with Cache(settings.cache_path) as cache:
        for seed in seeds:
            seed(cache)
        if watermark is not None:
            cache.set_watermark_ms(watermark)
        cache.commit()
    if seed_qlogs:
        _seed_client_routes(settings, fake)
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
    fake = _QlogClient([ROUTE])
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
        seed_qlogs=True,
    )
    assert fake.qlog_calls == 0
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
        seed_qlogs=True,
    )
    assert any(lo <= start_ms < hi for lo, hi in fake.windows), fake.windows
    assert all(lo > 86_400_000 for lo, _hi in fake.windows), fake.windows
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 1
    assert stats.qlogs_missing_local == 0
    row = _get(settings, route["fullname"])
    assert row is not None
    assert row.maxqlog == 6
    assert row.engaged_time_s != 99.0


def test_nightly_does_not_list_from_ancient_unparsed(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    ancient_start = int(datetime(2018, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    stranded = "deadbeefcafebabe|stranded-2018"
    fake = _QlogClient([])
    settings = _settings(tmp_path)
    seed_local_qlogs(
        settings.qlog_dir,
        stranded,
        fake.blob,
        maxqlog=1,
        dongle_id=ROUTE["dongle_id"],
    )
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
    assert fake.qlog_calls == 0
    assert stats.qlogs_missing_local == 0
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
    fake.blob = encode_synthetic_qlog([], compress="bz2")
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
        seed_qlogs=True,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 0
    assert stats.qlogs_missing_local == 0
    row = _get(settings, route["fullname"])
    assert row is not None
    assert row.qlog_parsed is True
    assert row.engaged_time_s == 99.0
    assert row.not_in_park_time_s == 80.0


def test_parse_missing_local_does_not_call_download(tmp_path, monkeypatch) -> None:
    fake = _QlogClient([ROUTE])
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [],
        None,
        backfill=True,
        seed_qlogs=False,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 0
    assert stats.qlogs_missing_local >= 1
    row = _get(settings, ROUTE["fullname"])
    assert row is not None
    assert row.qlog_parsed is False
    assert row.engaged_time_s is None


def test_parse_uses_local_files_not_client(tmp_path, monkeypatch) -> None:
    fake = _QlogClient([ROUTE])
    monkeypatch.setattr(
        "op_usage.pipeline.ProcessPoolExecutor",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("jobs=1 must not use a process pool")),
    )
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [],
        None,
        backfill=True,
        seed_qlogs=True,
        parse_jobs=1,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 1
    assert stats.qlogs_missing_local == 0
    row = _get(settings, ROUTE["fullname"])
    assert row is not None and row.qlog_parsed
    assert row.engaged_time_s is not None and row.engaged_time_s > 0


def test_incomplete_local_skips_and_retries_after_fill(tmp_path, monkeypatch) -> None:
    now_ms = _now()
    start_ms = now_ms - 3_600_000
    route = _route(
        fullname="deadbeefcafebabe|partial",
        maxqlog=3,
        segment_start_times=[start_ms],
        segment_end_times=[now_ms],
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
                end_time_utc_ms=now_ms,
                maxqlog=0,
            )
        ],
        start_ms,
        backfill=False,
    )
    seed_local_qlogs(
        settings.qlog_dir,
        route["fullname"],
        fake.blob,
        maxqlog=0,
        dongle_id=route["dongle_id"],
    )
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [],
        start_ms,
        backfill=False,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_missing_local >= 1
    assert stats.qlogs_parsed == 0
    row = _get(settings, route["fullname"])
    assert row is not None
    assert row.maxqlog == 3
    assert row.qlog_parsed is False
    assert row.engaged_time_s == 99.0

    seed_local_qlogs(
        settings.qlog_dir,
        route["fullname"],
        fake.blob,
        maxqlog=3,
        dongle_id=route["dongle_id"],
    )
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [],
        start_ms,
        backfill=False,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 1
    assert stats.qlogs_missing_local == 0
    row = _get(settings, route["fullname"])
    assert row is not None and row.qlog_parsed
    assert row.engaged_time_s != 99.0


def _mph_blob(duration_s: float, mph: float, dt: float = 1.0) -> bytes:
    v_ms = mph / MS_TO_MPH
    samples: list[EnabledSample] = []
    t = 0.0
    while t <= duration_s + 1e-9:
        ns = int(round(t * 1_000_000_000))
        samples.append(EnabledSample(ns, True, SELFDRIVE_SOURCE))
        samples.append(
            EnabledSample(ns, True, GEAR_SOURCE, v_ego_ms=v_ms, cruise_speed_ms=v_ms)
        )
        t += dt
    return encode_synthetic_qlog(samples, compress=None)


def _engaged_fields(row):
    return (
        row.engaged_time_s,
        row.weighted_engaged_time_s,
        row.not_in_park_time_s,
        row.steady_frac,
        row.parser_version,
        row.engaged_source,
        row.qlog_parsed,
    )


def test_parse_jobs_matches_serial_engaged_and_weighted(tmp_path, monkeypatch) -> None:
    plain = encode_synthetic_qlog(
        [
            EnabledSample(0, True, SELFDRIVE_SOURCE),
            EnabledSample(2 * NS, True, SELFDRIVE_SOURCE),
            EnabledSample(0, True, GEAR_SOURCE),
            EnabledSample(2 * NS, True, GEAR_SOURCE),
        ],
        compress=None,
    )
    speed = _mph_blob(10.0, 70.0)
    routes = [
        _route(fullname="deadbeefcafebabe|plain-a", maxqlog=0),
        _route(fullname="deadbeefcafebabe|weighted-b", maxqlog=1),
        _route(fullname="deadbeefcafebabe|plain-c", maxqlog=2),
    ]
    blobs = {
        routes[0]["fullname"]: plain,
        routes[1]["fullname"]: speed,
        routes[2]["fullname"]: plain,
    }

    def _once(subdir: str, jobs: int):
        fake = _QlogClient(routes)
        root = tmp_path / subdir
        root.mkdir()
        settings = _settings(root, parse_jobs=jobs)
        monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
        for route in routes:
            seed_local_qlogs(
                settings.qlog_dir,
                route["fullname"],
                blobs[route["fullname"]],
                maxqlog=int(route["maxqlog"]),
                dongle_id=str(route["dongle_id"]),
            )
        stats = run_pipeline(settings, backfill=True)
        return stats, settings, fake

    serial_stats, serial_settings, serial_fake = _once("serial", 1)
    pool_stats, pool_settings, pool_fake = _once("pool", 2)
    assert serial_fake.qlog_calls == 0
    assert pool_fake.qlog_calls == 0
    assert serial_stats.qlogs_parsed == pool_stats.qlogs_parsed == 3
    assert serial_stats.qlogs_missing_local == pool_stats.qlogs_missing_local == 0
    for route in routes:
        name = route["fullname"]
        a, b = _get(serial_settings, name), _get(pool_settings, name)
        assert a is not None and b is not None
        assert _engaged_fields(a) == _engaged_fields(b)
        assert a.parser_version == PARSER_VERSION
        assert a.engaged_time_s is not None and a.engaged_time_s > 0
    weighted = _get(serial_settings, routes[1]["fullname"])
    assert weighted is not None
    assert weighted.weighted_engaged_time_s is not None
    assert weighted.weighted_engaged_time_s > 0


def test_parse_jobs_missing_local_does_not_call_download(tmp_path, monkeypatch) -> None:
    extra = _route(fullname="deadbeefcafebabe|also-missing", maxqlog=1)
    fake = _QlogClient([ROUTE, extra])
    stats, settings = _run(
        tmp_path,
        monkeypatch,
        fake,
        [],
        None,
        backfill=True,
        seed_qlogs=False,
        parse_jobs=2,
    )
    assert fake.qlog_calls == 0
    assert stats.qlogs_parsed == 0
    assert stats.qlogs_missing_local == 2
    for name in (ROUTE["fullname"], extra["fullname"]):
        row = _get(settings, name)
        assert row is not None
        assert row.qlog_parsed is False
        assert row.engaged_time_s is None


