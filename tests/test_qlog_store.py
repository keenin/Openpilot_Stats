from __future__ import annotations

from pathlib import Path

from helpers import drive_row, seed_local_qlogs
from op_usage.cache import Cache
from op_usage.cli import main
from op_usage.config import Settings, default_qlog_dir, is_live_qlog_dir, load_settings
from op_usage.qlog_store import (
    list_local_segments,
    load_route_qlogs,
    local_complete,
    qlog_segment_from_url,
    run_sync_qlogs,
    segment_path,
    split_route_name,
    urls_by_segment,
    write_qlog_atomic,
)

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path) -> Settings:
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
    )


def test_split_route_and_layout(tmp_path: Path) -> None:
    dongle, route_id = split_route_name("deadbeefcafebabe|2026-09-01--00-00-00")
    assert dongle == "deadbeefcafebabe"
    assert route_id == "2026-09-01--00-00-00"
    path = segment_path(tmp_path / "qlogs", dongle, route_id, 3)
    assert path == tmp_path / "qlogs" / dongle / route_id / "3.qlog"


def test_write_atomic_and_ignore_partial(tmp_path: Path) -> None:
    dest = tmp_path / "0.qlog"
    write_qlog_atomic(dest, b"hello")
    assert dest.read_bytes() == b"hello"
    assert not dest.with_name("0.qlog.partial").exists()
    leftover = tmp_path / "qlogs" / "d" / "r" / "1.qlog.partial"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"nope")
    (tmp_path / "qlogs" / "d" / "r" / "0.qlog").write_bytes(b"keep")
    assert list_local_segments(tmp_path / "qlogs", "d", "r") == [0]


def test_completeness_vs_maxqlog(tmp_path: Path) -> None:
    seed_local_qlogs(tmp_path, "d|r", b"abc", maxqlog=1, dongle_id="d")
    have = list_local_segments(tmp_path, "d", "r")
    assert have == [0, 1]
    assert local_complete(have, 1) is True
    assert local_complete(have, 2) is False
    assert local_complete(have, None) is False
    blobs = load_route_qlogs(tmp_path, route_name="d|r", dongle_id="d", maxqlog=1)
    assert blobs is not None and blobs[0] == b"abc"
    assert load_route_qlogs(tmp_path, route_name="d|r", dongle_id="d", maxqlog=2) is None
    assert load_route_qlogs(tmp_path, route_name="missing|x", dongle_id="missing", maxqlog=None) is None


def test_qlog_segment_from_url() -> None:
    azure = "https://commadataci.blob.core.windows.net/openpilotdata/d/2026-01-01--00-00-00/4/qlog.bz2?sv=1"
    cdn = "https://cdn.comma.ai/d/2026-01-01--00-00-00/12/qlog"
    zst = "https://example.invalid/path/7/qlog.zst"
    assert qlog_segment_from_url(azure) == 4
    assert qlog_segment_from_url(cdn) == 12
    assert qlog_segment_from_url(zst) == 7
    assert qlog_segment_from_url("https://example.invalid/qlog.bin") is None
    mapped = urls_by_segment(
        [
            "https://example.invalid/x/0/qlog.bz2",
            "https://example.invalid/plain",
        ]
    )
    assert mapped[0].endswith("0/qlog.bz2")
    assert mapped[1].endswith("plain")


class _SyncClient:
    def __init__(self, files: dict[str, list[str]], blobs: dict[str, bytes]) -> None:
        self.files = files
        self.blobs = blobs
        self.files_calls: list[str] = []
        self.downloads: list[str] = []

    def verify_auth(self) -> dict:
        return {}

    def route_qlog_urls(self, route_name: str) -> list[str]:
        self.files_calls.append(route_name)
        return list(self.files[route_name])

    def download_bytes(self, url: str) -> bytes:
        self.downloads.append(url)
        return self.blobs[url]


def test_sync_writes_layout_and_skips_existing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    name = "deadbeefcafebabe|2026-09-01--00-00-00"
    url0 = "https://cdn.example/deadbeefcafebabe/2026-09-01--00-00-00/0/qlog.bz2"
    url1 = "https://cdn.example/deadbeefcafebabe/2026-09-01--00-00-00/1/qlog.bz2"
    url2 = "https://cdn.example/deadbeefcafebabe/2026-09-01--00-00-00/2/qlog.bz2"
    already = segment_path(settings.qlog_dir, "deadbeefcafebabe", "2026-09-01--00-00-00", 0)
    write_qlog_atomic(already, b"already")
    with Cache(settings.cache_path) as cache:
        cache.upsert_route_meta(drive_row(route_name=name, maxqlog=2, qlog_parsed=False))
        cache.commit()
    client = _SyncClient(
        {name: [url0, url1, url2]},
        {url0: b"new0", url1: b"seg1", url2: b"seg2"},
    )
    stats = run_sync_qlogs(settings, client=client)
    assert stats.routes_checked == 1
    assert stats.routes_skipped_complete == 0
    assert stats.qlogs_downloaded == 2
    assert client.files_calls == [name]
    assert url0 not in client.downloads
    assert already.read_bytes() == b"already"
    assert (
        segment_path(settings.qlog_dir, "deadbeefcafebabe", "2026-09-01--00-00-00", 1).read_bytes()
        == b"seg1"
    )
    assert (
        segment_path(settings.qlog_dir, "deadbeefcafebabe", "2026-09-01--00-00-00", 2).read_bytes()
        == b"seg2"
    )

    stats2 = run_sync_qlogs(settings, client=client)
    assert stats2.routes_skipped_complete == 1
    assert stats2.qlogs_downloaded == 0
    assert client.files_calls == [name]


def test_sync_skips_complete_without_files_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    name = "deadbeefcafebabe|done"
    seed_local_qlogs(settings.qlog_dir, name, b"x", maxqlog=1, dongle_id="deadbeefcafebabe")
    with Cache(settings.cache_path) as cache:
        cache.upsert_route_meta(
            drive_row(route_name=name, dongle_id="deadbeefcafebabe", maxqlog=1, qlog_parsed=False)
        )
        cache.commit()
    client = _SyncClient({name: []}, {})
    stats = run_sync_qlogs(settings, client=client)
    assert stats.routes_skipped_complete == 1
    assert client.files_calls == []
    assert client.downloads == []


def test_nightly_script_lists_then_syncs_then_parses() -> None:
    text = (ROOT / "scripts" / "nightly.sh").read_text(encoding="utf-8")
    code = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    py = [line for line in code if "op_usage" in line]
    assert len(py) == 3
    assert "nightly --metadata-only" in py[0]
    assert "sync-qlogs" in py[1]
    assert "nightly" in py[2] and "metadata-only" not in py[2]
    assert code.index(py[0]) < code.index(py[1]) < code.index(py[2])


def test_sync_qlogs_cli_help(capsys) -> None:
    try:
        main(["sync-qlogs", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected help SystemExit")
    out = capsys.readouterr().out
    assert "missing qlogs" in out
    try:
        main(["download-qlogs", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    alias = capsys.readouterr().out
    assert "missing qlogs" in alias


def test_qlog_dir_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OP_USAGE_QLOG_DIR", str(tmp_path / "custom-qlogs"))
    monkeypatch.delenv("QLOG_DIR", raising=False)
    settings = load_settings()
    assert settings.qlog_dir == (tmp_path / "custom-qlogs").resolve()


def test_default_qlog_dir_is_cache_family() -> None:
    path = default_qlog_dir()
    assert path.name == "qlogs"
    assert path.parent.name == "op-usage"
    assert is_live_qlog_dir(path)
    assert not is_live_qlog_dir(Path("/tmp/op-usage-qlogs"))
