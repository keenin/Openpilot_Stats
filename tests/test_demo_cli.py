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


def test_deploy_dry_run(tmp_path, capsys) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    rc = main(["deploy", "--dry-run", "--out", str(site)])
    assert rc == 0
    assert "wrangler pages deploy" in capsys.readouterr().out
