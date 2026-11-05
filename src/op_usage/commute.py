"""Commute route-id file. Membership only — no coordinates.

Default path is ``~/.config/op-usage/commute_routes.txt``. Override with
``OP_USAGE_COMMUTE_ROUTES``. One route id per line. The file stays outside
git (same idea as credentials.env).

Missing file → the site stays one table. Empty file → an empty commute
section, then every other drive.
"""

from __future__ import annotations

import re
from pathlib import Path

from op_usage.cache import DriveRow

# Comma fullname is "{dongle_id}|{route_id}". A chained name may list several
# ids separated by "|", commas, or whitespace.
_CHUNK = re.compile(r"[\s,]+")


def load_commute_route_ids(path: Path) -> set[str] | None:
    """Route ids from ``path``, or None if that file does not exist.

    Blank lines and ``#`` comments are ignored. Ids are matched
    case-insensitively. An existing file with no ids returns an empty set,
    which is not the same as a missing file.
    """
    if not path.is_file():
        return None
    ids: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ids.add(line.casefold())
    return ids


def route_name_parts(route_name: str) -> set[str]:
    """Pieces of a stored route name that a commute file may list.

    The whole string is included, plus each ``|`` / comma / whitespace piece.
    A drive is commute when any piece is in the file, so ``dongle|route_id``
    matches a line of ``route_id``, and a chained name matches if any of its
    ids is listed.
    """
    name = route_name.strip()
    if not name:
        return set()
    parts = {name}
    for chunk in _CHUNK.split(name):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.add(chunk)
        for piece in chunk.split("|"):
            piece = piece.strip()
            if piece:
                parts.add(piece)
    return parts


def is_commute_route(route_name: str, route_ids: set[str]) -> bool:
    """True when any route-name part is in ``route_ids`` (case-insensitive)."""
    if not route_ids or not route_name.strip():
        return False
    folded = {route_id.casefold() for route_id in route_ids}
    return any(part.casefold() in folded for part in route_name_parts(route_name))


def partition_drives(
    drives: list[DriveRow],
    route_ids: set[str],
) -> tuple[list[DriveRow], list[DriveRow]]:
    """Split drives into (commute, non-commute). Every drive is in exactly one."""
    commute: list[DriveRow] = []
    other: list[DriveRow] = []
    for drive in drives:
        if is_commute_route(drive.route_name, route_ids):
            commute.append(drive)
        else:
            other.append(drive)
    return commute, other
