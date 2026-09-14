"""SQLite cache: per-route metadata, engaged time, incremental watermark.

qlog_parsed=1 rows are not re-read unless incoming maxqlog grew or
parses are explicitly cleared. The 24h end-time window is a listing
hint (see pipeline), not a nightly re-download trigger.
total_drive_time_s is API wall-clock; engage % uses weighted engaged
over not_in_park_time_s after a parse (raw engaged if weighted is null).
Schema picture is in the README.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from op_usage import MIN_MILES
from op_usage.config import is_live_cache_path

SCHEMA_VERSION = "4"  # weighted_engaged_time_s; does not auto-clear qlog rows

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drives (
  route_name TEXT PRIMARY KEY,
  dongle_id TEXT NOT NULL,
  start_time_utc_ms INTEGER NOT NULL,
  end_time_utc_ms INTEGER NOT NULL,
  length_miles REAL NOT NULL,
  total_drive_time_s REAL NOT NULL,
  git_commit TEXT,
  git_branch TEXT,
  git_remote TEXT,
  maxqlog INTEGER,
  engaged_time_s REAL,
  engaged_source TEXT,
  not_in_park_time_s REAL,
  weighted_engaged_time_s REAL,
  steady_frac REAL,
  parser_version INTEGER,
  qlog_parsed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drives_commit ON drives(git_commit);
CREATE INDEX IF NOT EXISTS idx_drives_start ON drives(start_time_utc_ms);

CREATE TABLE IF NOT EXISTS commit_weights (
  repo TEXT NOT NULL,
  git_commit TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (repo, git_commit)
);
"""


@dataclass
class DriveRow:
    route_name: str
    dongle_id: str
    start_time_utc_ms: int
    end_time_utc_ms: int
    length_miles: float
    total_drive_time_s: float
    git_commit: str
    git_branch: str
    git_remote: str
    maxqlog: int | None
    engaged_time_s: float | None
    engaged_source: str | None
    qlog_parsed: bool
    updated_at: str = ""
    not_in_park_time_s: float | None = None
    weighted_engaged_time_s: float | None = None
    steady_frac: float | None = None
    parser_version: int | None = None


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA_SQL)
        self._migrate_drives_columns()
        previous = self.get_meta("schema_version")
        self.set_meta("schema_version", SCHEMA_VERSION)
        self._conn.commit()
        self.schema_upgraded_from = previous if previous and previous != SCHEMA_VERSION else None

    def _migrate_drives_columns(self) -> None:
        """Add columns introduced after the original CREATE TABLE."""
        cols = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(drives)")}
        if "not_in_park_time_s" not in cols:
            self._conn.execute("ALTER TABLE drives ADD COLUMN not_in_park_time_s REAL")
        if "weighted_engaged_time_s" not in cols:
            self._conn.execute("ALTER TABLE drives ADD COLUMN weighted_engaged_time_s REAL")
        if "steady_frac" not in cols:
            self._conn.execute("ALTER TABLE drives ADD COLUMN steady_frac REAL")
        if "parser_version" not in cols:
            self._conn.execute("ALTER TABLE drives ADD COLUMN parser_version INTEGER")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def watermark_ms(self) -> int:
        raw = self.get_meta("watermark_ms")
        return int(raw) if raw else 0

    def set_watermark_ms(self, ms: int) -> None:
        current = self.watermark_ms()
        if ms > current:
            self.set_meta("watermark_ms", str(ms))

    def mark_run(self) -> None:
        self.set_meta("last_run_iso", _now())

    def get_drive(self, route_name: str) -> DriveRow | None:
        row = self._conn.execute("SELECT * FROM drives WHERE route_name = ?", (route_name,)).fetchone()
        return None if row is None else _row_to_drive(row)

    def upsert_route_meta(self, drive: DriveRow) -> None:
        """Insert or refresh comma API metadata. Does not clear cached qlog parse."""
        self._conn.execute(_UPSERT_ROUTE_SQL, _drive_values(drive, qlog_parsed=0))

    def save_engaged(
        self,
        route_name: str,
        engaged_time_s: float,
        engaged_source: str,
        not_in_park_time_s: float | None = None,
        weighted_engaged_time_s: float | None = None,
        steady_frac: float | None = None,
        parser_version: int | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE drives SET
              engaged_time_s = ?, engaged_source = ?, not_in_park_time_s = ?,
              weighted_engaged_time_s = ?, steady_frac = ?, parser_version = ?,
              qlog_parsed = 1, updated_at = ?
            WHERE route_name = ?
            """,
            (
                engaged_time_s,
                engaged_source,
                not_in_park_time_s,
                weighted_engaged_time_s,
                steady_frac,
                parser_version,
                _now(),
                route_name,
            ),
        )

    def clear_engaged_parses(self, route_names: Iterable[str] | None = None) -> int:
        """Drop cached qlog results so the next fetch re-reads those routes.

        qlog_parsed=1 (including zeros) is otherwise skipped forever. If
        route_names is given, only those rows are cleared.
        """
        sql = """
            UPDATE drives SET
              qlog_parsed = 0,
              engaged_time_s = NULL,
              engaged_source = NULL,
              not_in_park_time_s = NULL,
              weighted_engaged_time_s = NULL,
              steady_frac = NULL,
              parser_version = NULL,
              updated_at = ?
        """
        if route_names is None:
            cur = self._conn.execute(sql, (_now(),))
        else:
            names = [name for name in route_names if name]
            if not names:
                return 0
            placeholders = ",".join("?" * len(names))
            cur = self._conn.execute(
                f"{sql} WHERE route_name IN ({placeholders})",
                (_now(), *names),
            )
        return int(cur.rowcount or 0)

    def needs_qlog_parse(self, drive: DriveRow) -> bool:
        """True if incoming API metadata still needs a qlog download.

        Parse only when the row is unparsed or incoming maxqlog grew.
        `drive` must be the *new* listing, compared before upsert_route_meta.
        """
        existing = self.get_drive(drive.route_name)
        if existing is None or not existing.qlog_parsed:
            return True
        old, new = existing.maxqlog, drive.maxqlog
        return new is not None and (old is None or new > old)

    def earliest_settling_start_ms(self, recheck_after_ms: int) -> int | None:
        """Earliest start among in-flight / recently-ended cached drives.

        Used to keep settling routes in the nightly *list* window so
        maxqlog growth is visible. Does not include ancient unparsed rows.
        """
        row = self._conn.execute(
            "SELECT MIN(start_time_utc_ms) FROM drives WHERE end_time_utc_ms >= ?",
            (recheck_after_ms,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def late_upload_starts(self, *, since_ms: int, recheck_after_ms: int) -> list[int]:
        """Start times of completed-but-recent drives (late qlog uploads).

        `since_ms <= end < recheck_after_ms` — older than the 24h settling
        list window, but recent enough that comma may still append qlogs.
        """
        rows = self._conn.execute(
            """
            SELECT start_time_utc_ms FROM drives
            WHERE end_time_utc_ms >= ? AND end_time_utc_ms < ?
            ORDER BY start_time_utc_ms
            """,
            (since_ms, recheck_after_ms),
        )
        return [int(row[0]) for row in rows]

    def iter_drives(self) -> Iterator[DriveRow]:
        for row in self._conn.execute("SELECT * FROM drives"):
            yield _row_to_drive(row)

    def replace_all(self, drives: Iterable[DriveRow]) -> None:
        """Used by demo/fixtures — full replace, no comma API."""
        if is_live_cache_path(self.path):
            raise RuntimeError(
                "refusing to DELETE FROM drives on the live cache "
                f"({self.path}). Pass --cache to a temp sqlite for demo."
            )
        self._conn.execute("DELETE FROM drives")
        for drive in drives:
            self._conn.execute(_DRIVE_INSERT_SQL, _drive_values(drive, qlog_parsed=1))
        self._conn.commit()

    def get_commit_weights(self, repo: str, git_commit: str) -> str | None:
        row = self._conn.execute(
            "SELECT fingerprint FROM commit_weights WHERE repo = ? AND git_commit = ?",
            (repo, git_commit.lower()),
        ).fetchone()
        return None if row is None else str(row["fingerprint"])

    def set_commit_weights(self, repo: str, git_commit: str, fingerprint: str) -> None:
        self._conn.execute(
            """
            INSERT INTO commit_weights(repo, git_commit, fingerprint, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo, git_commit) DO UPDATE SET
              fingerprint = excluded.fingerprint,
              updated_at = excluded.updated_at
            """,
            (repo, git_commit.lower(), fingerprint, _now()),
        )

    def commit(self) -> None:
        self._conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DRIVE_INSERT_SQL = """
INSERT INTO drives (
  route_name, dongle_id, start_time_utc_ms, end_time_utc_ms,
  length_miles, total_drive_time_s, git_commit, git_branch, git_remote,
  maxqlog, engaged_time_s, engaged_source, not_in_park_time_s,
  weighted_engaged_time_s, steady_frac, parser_version,
  qlog_parsed, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Last-known-good: blank incoming git_* does not clobber a cached value.
_KEEP_NONEMPTY = (
    "CASE WHEN excluded.{0} IS NOT NULL AND TRIM(excluded.{0}) != '' "
    "THEN excluded.{0} ELSE drives.{0} END"
)

_UPSERT_ROUTE_SQL = _DRIVE_INSERT_SQL + f"""
ON CONFLICT(route_name) DO UPDATE SET
  dongle_id = excluded.dongle_id,
  start_time_utc_ms = excluded.start_time_utc_ms,
  end_time_utc_ms = excluded.end_time_utc_ms,
  length_miles = CASE
    WHEN excluded.length_miles >= {MIN_MILES} THEN excluded.length_miles
    WHEN drives.length_miles >= {MIN_MILES} THEN drives.length_miles
    WHEN excluded.length_miles > 0 THEN excluded.length_miles
    ELSE drives.length_miles
  END,
  total_drive_time_s = excluded.total_drive_time_s,
  git_commit = {_KEEP_NONEMPTY.format("git_commit")},
  git_branch = {_KEEP_NONEMPTY.format("git_branch")},
  git_remote = {_KEEP_NONEMPTY.format("git_remote")},
  maxqlog = excluded.maxqlog,
  updated_at = excluded.updated_at
"""


def _drive_values(drive: DriveRow, qlog_parsed: int) -> tuple:
    return (
        drive.route_name,
        drive.dongle_id,
        drive.start_time_utc_ms,
        drive.end_time_utc_ms,
        drive.length_miles,
        drive.total_drive_time_s,
        drive.git_commit,
        drive.git_branch,
        drive.git_remote,
        drive.maxqlog,
        drive.engaged_time_s,
        drive.engaged_source,
        drive.not_in_park_time_s,
        drive.weighted_engaged_time_s,
        drive.steady_frac,
        drive.parser_version,
        qlog_parsed,
        _now(),
    )


def _row_to_drive(row: sqlite3.Row) -> DriveRow:
    raw_park = row["not_in_park_time_s"]
    keys = row.keys()
    raw_weighted = row["weighted_engaged_time_s"] if "weighted_engaged_time_s" in keys else None
    raw_frac = row["steady_frac"] if "steady_frac" in keys else None
    raw_ver = row["parser_version"] if "parser_version" in keys else None
    return DriveRow(
        route_name=row["route_name"],
        dongle_id=row["dongle_id"],
        start_time_utc_ms=int(row["start_time_utc_ms"]),
        end_time_utc_ms=int(row["end_time_utc_ms"]),
        length_miles=float(row["length_miles"]),
        total_drive_time_s=float(row["total_drive_time_s"]),
        git_commit=row["git_commit"] or "",
        git_branch=row["git_branch"] or "",
        git_remote=row["git_remote"] or "",
        maxqlog=row["maxqlog"],
        engaged_time_s=None if row["engaged_time_s"] is None else float(row["engaged_time_s"]),
        engaged_source=row["engaged_source"],
        qlog_parsed=bool(row["qlog_parsed"]),
        updated_at=row["updated_at"] or "",
        not_in_park_time_s=None if raw_park is None else float(raw_park),
        weighted_engaged_time_s=None if raw_weighted is None else float(raw_weighted),
        steady_frac=None if raw_frac is None else float(raw_frac),
        parser_version=None if raw_ver is None else int(raw_ver),
    )
