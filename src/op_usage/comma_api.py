"""Comma connect-style REST client.

Verified against public docs (https://api.comma.ai / commaai/comma-api openapi.yaml):

  Auth header:     Authorization: JWT <token>   (not Bearer)
  Token source:    https://jwt.comma.ai
  Base URL:        https://api.commadotai.com  (api.comma.ai is the doc host)

  GET /v1/me
  GET /v1/devices/{dongleId}/routes_segments?start={ms}&end={ms}
       → RouteSegment objects (route metadata + segment_numbers / times)
       length_miles ← `distance` (miles), else OpenAPI `length` (miles)
       normalize_routes also accepts per-minute Segment objects
       (canonical_route_name) if that shape appears.
  GET /v1/route/{routeName}/files
       → { qlogs: [signed URLs] }  RATE LIMIT 5/min

Route length (miles): live routes_segments objects use `distance`. OpenAPI still
documents `length`. commaai/connect copies length → distance only when distance
is absent (back-compat). Both fields are GPS path length in miles — connect
displays them as mi / (mi × 1.60934) km. We follow that mapping.

401 on /v1/me or any JSON call: mint a new user JWT at jwt.comma.ai.
routes_segments is chunked by CHUNK_DAYS (default 14). If a window looks
truncated (~1000 rows), shrink CHUNK_DAYS. Route ids are opaque strings.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.commadotai.com"


@dataclass(frozen=True)
class RouteMeta:
    route_name: str
    dongle_id: str
    start_time_utc_ms: int
    end_time_utc_ms: int
    length_miles: float
    git_commit: str
    git_branch: str
    git_remote: str
    maxqlog: int | None


class CommaApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CommaClient:
    def __init__(
        self,
        jwt: str,
        dongle_id: str,
        api_base: str = DEFAULT_API_BASE,
        timeout_s: float = 60.0,
        files_min_interval_s: float = 13.0,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not jwt or not dongle_id:
            raise ValueError("COMMA_JWT and DONGLE_ID are required for live API calls")
        self.jwt = jwt
        self.dongle_id = dongle_id
        self.api_base = api_base.rstrip("/")
        self.timeout_s = timeout_s
        self.files_min_interval_s = files_min_interval_s
        self._session = session or requests.Session()
        self._sleeper = sleeper
        self._last_files_at = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"JWT {self.jwt}",
            "Accept": "application/json",
            "User-Agent": "op-usage/0.1 (personal; not comma-connect)",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_base}{path}"
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = self._session.get(
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=self.timeout_s,
                )
            except requests.RequestException as exc:
                last_exc = exc
                self._sleeper(min(2**attempt, 16))
                continue
            if resp.status_code == 401:
                raise CommaApiError(
                    "comma API 401 — mint a new user JWT at https://jwt.comma.ai "
                    "and update ~/.config/op-usage/credentials.env",
                    status=401,
                )
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 20))
                log.warning("rate limited on %s; sleeping %.0fs", path, wait)
                last_exc = CommaApiError(f"{path} HTTP 429", status=429)
                self._sleeper(wait)
                continue
            if resp.status_code >= 500:
                last_exc = CommaApiError(f"{path} HTTP {resp.status_code}", status=resp.status_code)
                self._sleeper(min(2**attempt, 16))
                continue
            if not resp.ok:
                raise CommaApiError(
                    f"{path} HTTP {resp.status_code}: {resp.text[:300]}",
                    status=resp.status_code,
                )
            return resp.json()
        raise CommaApiError(f"{path} failed after retries: {last_exc}")

    def verify_auth(self) -> dict[str, Any]:
        """GET /v1/me — confirms the JWT (401 → mint a new one)."""
        return self._get("/v1/me")

    def list_routes_segments(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """GET /v1/devices/{dongleId}/routes_segments?start=&end= (milliseconds)."""
        path = f"/v1/devices/{self.dongle_id}/routes_segments"
        payload = self._get(path, params={"start": int(start_ms), "end": int(end_ms)})
        if not isinstance(payload, list):
            raise CommaApiError(f"routes_segments expected a JSON array, got {type(payload).__name__}")
        return payload

    def route_qlog_urls(self, route_name: str) -> list[str]:
        """GET /v1/route/{routeName}/files → qlogs[]. Rate limited to 5/min."""
        elapsed = time.monotonic() - self._last_files_at
        if self._last_files_at and elapsed < self.files_min_interval_s:
            self._sleeper(self.files_min_interval_s - elapsed)
        encoded = quote(route_name, safe="")
        payload = self._get(f"/v1/route/{encoded}/files")
        self._last_files_at = time.monotonic()
        if not isinstance(payload, dict):
            raise CommaApiError("route files expected a JSON object")
        qlogs = payload.get("qlogs") or []
        return [u for u in qlogs if isinstance(u, str) and u]

    def download_bytes(self, url: str) -> bytes:
        """Signed blob URL — no JWT header (the query string is the auth)."""
        timeout = max(self.timeout_s, 120.0)
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = self._session.get(url, timeout=timeout)
            except requests.RequestException as exc:
                last_exc = exc
                self._sleeper(min(2**attempt, 16))
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 20))
                log.warning("rate limited on qlog download; sleeping %.0fs", wait)
                last_exc = CommaApiError("qlog download HTTP 429", status=429)
                self._sleeper(wait)
                continue
            if resp.status_code >= 500:
                last_exc = CommaApiError(
                    f"qlog download HTTP {resp.status_code}", status=resp.status_code
                )
                self._sleeper(min(2**attempt, 16))
                continue
            if not resp.ok:
                raise CommaApiError(
                    f"qlog download HTTP {resp.status_code}", status=resp.status_code
                )
            return resp.content
        raise CommaApiError(f"qlog download failed after retries: {last_exc}")


def normalize_routes(payload: list[dict[str, Any]], dongle_id: str) -> list[RouteMeta]:
    """Accept either RouteSegment (one object per route) or Segment lists."""
    if not payload:
        return []
    sample = payload[0]
    if "fullname" in sample or "segment_numbers" in sample:
        routes = [_from_route_object(item, dongle_id) for item in payload]
        return [r for r in routes if r.route_name]
    if "canonical_route_name" in sample or "route_name" in sample:
        return _group_segments(payload, dongle_id)
    raise CommaApiError(
        "unrecognized routes payload; expected RouteSegment (fullname) or Segment "
        "(canonical_route_name)"
    )


def _from_route_object(item: dict[str, Any], dongle_id: str) -> RouteMeta:
    name = str(item.get("fullname") or item.get("canonical_route_name") or "")
    start_ms, end_ms = _route_window_ms(item)
    maxqlog = item.get("maxqlog")
    return RouteMeta(
        route_name=name,
        dongle_id=str(item.get("dongle_id") or dongle_id),
        start_time_utc_ms=start_ms,
        end_time_utc_ms=end_ms,
        length_miles=_length_miles(item),
        git_commit=str(item.get("git_commit") or "").strip(),
        git_branch=str(item.get("git_branch") or "").strip(),
        git_remote=str(item.get("git_remote") or "").strip(),
        maxqlog=int(maxqlog) if maxqlog is not None else None,
    )


def _group_segments(segments: list[dict[str, Any]], dongle_id: str) -> list[RouteMeta]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for seg in segments:
        key = str(seg.get("canonical_route_name") or seg.get("route_name") or "")
        if not key:
            continue
        grouped.setdefault(key, []).append(seg)
    routes: list[RouteMeta] = []
    for name, segs in grouped.items():
        starts = [_scalar_ms(s, "start_time_utc_millis") for s in segs]
        ends = [_scalar_ms(s, "end_time_utc_millis") for s in segs]
        starts = [x for x in starts if x]
        ends = [x for x in ends if x]
        length = sum(_length_miles(s) for s in segs)
        head = segs[0]
        qlogs = [s.get("proc_qlog") for s in segs if s.get("proc_qlog") is not None]
        maxqlog = max((s.get("number") or 0) for s in segs) if segs else None
        if qlogs:
            maxqlog = max(maxqlog or 0, len(segs) - 1)
        routes.append(
            RouteMeta(
                route_name=name,
                dongle_id=str(head.get("dongle_id") or dongle_id),
                start_time_utc_ms=min(starts) if starts else 0,
                end_time_utc_ms=max(ends) if ends else 0,
                length_miles=length,
                git_commit=str(head.get("git_commit") or "").strip(),
                git_branch=str(head.get("git_branch") or "").strip(),
                git_remote=str(head.get("git_remote") or "").strip(),
                maxqlog=int(maxqlog) if maxqlog is not None else None,
            )
        )
    return routes


def _length_miles(item: dict[str, Any]) -> float:
    """GPS path length in miles from a RouteSegment or Segment object.

    Live API / connect: `distance` (miles). OpenAPI / older payloads: `length`
    (also miles). Prefer distance when it is present — even if it is 0 — so a
    leftover `length` of a few thousandths of a mile cannot wipe a real drive.
    Same rule as commaai/connect checkRoutesData:
      if (r.distance == null && r.length != null) r.distance = r.length
    """
    raw = item.get("distance")
    if raw is None:
        raw = item.get("length")
    if raw is None:
        raw = item.get("length_miles")
    try:
        miles = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if miles < 0.0:
        return 0.0
    return miles


def _route_window_ms(item: dict[str, Any]) -> tuple[int, int]:
    starts = item.get("segment_start_times") or []
    ends = item.get("segment_end_times") or []
    if starts and ends:
        return int(min(starts)), int(max(ends))
    start = _scalar_ms(item, "start_time_utc_millis") or _maybe_unix_to_ms(item.get("start_time"))
    end = _scalar_ms(item, "end_time_utc_millis") or _maybe_unix_to_ms(item.get("end_time"))
    if start and end:
        return start, end
    created = item.get("create_time")
    if created:
        ms = _maybe_unix_to_ms(created)
        return ms, ms
    return 0, 0


def _scalar_ms(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if value is None:
        return 0
    return int(value)


def _maybe_unix_to_ms(value: Any) -> int:
    if value is None:
        return 0
    num = float(value)
    if num < 1e11:  # seconds
        return int(num * 1000)
    return int(num)


def iter_time_chunks(start_ms: int, end_ms: int, chunk_days: int) -> list[tuple[int, int]]:
    """Inclusive windows used for backfill so we never request all history at once."""
    if end_ms <= start_ms:
        return []
    step = max(chunk_days, 1) * 24 * 60 * 60 * 1000
    chunks: list[tuple[int, int]] = []
    cursor = start_ms
    while cursor < end_ms:
        nxt = min(cursor + step, end_ms)
        chunks.append((cursor, nxt))
        cursor = nxt
    return chunks
