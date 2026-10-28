"""SQLite backed job history."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    playlist_url     TEXT NOT NULL,
    playlist_label   TEXT NOT NULL,
    audio_format     TEXT NOT NULL,
    title_first      INTEGER NOT NULL DEFAULT 0,
    max_concurrent   INTEGER NOT NULL,
    download_archive INTEGER NOT NULL DEFAULT 0,
    output_dir       TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    exit_code        INTEGER,
    matched          INTEGER NOT NULL DEFAULT 0,
    no_match         INTEGER NOT NULL DEFAULT 0,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs (created_at DESC);
"""

ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running", "cancelling")
TERMINAL_STATUSES: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
)


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JobStore:
    """Small thread-safe wrapper around a SQLite jobs table."""

    # Retry transient lock/IO errors (bind mounts, antivirus, backups).
    _RETRY_DELAYS: tuple[float, ...] = (0.0, 0.1, 0.3, 0.9)

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        with closing(self._connection()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # Rollback journal (the SQLite default) rather than WAL: WAL keeps a
        # shared-memory file that does not survive restarts reliably on
        # bind-mounted data directories (Docker Desktop, SMB/NFS shares).
        return conn

    def _write(self, sql: str, params: tuple[Any, ...]) -> int:
        last_error: sqlite3.OperationalError | None = None
        for delay in self._RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                with self._lock, closing(self._connection()) as conn:
                    with conn:
                        cursor = conn.execute(sql, params)
                        return cursor.rowcount
            except sqlite3.OperationalError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def create(self, **fields: Any) -> None:
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        self._write(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
            tuple(fields.values()),
        )

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._write(
            f"UPDATE jobs SET {assignments} WHERE id = ?",
            (*fields.values(), job_id),
        )

    def get(self, job_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return dict(rows[0]) if rows else None

    def list(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def _query(
        self, sql: str, params: tuple[Any, ...]
    ) -> Sequence[sqlite3.Row]:
        last_error: sqlite3.OperationalError | None = None
        for delay in self._RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                with closing(self._connection()) as conn:
                    return conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def delete(self, job_id: str) -> None:
        self._write("DELETE FROM jobs WHERE id = ?", (job_id,))

    def mark_interrupted(self) -> int:
        """Fail jobs that were still active when the process stopped."""

        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        return self._write(
            "UPDATE jobs SET status = 'failed', finished_at = ?, "
            "error = 'Interrupted by a server restart' "
            f"WHERE status IN ({placeholders})",
            (utcnow(), *ACTIVE_STATUSES),
        )
