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
    status TEXT NOT NULL, -- pending | done | failed | unknown
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

    def age(self) -> float:
        """Seconds since the record was last updated."""
        updated = datetime.fromisoformat(self.updated_at)
        return (datetime.now(timezone.utc) - updated).total_seconds()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SQLiteStore:
    """Effect records in a SQLite file, using WAL mode and a busy timeout.

    Each thread gets its own connection. Connections run in autocommit mode, so
    every statement is its own transaction.

    A pending record is owned by whoever set its current updated_at value. That
    value is returned as a token by claim and take, and finish only applies
    when the token still matches. This keeps a slow owner from overwriting a
    record that was taken over after verify.
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

    def claim(self, key: str, args_hash: str | None) -> str | None:
        """Insert a pending record. Returns an owner token, or None if the key exists."""
        now = _now()
        cur = self._conn().execute(
            "INSERT INTO effects (key, status, args_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO NOTHING",
            (key, Status.PENDING.value, args_hash, now, now),
        )
        return now if cur.rowcount == 1 else None

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
        self,
        key: str,
        token: str,
        status: Status,
        result: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Move a pending record we own to its final state."""
        cur = self._conn().execute(
            "UPDATE effects SET status = ?, result = ?, error = ?, updated_at = ? "
            "WHERE key = ? AND status = ? AND updated_at = ?",
            (status.value, result, error, _now(), key, Status.PENDING.value, token),
        )
        return cur.rowcount == 1

    def take(
        self,
        record: Record,
        status: Status,
        result: str | None = None,
        error: str | None = None,
    ) -> str | None:
        """Change a record only if it is unchanged since it was read.

        Returns the new token, or None if someone else changed it first.
        """
        now = _now()
        if now <= record.updated_at:
            return None
        cur = self._conn().execute(
            "UPDATE effects SET status = ?, result = ?, error = ?, updated_at = ? "
            "WHERE key = ? AND status = ? AND updated_at = ?",
            (status.value, result, error, now, record.key, record.status.value, record.updated_at),
        )
        return now if cur.rowcount == 1 else None

    def delete(self, key: str) -> bool:
        cur = self._conn().execute("DELETE FROM effects WHERE key = ?", (key,))
        return cur.rowcount == 1
