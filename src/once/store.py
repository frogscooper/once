"""SQLite storage for effect records."""

from __future__ import annotations

import enum
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    args_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class Status(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Record:
    key: str
    status: Status
    result: str | None
    error: str | None
    args_hash: str | None
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SQLiteStore:
    """Effect records in a SQLite file, using WAL mode and a busy timeout.

    Each thread gets its own connection. Connections run in autocommit mode, so
    every statement is its own transaction.
    """

    def __init__(self, path: str | Path = "once.db", busy_timeout: float = 5.0) -> None:
        self.path = str(path)
        self.busy_timeout = busy_timeout
        self._local = threading.local()
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None)
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
            self._local.conn = conn
        return conn

    def claim(self, key: str, args_hash: str | None) -> bool:
        """Insert a pending record. Returns True if this caller now owns the key."""
        now = _now()
        cur = self._conn().execute(
            "INSERT INTO effects (key, status, args_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO NOTHING",
            (key, Status.PENDING.value, args_hash, now, now),
        )
        return cur.rowcount == 1

    def get(self, key: str) -> Record | None:
        row = (
            self._conn()
            .execute(
                "SELECT key, status, result, error, args_hash, created_at, updated_at "
                "FROM effects WHERE key = ?",
                (key,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return Record(row[0], Status(row[1]), row[2], row[3], row[4], row[5], row[6])

    def finish(
        self, key: str, status: Status, result: str | None = None, error: str | None = None
    ) -> None:
        """Move a pending record to its final state."""
        self._conn().execute(
            "UPDATE effects SET status = ?, result = ?, error = ?, updated_at = ? "
            "WHERE key = ? AND status = ?",
            (status.value, result, error, _now(), key, Status.PENDING.value),
        )

    def delete(self, key: str) -> bool:
        cur = self._conn().execute("DELETE FROM effects WHERE key = ?", (key,))
        return cur.rowcount == 1
