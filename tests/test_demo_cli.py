from __future__ import annotations

from pathlib import Path

from op_usage.cache import Cache
from op_usage.cli import main
from op_usage.config import default_cache_path, default_site_dir

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drives.json"


def test_demo_cli_refuses_live_paths(tmp_path) -> None:
    try:
        main(["demo", "--fixture", str(FIXTURE), "--out", str(tmp_path / "site"), "--cache", str(default_cache_path())])
    except SystemExit as exc:
        assert "live cache" in str(exc)
        assert not default_cache_path().is_file()
    else:
        raise AssertionError("expected SystemExit")
    try:
        main(["demo", "--fixture", str(FIXTURE), "--out", str(default_site_dir()), "--cache", str(tmp_path / "demo.sqlite")])
    except SystemExit as exc:
        assert "live site" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
    assert not (default_site_dir() / "index.html").is_file()
    existed = default_cache_path().is_file()
    assert main(["demo", "--fixture", str(FIXTURE)]) == 0
    if not existed:
        assert not default_cache_path().is_file()


def test_demo_cli_writes_index(tmp_path) -> None:
    out = tmp_path / "site"
    rc = main(["demo", "--fixture", str(FIXTURE), "--out", str(out), "--cache", str(tmp_path / "demo.sqlite")])
    assert rc == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "nightly-togo" in html
    assert "wip-two-drives" not in html
    assert "Updated " in html


def test_verbose_works_before_or_after_subcommand(tmp_path) -> None:
    cache = tmp_path / "c.sqlite"
    with Cache(cache) as db:
        db.commit()
    assert main(["generate", "-v", "--cache", str(cache), "--out", str(tmp_path / "site")]) == 0
    assert main(["-v", "generate", "--cache", str(cache), "--out", str(tmp_path / "site2")]) == 0


def test_metadata_only_conflicts_with_reparse(capsys) -> None:
    try:
        main(["backfill", "--metadata-only", "--reparse-engaged"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse conflict")
    err = capsys.readouterr().err
    assert "--metadata-only cannot be combined with --reparse-engaged" in err
    try:
        main(["backfill", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--reparse-engaged" in out
    assert "--metadata-only" in out


def test_deploy_dry_run(tmp_path, capsys) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    assert main(["deploy", "--dry-run", "--out", str(site)]) == 0
    assert "wrangler pages deploy" in capsys.readouterr().out
