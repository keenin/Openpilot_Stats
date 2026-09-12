"""SQLite cache: per-route metadata, engaged time, incremental watermark.

Schema (see README for the same picture):

  meta(key TEXT PK, value TEXT)
    watermark_ms          max start_time_utc_ms of listed routes
    last_run_iso
    dongle_id
    schema_version

  drives(...) one row per route; engaged_time_s and not_in_park_time_s
  are cached so old qlogs are never re-parsed once qlog_parsed=1 and the
  route is outside the recheck window.

  total_drive_time_s is API wall-clock (end-start). Engage % uses
  not_in_park_time_s (qlog gear integral) after a parse.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = "3"  # not_in_park_time_s; does not auto-clear qlog rows

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
  qlog_parsed INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drives_commit ON drives(git_commit);
CREATE INDEX IF NOT EXISTS idx_drives_start ON drives(start_time_utc_ms);
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
        self.set_meta("last_run_iso", datetime.now(timezone.utc).isoformat())

    def get_drive(self, route_name: str) -> DriveRow | None:
        row = self._conn.execute("SELECT * FROM drives WHERE route_name = ?", (route_name,)).fetchone()
        return None if row is None else _row_to_drive(row)

    def upsert_route_meta(self, drive: DriveRow) -> None:
        """Insert or refresh comma API metadata. Does not clear cached qlog parse."""
        existing = self.get_drive(drive.route_name)
        if existing and existing.qlog_parsed:
            self._conn.execute(
                """
                UPDATE drives SET
                  dongle_id = ?, start_time_utc_ms = ?, end_time_utc_ms = ?,
                  length_miles = ?, total_drive_time_s = ?,
                  git_commit = ?, git_branch = ?, git_remote = ?,
                  maxqlog = ?, updated_at = ?
                WHERE route_name = ?
                """,
                (
                    drive.dongle_id,
                    drive.start_time_utc_ms,
                    drive.end_time_utc_ms,
                    drive.length_miles,
                    drive.total_drive_time_s,
                    drive.git_commit,
                    drive.git_branch,
                    drive.git_remote,
                    drive.maxqlog,
                    _now(),
                    drive.route_name,
                ),
            )
            return
        self._conn.execute(
            """
            INSERT INTO drives (
              route_name, dongle_id, start_time_utc_ms, end_time_utc_ms,
              length_miles, total_drive_time_s, git_commit, git_branch, git_remote,
              maxqlog, engaged_time_s, engaged_source, not_in_park_time_s,
              qlog_parsed, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(route_name) DO UPDATE SET
              dongle_id = excluded.dongle_id,
              start_time_utc_ms = excluded.start_time_utc_ms,
              end_time_utc_ms = excluded.end_time_utc_ms,
              length_miles = excluded.length_miles,
              total_drive_time_s = excluded.total_drive_time_s,
              git_commit = excluded.git_commit,
              git_branch = excluded.git_branch,
              git_remote = excluded.git_remote,
              maxqlog = excluded.maxqlog,
              updated_at = excluded.updated_at
            """,
            (
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
                _now(),
            ),
        )

    def save_engaged(
        self,
        route_name: str,
        engaged_time_s: float,
        engaged_source: str,
        not_in_park_time_s: float | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE drives SET
              engaged_time_s = ?, engaged_source = ?, not_in_park_time_s = ?,
              qlog_parsed = 1, updated_at = ?
            WHERE route_name = ?
            """,
            (engaged_time_s, engaged_source, not_in_park_time_s, _now(), route_name),
        )

    def clear_engaged_parses(self, route_names: Iterable[str] | None = None) -> int:
        """Drop cached qlog results so the next fetch re-reads those routes.

        Use after a parser fix: qlog_parsed=1 (including engaged_time_s=0 or
        a NULL not_in_park_time_s) is otherwise skipped forever.

        If route_names is given, only those rows are cleared — so
        nightly --reparse-engaged cannot wipe history outside the fetch window.
        Omit route_names to clear every drive (the SQL-equivalent documented
        in the README).
        """
        sql = """
            UPDATE drives SET
              qlog_parsed = 0,
              engaged_time_s = NULL,
              engaged_source = NULL,
              not_in_park_time_s = NULL,
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

    def needs_qlog_parse(self, drive: DriveRow, recheck_after_ms: int) -> bool:
        """True if incoming API metadata still needs a qlog download.

        `drive` must be the *new* listing (especially maxqlog), compared
        against the cached row. Call this before upsert_route_meta — after
        an upsert the cached maxqlog already matches, so the 24h
        “maxqlog grew” recheck never fires.
        """
        existing = self.get_drive(drive.route_name)
        if existing is None:
            return True
        if not existing.qlog_parsed:
            return True
        in_flight = drive.start_time_utc_ms >= recheck_after_ms
        if in_flight and _maxqlog_grew(existing.maxqlog, drive.maxqlog):
            return True
        return False

    def iter_drives(self) -> Iterator[DriveRow]:
        for row in self._conn.execute("SELECT * FROM drives"):
            yield _row_to_drive(row)

    def replace_all(self, drives: Iterable[DriveRow]) -> None:
        """Used by demo/fixtures — full replace, no comma API."""
        self._conn.execute("DELETE FROM drives")
        for drive in drives:
            self._conn.execute(
                """
                INSERT INTO drives (
                  route_name, dongle_id, start_time_utc_ms, end_time_utc_ms,
                  length_miles, total_drive_time_s, git_commit, git_branch, git_remote,
                  maxqlog, engaged_time_s, engaged_source, not_in_park_time_s,
                  qlog_parsed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
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
                    _now(),
                ),
            )
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _maxqlog_grew(old: int | None, new: int | None) -> bool:
    if new is None:
        return False
    if old is None:
        return True
    return new > old


def _row_to_drive(row: sqlite3.Row) -> DriveRow:
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
        not_in_park_time_s=_optional_float(row, "not_in_park_time_s"),
    )


def _optional_float(row: sqlite3.Row, key: str) -> float | None:
    try:
        raw = row[key]
    except (IndexError, KeyError):
        return None
    return None if raw is None else float(raw)
