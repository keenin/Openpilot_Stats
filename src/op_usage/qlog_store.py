"""Local qlog blob store. Parse reads these files; Comma /files is sync-only.

Layout (matches the bulk-download cache):
  {qlog_dir}/{dongle_id}/{route_id}/{segment}.qlog

route_name is `{dongle_id}|{route_id}` (comma fullname). Writes are atomic
({segment}.qlog.partial → rename). Empty files do not count as present.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from op_usage.cache import Cache, DriveRow
from op_usage.comma_api import CommaClient
from op_usage.config import Settings

log = logging.getLogger(__name__)

_SEG_NAME = re.compile(r"^(\d+)\.qlog$")
# CDN / Azure paths look like .../{dongle}/{route}/{seg}/qlog.bz2?sig=...
_QLOG_SEG_IN_URL = re.compile(r"/(\d+)/qlog(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)


@dataclass
class SyncStats:
    routes_checked: int = 0
    routes_skipped_complete: int = 0
    qlogs_downloaded: int = 0
    routes_incomplete: int = 0
    qlogs_failed: int = 0


def split_route_name(route_name: str, dongle_id: str = "") -> tuple[str, str]:
    """Return (dongle_id, route_id) for the on-disk layout."""
    if "|" in route_name:
        left, right = route_name.split("|", 1)
        return (left or dongle_id, right)
    return (dongle_id or "unknown", route_name)


def route_dir(qlog_dir: Path, dongle_id: str, route_id: str) -> Path:
    return qlog_dir / dongle_id / route_id


def segment_path(qlog_dir: Path, dongle_id: str, route_id: str, segment: int) -> Path:
    return route_dir(qlog_dir, dongle_id, route_id) / f"{int(segment)}.qlog"


def write_qlog_atomic(path: Path, data: bytes) -> None:
    """Write bytes via `{name}.partial` then rename onto `{name}.qlog`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def list_local_segments(qlog_dir: Path, dongle_id: str, route_id: str) -> list[int]:
    """Segment numbers with a non-empty `{n}.qlog`. Ignores `.partial` leftovers."""
    folder = route_dir(qlog_dir, dongle_id, route_id)
    if not folder.is_dir():
        return []
    found: list[int] = []
    for child in folder.iterdir():
        if not child.is_file():
            continue
        match = _SEG_NAME.match(child.name)
        if match is None or child.stat().st_size <= 0:
            continue
        found.append(int(match.group(1)))
    return sorted(found)


def local_complete(have: list[int], maxqlog: int | None) -> bool:
    """True when every segment 0..maxqlog is on disk. Unknown maxqlog is never complete."""
    if maxqlog is None:
        return False
    needed = set(range(int(maxqlog) + 1))
    return needed.issubset(set(have))


def qlog_segment_from_url(url: str) -> int | None:
    path = urlparse(url).path.rstrip("/")
    match = _QLOG_SEG_IN_URL.search(path)
    if match:
        return int(match.group(1))
    return None


def urls_by_segment(urls: list[str]) -> dict[int, str]:
    """Map qlog URL → segment index. Falls back to list order when the path has no /N/qlog."""
    out: dict[int, str] = {}
    for index, url in enumerate(urls):
        seg = qlog_segment_from_url(url)
        out[index if seg is None else seg] = url
    return out


def load_route_qlogs(
    qlog_dir: Path,
    *,
    route_name: str,
    dongle_id: str = "",
    maxqlog: int | None = None,
) -> list[bytes] | None:
    """Load segment bytes in order, or None if missing/incomplete vs maxqlog.

    When maxqlog is known, all of 0..maxqlog must exist (non-empty).
    When maxqlog is unknown, any local segments are used (best effort); None
    only if the route directory has nothing.
    """
    dongle, route_id = split_route_name(route_name, dongle_id)
    have = list_local_segments(qlog_dir, dongle, route_id)
    if maxqlog is not None:
        if not local_complete(have, maxqlog):
            return None
        order = list(range(int(maxqlog) + 1))
    else:
        if not have:
            return None
        order = have
    return [segment_path(qlog_dir, dongle, route_id, seg).read_bytes() for seg in order]


def load_drive_qlogs(qlog_dir: Path, row: DriveRow) -> list[bytes] | None:
    return load_route_qlogs(
        qlog_dir,
        route_name=row.route_name,
        dongle_id=row.dongle_id,
        maxqlog=row.maxqlog,
    )


def missing_segments(have: list[int], maxqlog: int | None) -> list[int]:
    if maxqlog is None:
        return []
    have_set = set(have)
    return [i for i in range(int(maxqlog) + 1) if i not in have_set]


def run_sync_qlogs(
    settings: Settings,
    *,
    client: CommaClient | None = None,
) -> SyncStats:
    """Download missing qlogs for sqlite routes. Skips routes already complete on disk."""
    if not settings.has_auth:
        raise SystemExit(
            "COMMA_JWT and DONGLE_ID are required. Put them in "
            "~/.config/op-usage/credentials.env (see .env.example)."
        )
    if client is None:
        client = CommaClient(
            jwt=settings.comma_jwt or "",
            dongle_id=settings.dongle_id or "",
            api_base=settings.api_base,
            timeout_s=settings.request_timeout_s,
            files_min_interval_s=settings.files_min_interval_s,
        )
        log.info("verifying JWT via GET /v1/me")
        client.verify_auth()

    stats = SyncStats()
    with Cache(settings.cache_path) as cache:
        rows = list(cache.iter_drives())
    if not rows:
        log.info(
            "no routes in sqlite (%s); run nightly/backfill --metadata-only first",
            settings.cache_path,
        )
        return stats

    log.info(
        "sync-qlogs: checking %d sqlite routes under %s",
        len(rows),
        settings.qlog_dir,
    )
    for row in rows:
        _sync_one_route(settings.qlog_dir, client, row, stats)

    log.info(
        "sync-qlogs done: checked=%d skipped_complete=%d downloaded=%d "
        "incomplete=%d failed=%d",
        stats.routes_checked,
        stats.routes_skipped_complete,
        stats.qlogs_downloaded,
        stats.routes_incomplete,
        stats.qlogs_failed,
    )
    return stats


def _sync_one_route(
    qlog_dir: Path,
    client: CommaClient,
    row: DriveRow,
    stats: SyncStats,
) -> None:
    stats.routes_checked += 1
    dongle, route_id = split_route_name(row.route_name, row.dongle_id)
    have = list_local_segments(qlog_dir, dongle, route_id)
    if local_complete(have, row.maxqlog):
        stats.routes_skipped_complete += 1
        return
    try:
        urls = client.route_qlog_urls(row.route_name)
        by_seg = urls_by_segment([u for u in urls if u])
        if not by_seg:
            log.warning("no qlog URLs for %s; leave unparsed until a later sync", row.route_name)
            stats.routes_incomplete += 1
            return
        for seg, url in sorted(by_seg.items()):
            dest = segment_path(qlog_dir, dongle, route_id, seg)
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            blob = client.download_bytes(url)
            if not blob:
                log.warning("empty qlog download for %s seg %s; not writing", row.route_name, seg)
                continue
            write_qlog_atomic(dest, blob)
            stats.qlogs_downloaded += 1
            log.info(
                "  %s → %s (%d bytes)",
                row.route_name,
                dest,
                len(blob),
            )
        have = list_local_segments(qlog_dir, dongle, route_id)
        if row.maxqlog is not None and not local_complete(have, row.maxqlog):
            stats.routes_incomplete += 1
            log.warning(
                "qlogs still incomplete for %s (maxqlog=%s have=%s missing=%s)",
                row.route_name,
                row.maxqlog,
                have,
                missing_segments(have, row.maxqlog),
            )
    except Exception as exc:
        stats.qlogs_failed += 1
        log.warning("qlog sync failed for %s: %s", row.route_name, exc)
