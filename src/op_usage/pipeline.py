"""Fetch → cache engaged time → write index.html.

Incremental: watermark + 24h recheck. First run backfills from BACKFILL_START.
Demo/dry-run uses fixtures and never talks to comma.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from op_usage.aggregate import aggregate_commits
from op_usage.cache import SCHEMA_VERSION, Cache, DriveRow
from op_usage.comma_api import CommaClient, RouteMeta, iter_time_chunks, normalize_routes
from op_usage.config import Settings, is_live_cache_path, is_live_site_dir
from op_usage.qlog import extract_engaged_time_from_qlogs, load_event_module
from op_usage.site import render_site, write_site
from op_usage.weights import make_weights_lookup

log = logging.getLogger(__name__)

# Completed drives older than the 24h settling list window, but recent enough
# that comma may still append qlog segments. Targeted re-list only — not a
# historical routes_segments scan, and not a parse trigger by itself.
LATE_UPLOAD_HOURS = 7 * 24
_START_WINDOW_PAD_MS = 60_000


@dataclass
class RunStats:
    routes_listed: int = 0
    qlogs_parsed: int = 0
    qlogs_skipped_cached: int = 0
    html_path: str = ""


def generate_from_cache(settings: Settings, cache: Cache) -> Path:
    commits = aggregate_commits(list(cache.iter_drives()), weights_lookup=make_weights_lookup(cache))
    html = render_site(
        commits,
        generated_at=datetime.now(timezone.utc),
        display_tz=settings.display_tz,
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
                not_in_park_time_s=(
                    None
                    if item.get("not_in_park_time_s") is None
                    else float(item["not_in_park_time_s"])
                ),
                weighted_engaged_time_s=(
                    None
                    if item.get("weighted_engaged_time_s") is None
                    else float(item["weighted_engaged_time_s"])
                ),
                steady_frac=(
                    None if item.get("steady_frac") is None else float(item["steady_frac"])
                ),
                parser_version=(
                    None if item.get("parser_version") is None else int(item["parser_version"])
                ),
            )
        )
    return drives


def refuse_demo_on_live_paths(settings: Settings) -> None:
    """Demo does a full DELETE FROM drives — never point it at live paths."""
    if is_live_cache_path(settings.cache_path):
        raise SystemExit(
            "demo refuses to use the live cache "
            f"({settings.cache_path}). Pass --cache /tmp/op-usage-demo.sqlite "
            "(or omit --cache to use a temp file)."
        )
    if is_live_site_dir(settings.site_dir):
        raise SystemExit(
            "demo refuses to overwrite the live site "
            f"({settings.site_dir}). Pass --out /tmp/op-usage-demo-site "
            "(or omit --out to use a temp directory)."
        )


def run_demo(settings: Settings, fixture_path: Path) -> RunStats:
    refuse_demo_on_live_paths(settings)
    with Cache(settings.cache_path) as cache:
        cache.replace_all(load_fixture_drives(fixture_path))
        html_path = generate_from_cache(settings, cache)
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
                "cache schema_version %s → %s; cached qlog rows will not "
                "reparse unless you pass --reparse-engaged (or SQL-clear qlog_parsed). "
                "Weighted engaged time needs a reparse of routes that predate parser v2.",
                cache.schema_upgraded_from,
                SCHEMA_VERSION,
            )
        start_ms = _window_start(
            cache, settings, backfill=backfill, recheck_after_ms=recheck_ms
        )
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
            payload, sample_logged = _list_chunk(
                client, lo, hi, settings.dongle_id or "", sample_logged
            )
            listed.extend(payload)

        if not backfill:
            late_since = now_ms - LATE_UPLOAD_HOURS * 3600 * 1000
            extra = _coalesce_start_windows(cache.late_upload_starts(
                since_ms=late_since, recheck_after_ms=recheck_ms
            ))
            for lo, hi in extra:
                if lo >= start_ms:
                    continue
                payload, sample_logged = _list_chunk(
                    client, lo, hi, settings.dongle_id or "", sample_logged
                )
                listed.extend(payload)

        listed = _dedupe_routes(listed)
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

        # Compare incoming maxqlog to the cached row *before* upsert overwrites it.
        need_parse: set[str] = set()
        listed_names: set[str] = set()
        for meta in listed:
            incoming = _meta_to_row(meta)
            listed_names.add(meta.route_name)
            if not metadata_only and cache.needs_qlog_parse(incoming):
                need_parse.add(meta.route_name)
            cache.upsert_route_meta(incoming)
            cache.set_watermark_ms(meta.start_time_utc_ms)

        if reparse_engaged:
            n = cache.clear_engaged_parses(route_names=[m.route_name for m in listed])
            log.info(
                "cleared %d cached qlog parses among %d listed routes; "
                "will re-download and reparse (other cache rows left intact)",
                n,
                len(listed),
            )
            need_parse = {m.route_name for m in listed}

        if not metadata_only:
            need_parse.update(
                row.route_name for row in cache.iter_drives() if not row.qlog_parsed
            )

        cache.commit()

        if metadata_only:
            log.info("metadata-only: skipped qlog downloads for %d listed routes", len(listed))
        else:
            stats.qlogs_skipped_cached = sum(
                1 for name in listed_names if name not in need_parse
            )
            for name in need_parse:
                row = cache.get_drive(name)
                if row is None:
                    continue
                try:
                    urls = client.route_qlog_urls(name)
                    blobs = [client.download_bytes(u) for u in urls]
                    result = extract_engaged_time_from_qlogs(blobs, event_mod=event_mod)
                    if result.sample_count == 0:
                        log.info(
                            "  %s empty qlog parse (samples=0); keeping last-known engaged",
                            name,
                        )
                        continue
                    # Store None when the gear integral is unusable; denominator_s
                    # falls back to API wall-clock. Do not write 0.0 as "measured".
                    cache.save_engaged(
                        name,
                        result.engaged_time_s,
                        result.source,
                        result.not_in_park_time_s,
                        result.weighted_engaged_time_s,
                        result.steady_frac,
                        result.parser_version,
                    )
                    cache.commit()
                    stats.qlogs_parsed += 1
                    park_log = (
                        result.not_in_park_time_s
                        if result.not_in_park_time_s is not None
                        else row.total_drive_time_s
                    )
                    log.info(
                        "  %s engaged=%.1fs weighted=%s not_in_park=%.1fs source=%s "
                        "samples=%d gear_samples=%d speed_samples=%d parser=%d",
                        name,
                        result.engaged_time_s,
                        (
                            f"{result.weighted_engaged_time_s:.1f}s"
                            if result.weighted_engaged_time_s is not None
                            else "null"
                        ),
                        park_log,
                        result.source,
                        result.sample_count,
                        result.gear_sample_count,
                        result.speed_sample_count,
                        result.parser_version,
                    )
                except Exception as exc:
                    log.warning("qlog parse failed for %s: %s", name, exc)

        cache.mark_run()
        cache.commit()
        html_path = generate_from_cache(settings, cache)
        stats.html_path = str(html_path)
    return stats


def _list_chunk(
    client: CommaClient,
    lo: int,
    hi: int,
    dongle_id: str,
    sample_logged: bool,
) -> tuple[list[RouteMeta], bool]:
    payload = client.list_routes_segments(lo, hi)
    if payload and not sample_logged:
        _log_length_fields(payload[0])
        sample_logged = True
    chunk = normalize_routes(payload, dongle_id)
    log.info("  chunk %s..%s → %d routes", lo, hi, len(chunk))
    if len(payload) >= 1000:
        log.warning(
            "chunk returned %d items — possible API cap; shrink CHUNK_DAYS.",
            len(payload),
        )
    return chunk, sample_logged


def _coalesce_start_windows(
    starts: list[int], pad_ms: int = _START_WINDOW_PAD_MS
) -> list[tuple[int, int]]:
    """Merge nearby drive starts into tiny targeted list windows."""
    if not starts:
        return []
    ordered = sorted(starts)
    windows: list[tuple[int, int]] = []
    lo = ordered[0]
    hi = ordered[0] + pad_ms
    for start in ordered[1:]:
        if start <= hi + pad_ms:
            hi = max(hi, start + pad_ms)
        else:
            windows.append((lo, hi))
            lo = start
            hi = start + pad_ms
    windows.append((lo, hi))
    return windows


def _log_length_fields(item: dict) -> None:
    """One-line dump of length-related keys from the first routes_segments object."""
    keys = sorted(str(k) for k in item.keys())
    log.info("routes_segments sample keys: %s", keys)
    for key in ("distance", "length", "length_miles"):
        if key in item:
            log.info("  sample %s=%r", key, item[key])


def _dedupe_routes(routes: list[RouteMeta]) -> list[RouteMeta]:
    """Keep the last listing per route_name; drop nameless rows (chunk overlap)."""
    by_name: dict[str, RouteMeta] = {}
    for meta in routes:
        if meta.route_name:
            by_name[meta.route_name] = meta
    return list(by_name.values())


def _window_start(
    cache: Cache,
    settings: Settings,
    *,
    backfill: bool,
    recheck_after_ms: int,
) -> int:
    watermark = cache.watermark_ms()
    if backfill or watermark == 0:
        start = datetime.fromisoformat(settings.backfill_start).replace(tzinfo=timezone.utc)
        return int(start.timestamp() * 1000)
    window = max(0, watermark - settings.recheck_hours * 3600 * 1000)
    settling = cache.earliest_settling_start_ms(recheck_after_ms)
    if settling is not None:
        window = min(window, settling)
    return window


def _ts_ms(item: dict, ms_key: str, iso_key: str) -> int:
    if ms_key in item:
        return int(item[ms_key])
    raw = item[iso_key]
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _meta_to_row(meta: RouteMeta) -> DriveRow:
    # API wall-clock; engage % uses not_in_park_time_s from the qlog parse.
    duration = max(0.0, (meta.end_time_utc_ms - meta.start_time_utc_ms) / 1000.0)
    return DriveRow(
        **asdict(meta),
        total_drive_time_s=duration,
        engaged_time_s=None,
        engaged_source=None,
        qlog_parsed=False,
    )
