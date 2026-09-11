from __future__ import annotations

from pathlib import Path

from op_usage.cache import Cache
from op_usage.config import Settings
from op_usage.pipeline import run_pipeline

from test_cache_and_api import _row

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
