from __future__ import annotations

from pathlib import Path

from op_usage.cli import main

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "drives.json"


def test_demo_cli_writes_index(tmp_path) -> None:
    out = tmp_path / "site"
    cache = tmp_path / "demo.sqlite"
    rc = main(["demo", "--fixture", str(FIXTURE), "--out", str(out), "--cache", str(cache)])
    assert rc == 0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "nightly-togo" in html
    assert "wip-two-drives" not in html
    assert "<h1" not in html
    assert "Demo data" not in html
    assert "Updated " in html
    assert "Engage %" in html


def test_verbose_works_before_or_after_subcommand(tmp_path) -> None:
    out = tmp_path / "site"
    cache = tmp_path / "c.sqlite"
    from op_usage.cache import Cache

    with Cache(cache) as db:
        db.commit()
    assert main(["generate", "-v", "--cache", str(cache), "--out", str(out)]) == 0
    assert (out / "index.html").is_file()
    out2 = tmp_path / "site2"
    assert main(["-v", "generate", "--cache", str(cache), "--out", str(out2)]) == 0
    assert (out2 / "index.html").is_file()


def test_reparse_engaged_flag_is_documented(capsys) -> None:
    try:
        main(["backfill", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert "--reparse-engaged" in out
    assert "not_in_park_time_s" in out
    assert "--metadata-only" in out


def test_metadata_only_conflicts_with_reparse(capsys) -> None:
    try:
        main(["backfill", "--metadata-only", "--reparse-engaged"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse conflict")
    assert "--metadata-only cannot be combined with --reparse-engaged" in capsys.readouterr().err


def test_deploy_dry_run(tmp_path, capsys) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    rc = main(["deploy", "--dry-run", "--out", str(site)])
    assert rc == 0
    assert "wrangler pages deploy" in capsys.readouterr().out
