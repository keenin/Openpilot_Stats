from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from op_usage.cache import Cache
from op_usage.config import Settings
from op_usage.pipeline import run_pipeline
from op_usage.qlog import EnabledSample, GEAR_SOURCE, SELFDRIVE_SOURCE, encode_synthetic_qlog

from test_cache_and_api import _row

NS = 1_000_000_000

ROUTE = {
    "fullname": "deadbeefcafebabe|2026-09-01--00-00-00",
    "dongle_id": "deadbeefcafebabe",
    "distance": 12.5,
    "length": 0.0,
    "git_commit": "abcabcabc",
    "git_branch": "nightly",
    "git_remote": "git@github.com:commaai/openpilot.git",
    "maxqlog": 3,
    "segment_start_times": [1_725_000_000_000],
    "segment_end_times": [1_725_000_600_000],
}


class _FakeClient:
    def __init__(self) -> None:
        self.qlog_calls = 0

    def verify_auth(self) -> dict:
        return {}

    def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        return [ROUTE]

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
        owner_name="t",
        backfill_start="2026-09-01",
        openpilot_path=None,
        cereal_path=None,
        cf_pages_project="op-usage",
        chunk_days=30,
        recheck_hours=24,
        files_min_interval_s=13,
        request_timeout_s=60,
    )


def test_metadata_only_refreshes_length_without_qlogs(tmp_path, monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    monkeypatch.setattr(
        "op_usage.pipeline.load_event_module",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cereal must not load")),
    )
    settings = _settings(tmp_path)
    with Cache(settings.cache_path) as cache:
        cache.upsert_route_meta(
            _row(
                route_name=ROUTE["fullname"],
                dongle_id=ROUTE["dongle_id"],
                length_miles=0.0,
                qlog_parsed=False,
                engaged_time_s=None,
            )
        )
        cache.save_engaged(ROUTE["fullname"], 12509.5, "selfdriveState.enabled")
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


def test_metadata_only_rejects_reparse() -> None:
    settings = Settings(
        comma_jwt="jwt",
        dongle_id="d",
        api_base="https://example.invalid",
        cache_path=Path("/tmp/unused.sqlite"),
        site_dir=Path("/tmp/unused-site"),
        display_tz="UTC",
        owner_name="t",
        backfill_start="2026-09-01",
        openpilot_path=None,
        cereal_path=None,
        cf_pages_project="op-usage",
        chunk_days=30,
        recheck_hours=24,
        files_min_interval_s=13,
        request_timeout_s=60,
    )
    try:
        run_pipeline(settings, backfill=True, metadata_only=True, reparse_engaged=True)
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
        self.blob = _qlog_blob()

    def verify_auth(self) -> dict:
        return {}

    def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict]:
        return list(self.routes)

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
        cache.upsert_route_meta(
            _row(
                route_name=route["fullname"],
                dongle_id=route["dongle_id"],
                start_time_utc_ms=start_ms,
                end_time_utc_ms=now_ms,
                maxqlog=1,
                qlog_parsed=False,
                engaged_time_s=None,
            )
        )
        cache.save_engaged(route["fullname"], 99.0, "selfdriveState.enabled", 80.0)
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
    fake = _QlogClient([ROUTE])
    monkeypatch.setattr("op_usage.pipeline.CommaClient", lambda **kwargs: fake)
    settings = _settings(tmp_path)
    hist_name = "deadbeefcafebabe|historical"
    with Cache(settings.cache_path) as cache:
        cache.upsert_route_meta(
            _row(
                route_name=ROUTE["fullname"],
                dongle_id=ROUTE["dongle_id"],
                qlog_parsed=False,
                engaged_time_s=None,
            )
        )
        cache.save_engaged(ROUTE["fullname"], 1.0, "selfdriveState.enabled", 2.0)
        cache.upsert_route_meta(
            _row(
                route_name=hist_name,
                dongle_id=ROUTE["dongle_id"],
                start_time_utc_ms=1_000,
                qlog_parsed=False,
                engaged_time_s=None,
            )
        )
        cache.save_engaged(hist_name, 42.0, "selfdriveState.enabled", 50.0)
        cache.set_watermark_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
        cache.commit()

    stats = run_pipeline(settings, backfill=False, reparse_engaged=True)
    assert fake.qlog_calls == 1
    assert stats.qlogs_parsed == 1
    with Cache(settings.cache_path) as cache:
        listed = cache.get_drive(ROUTE["fullname"])
        hist = cache.get_drive(hist_name)
    assert hist is not None and hist.qlog_parsed
    assert hist.engaged_time_s == 42.0
    assert hist.not_in_park_time_s == 50.0
    assert listed is not None and listed.qlog_parsed
    assert listed.engaged_time_s != 1.0
