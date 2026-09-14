"""CLI: demo | backfill | nightly | generate | deploy."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from op_usage.cache import Cache
from op_usage.config import load_settings
from op_usage.pipeline import generate_from_cache, run_demo, run_pipeline


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    raw, verbose = _strip_verbose(raw)
    parser = argparse.ArgumentParser(
        prog="op-usage",
        description=(
            "Personal openpilot usage site (private pipeline → static HTML). "
            "-v/--verbose works before or after the subcommand."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--out", type=Path, default=None, help="SITE_DIR override")
    shared.add_argument("--cache", type=Path, default=None, help="sqlite override")
    sub = parser.add_subparsers(dest="cmd", required=True)

    reparse = argparse.ArgumentParser(add_help=False)
    reparse.add_argument(
        "--reparse-engaged",
        action="store_true",
        help=(
            "Clear cached qlog parses for routes this run will list "
            "(engaged_time_s, not_in_park_time_s, weighted_engaged_time_s, "
            "including zeros) and re-read those qlogs. Does not wipe routes "
            "outside the fetch window. After an interrupted run, resume with "
            "plain backfill/nightly. Needed once after the weighted-time schema bump."
        ),
    )
    reparse.add_argument(
        "--metadata-only",
        action="store_true",
        help=(
            "Re-list route metadata (length_miles, times, git_*) from the comma API "
            "and skip every qlog download. Use after the distance-field fix to refresh "
            "cached miles without --reparse-engaged."
        ),
    )

    demo = sub.add_parser("demo", parents=[shared], help="Generate site from fixture JSON (no JWT).")
    demo.add_argument(
        "--fixture",
        type=Path,
        default=_default_fixture(),
        help="JSON list of drives",
    )
    demo.epilog = (
        "Omitting --cache/--out writes to temp paths. Demo refuses the live "
        "default cache (~/.cache/op-usage/op-usage.sqlite) and ./site."
    )

    sub.add_parser(
        "backfill",
        parents=[shared, reparse],
        help="Full historical fetch once, then write HTML.",
    )
    sub.add_parser(
        "nightly",
        parents=[shared, reparse],
        help="Incremental fetch (watermark + last-day recheck) + HTML.",
    )
    sub.add_parser("generate", parents=[shared], help="Rebuild index.html from the local cache only.")
    dep = sub.add_parser("deploy", parents=[shared], help="wrangler pages deploy ./site")
    dep.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(raw)
    if getattr(args, "metadata_only", False) and getattr(args, "reparse_engaged", False):
        parser.error("--metadata-only cannot be combined with --reparse-engaged")
    logging.basicConfig(
        level=logging.DEBUG if (verbose or args.verbose) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = load_settings()
    settings = _apply_paths(settings, args.out, args.cache, demo=args.cmd == "demo")

    if args.cmd == "demo":
        stats = run_demo(settings, args.fixture)
        print(f"demo site → {stats.html_path}")
        return 0
    if args.cmd == "generate":
        with Cache(settings.cache_path) as cache:
            path = generate_from_cache(settings, cache)
        print(f"wrote {path}")
        return 0
    if args.cmd in ("backfill", "nightly"):
        stats = run_pipeline(
            settings,
            backfill=args.cmd == "backfill",
            reparse_engaged=args.reparse_engaged,
            metadata_only=args.metadata_only,
        )
        extra = " metadata_only" if args.metadata_only else ""
        skip = f" cached_skip={stats.qlogs_skipped_cached}" if args.cmd == "nightly" else ""
        print(
            f"{args.cmd} listed={stats.routes_listed} parsed={stats.qlogs_parsed}"
            f"{skip}{extra} → {stats.html_path}"
        )
        return 0
    return _deploy(settings, dry_run=args.dry_run)


def _strip_verbose(argv: list[str]) -> tuple[list[str], bool]:
    """Allow `op-usage -v backfill` and `op-usage backfill -v` (argparse only does one)."""
    verbose = False
    kept: list[str] = []
    for arg in argv:
        if arg in ("-v", "--verbose"):
            verbose = True
        else:
            kept.append(arg)
    return kept, verbose


def _apply_paths(settings, out: Path | None, cache: Path | None, *, demo: bool = False):
    """Apply --out/--cache; demo fills omitted paths with temps."""
    updates = {}
    if cache is not None:
        updates["cache_path"] = cache.resolve()
    elif demo:
        updates["cache_path"] = Path(tempfile.mkdtemp(prefix="op-usage-demo-")) / "demo.sqlite"
    if out is not None:
        updates["site_dir"] = out.resolve()
    elif demo:
        updates["site_dir"] = Path(tempfile.mkdtemp(prefix="op-usage-demo-site-"))
    return replace(settings, **updates) if updates else settings


def _default_fixture() -> Path:
    here = Path(__file__).resolve().parents[2] / "fixtures" / "drives.json"
    if here.is_file():
        return here
    return Path.cwd() / "fixtures" / "drives.json"


def _deploy(settings, dry_run: bool) -> int:
    site = settings.site_dir
    index = site / "index.html"
    if not index.is_file():
        print(f"missing {index} — run: python -m op_usage generate (or demo)", file=sys.stderr)
        return 1
    project = os.environ.get("CF_PAGES_PROJECT", settings.cf_pages_project)
    cmd = ["npx", "--yes", "wrangler", "pages", "deploy", str(site), "--project-name", project]
    print(" ".join(cmd))
    if dry_run:
        return 0
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
