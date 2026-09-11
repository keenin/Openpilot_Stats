"""Fetch → cache engaged time → write index.html.

Incremental: watermark + 24h recheck. First run backfills from BACKFILL_START.
Demo/dry-run uses fixtures and never talks to comma.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from op_usage.aggregate import aggregate_commits
from op_usage.cache import SCHEMA_VERSION, Cache, DriveRow
from op_usage.comma_api import CommaClient, RouteMeta, iter_time_chunks, normalize_routes
from op_usage.config import Settings
from op_usage.qlog import extract_engaged_time_from_qlogs, load_event_module
from op_usage.site import render_site, write_site

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    routes_listed: int = 0
    qlogs_parsed: int = 0
    qlogs_skipped_cached: int = 0
    html_path: str = ""


def generate_from_cache(settings: Settings, cache: Cache, mode: str = "live") -> Path:
    commits = aggregate_commits(list(cache.iter_drives()))
    html = render_site(
        commits,
        owner_name=settings.owner_name,
        generated_at=datetime.now(timezone.utc),
        display_tz=settings.display_tz,
        mode=mode,
    )
    path = write_site(settings.site_dir, html)
    log.info("wrote %s (%d commits)", path, len(commits))
    return path


def load_fixture_drives(path: Path) -> list[DriveRow]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    drives: list[DriveRow] = []
    for item in raw:
        start = _ts_ms(item, "start_time_utc_ms", "start")
        end = _ts_ms(item, "end_time_utc_ms", "end")
        drives.append(
            DriveRow(
                route_name=item["route_name"],
                dongle_id=item.get("dongle_id", "demo"),
                start_time_utc_ms=start,
                end_time_utc_ms=end,
                length_miles=float(item["length_miles"]),
                total_drive_time_s=float(item.get("total_drive_time_s") or (end - start) / 1000.0),
                git_commit=item["git_commit"],
                git_branch=item.get("git_branch", ""),
                git_remote=item.get("git_remote", ""),
                maxqlog=item.get("maxqlog"),
                engaged_time_s=float(item["engaged_time_s"]),
                engaged_source=item.get("engaged_source", "selfdriveState.enabled"),
                qlog_parsed=True,
            )
        )
    return drives


def run_demo(settings: Settings, fixture_path: Path) -> RunStats:
    settings.site_dir.mkdir(parents=True, exist_ok=True)
    cache_path = settings.cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with Cache(cache_path) as cache:
        cache.replace_all(load_fixture_drives(fixture_path))
        html_path = generate_from_cache(settings, cache, mode="demo")
    return RunStats(html_path=str(html_path), routes_listed=0)


def run_pipeline(
    settings: Settings,
    *,
    backfill: bool,
    reparse_engaged: bool = False,
    metadata_only: bool = False,
) -> RunStats:
    if not settings.has_auth:
        raise SystemExit(
            "COMMA_JWT and DONGLE_ID are required. Put them in "
            "~/.config/op-usage/credentials.env (see .env.example)."
        )
    if metadata_only and reparse_engaged:
        raise SystemExit("--metadata-only cannot be combined with --reparse-engaged")
    stats = RunStats()
    client = CommaClient(
        jwt=settings.comma_jwt or "",
        dongle_id=settings.dongle_id or "",
        api_base=settings.api_base,
        timeout_s=settings.request_timeout_s,
        files_min_interval_s=settings.files_min_interval_s,
    )
    log.info("verifying JWT via GET /v1/me")
    client.verify_auth()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    recheck_ms = now_ms - settings.recheck_hours * 3600 * 1000
    event_mod = None if metadata_only else load_event_module(settings.openpilot_path, settings.cereal_path)

    with Cache(settings.cache_path) as cache:
        cache.set_meta("dongle_id", settings.dongle_id or "")
        if cache.schema_upgraded_from:
            log.warning(
                "cache schema_version %s → %s; cached engaged_time_s=0 rows will not "
                "reparse unless you pass --reparse-engaged (or SQL-clear qlog_parsed)",
                cache.schema_upgraded_from,
                SCHEMA_VERSION,
            )
        if reparse_engaged:
            n = cache.clear_engaged_parses()
            log.info("cleared %d cached qlog parses; will re-download and reparse", n)
        start_ms = _window_start(cache, settings, backfill=backfill, now_ms=now_ms)
        log.info(
            "listing routes %s → now (chunk=%dd, backfill=%s, metadata_only=%s)",
            datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date(),
            settings.chunk_days,
            backfill,
            metadata_only,
        )
        listed: list[RouteMeta] = []
        sample_logged = False
        for lo, hi in iter_time_chunks(start_ms, now_ms, settings.chunk_days):
            payload = client.list_routes_segments(lo, hi)
            if payload and not sample_logged:
                _log_length_fields(payload[0])
                sample_logged = True
            chunk = normalize_routes(payload, settings.dongle_id or "")
            log.info("  chunk %s..%s → %d routes", lo, hi, len(chunk))
            if len(payload) >= 1000:
                log.warning(
                    "chunk returned %d items — possible API cap. TODO: shrink CHUNK_DAYS.",
                    len(payload),
                )
            listed.extend(chunk)

        stats.routes_listed = len(listed)
        miles_gt0 = sum(1 for m in listed if m.length_miles > 0)
        miles_ge1 = sum(1 for m in listed if m.length_miles >= 1)
        log.info(
            "listed %d routes; length_miles>0=%d length_miles>=1=%d",
            len(listed),
            miles_gt0,
            miles_ge1,
        )
        if listed and miles_gt0 == 0:
            log.warning(
                "every listed route has length_miles=0 after mapping distance/length — "
                "check the sample keys logged above"
            )
        for meta in listed:
            cache.upsert_route_meta(_meta_to_row(meta))
            cache.set_watermark_ms(meta.start_time_utc_ms)

        if metadata_only:
            log.info("metadata-only: skipped qlog downloads for %d listed routes", len(listed))
        else:
            for meta in listed:
                row = cache.get_drive(meta.route_name)
                if row is None:
                    continue
                if not cache.needs_qlog_parse(row, recheck_ms):
                    stats.qlogs_skipped_cached += 1
                    continue
                try:
                    urls = client.route_qlog_urls(meta.route_name)
                    blobs = [client.download_bytes(u) for u in urls]
                    result = extract_engaged_time_from_qlogs(blobs, event_mod=event_mod)
                    cache.save_engaged(meta.route_name, result.engaged_time_s, result.source)
                    stats.qlogs_parsed += 1
                    log.info(
                        "  %s engaged=%.1fs source=%s samples=%d",
                        meta.route_name,
                        result.engaged_time_s,
                        result.source,
                        result.sample_count,
                    )
                except Exception as exc:
                    log.warning("qlog parse failed for %s: %s", meta.route_name, exc)

        cache.mark_run()
        cache.commit()
        html_path = generate_from_cache(settings, cache, mode="live")
        stats.html_path = str(html_path)
    return stats


def _log_length_fields(item: dict) -> None:
    """One-line dump of length-related keys from the first routes_segments object."""
    keys = sorted(str(k) for k in item.keys())
    log.info("routes_segments sample keys: %s", keys)
    for key in ("distance", "length", "length_miles"):
        if key in item:
            log.info("  sample %s=%r", key, item[key])


def _window_start(cache: Cache, settings: Settings, *, backfill: bool, now_ms: int) -> int:
    watermark = cache.watermark_ms()
    if backfill or watermark == 0:
        start = datetime.fromisoformat(settings.backfill_start).replace(tzinfo=timezone.utc)
        return int(start.timestamp() * 1000)
    recheck = timedelta(hours=settings.recheck_hours)
    return max(0, watermark - int(recheck.total_seconds() * 1000))


def _ts_ms(item: dict, ms_key: str, iso_key: str) -> int:
    if ms_key in item:
        return int(item[ms_key])
    raw = item[iso_key]
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _meta_to_row(meta: RouteMeta) -> DriveRow:
    # Wall-clock route duration, same window connect uses (segment start/end
    # times, stored there as milliseconds). Engage % = engaged / this.
    duration = max(0.0, (meta.end_time_utc_ms - meta.start_time_utc_ms) / 1000.0)
    return DriveRow(
        route_name=meta.route_name,
        dongle_id=meta.dongle_id,
        start_time_utc_ms=meta.start_time_utc_ms,
        end_time_utc_ms=meta.end_time_utc_ms,
        length_miles=meta.length_miles,
        total_drive_time_s=duration,
        git_commit=meta.git_commit,
        git_branch=meta.git_branch,
        git_remote=meta.git_remote,
        maxqlog=meta.maxqlog,
        engaged_time_s=None,
        engaged_source=None,
        qlog_parsed=False,
    )
