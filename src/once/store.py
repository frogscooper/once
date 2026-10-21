"""SQLite storage for effect records."""

from __future__ import annotations

import enum
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
        self._conns: dict[threading.Thread, sqlite3.Connection] = {}
        self._conns_lock = threading.Lock()
        self._setup()

    def _setup(self) -> None:
        # Switching to WAL can fail with "database is locked" without going
        # through the busy handler when several connections open a new file at
        # once, so retry here until busy_timeout runs out.
        conn = self._conn()
        deadline = time.monotonic() + self.busy_timeout
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            # Each connection is only used by the thread that opened it;
            # check_same_thread is off so close() can close them all.
            conn = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
            self._local.conn = conn
            with self._conns_lock:
                # Close connections left behind by threads that have exited.
                for thread in [t for t in self._conns if not t.is_alive()]:
                    self._conns.pop(thread).close()
                self._conns[threading.current_thread()] = conn
        return conn

    def close(self) -> None:
        """Close every connection this store opened. It reopens on next use."""
        with self._conns_lock:
            conns, self._conns = self._conns, {}
        for conn in conns.values():
            conn.close()
        self._local = threading.local()

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
            # Coarse clocks (Windows) can return the same time twice. The new
            # value has to differ from the old one, since it is the owner token.
            later = datetime.fromisoformat(record.updated_at) + timedelta(microseconds=1)
            now = later.isoformat(timespec="microseconds")
        cur = self._conn().execute(
            "UPDATE effects SET status = ?, result = ?, error = ?, updated_at = ? "
            "WHERE key = ? AND status = ? AND updated_at = ?",
            (status.value, result, error, now, record.key, record.status.value, record.updated_at),
        )
        return now if cur.rowcount == 1 else None

    def records(
        self, statuses: Iterable[Status] | None = None, limit: int | None = None
    ) -> list[Record]:
        """Records, most recently updated first."""
        sql = "SELECT key, status, result, error, args_hash, created_at, updated_at FROM effects"
        params: list[Any] = []
        if statuses is not None:
            values = [Status(s).value for s in statuses]
            sql += f" WHERE status IN ({','.join('?' * len(values))})"
            params += values
        sql += " ORDER BY updated_at DESC, key"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [Record(r[0], Status(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in rows]

    def keys_with_prefix(self, prefix: str, limit: int = 2) -> list[str]:
        rows = (
            self._conn()
            .execute(
                "SELECT key FROM effects WHERE substr(key, 1, ?) = ? ORDER BY key LIMIT ?",
                (len(prefix), prefix, limit),
            )
            .fetchall()
        )
        return [r[0] for r in rows]

    def purge(self, older_than: float, statuses: Iterable[Status]) -> int:
        """Delete records with one of ``statuses`` not updated for ``older_than`` seconds."""
        values = [Status(s).value for s in statuses]
        if not values:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than)).isoformat(
            timespec="microseconds"
        )
        marks = ",".join("?" * len(values))
        cur = self._conn().execute(
            f"DELETE FROM effects WHERE updated_at < ? AND status IN ({marks})",
            [cutoff, *values],
        )
        return cur.rowcount

    def delete(self, key: str) -> bool:
        cur = self._conn().execute("DELETE FROM effects WHERE key = ?", (key,))
        return cur.rowcount == 1
