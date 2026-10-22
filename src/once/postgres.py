"""Postgres storage for effect records, for workers on more than one machine.

Needs psycopg 3: pip install 'agent-once[postgres]'.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from typing import Any

try:
    import psycopg
    from psycopg import sql
except ImportError as exc:  # pragma: no cover - depends on what is installed
    raise ImportError("PostgresStore needs psycopg, pip install 'agent-once[postgres]'") from exc

from .store import Record, Status, _cutoff, _next_token, _now

DEFAULT_TABLE = "once_effects"

_COLUMNS = "key, status, result, error, args_hash, created_at, updated_at"

# Timestamps are stored as text in the same format SQLiteStore uses. The exact
# string is the owner token, so it must round-trip unchanged.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    args_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class PostgresStore:
    """Effect records in a Postgres table.

    dsn: a libpq connection string or postgresql:// URL.
    table: table name, optionally schema qualified ("myschema.effects").
        Created if missing, unless create is False.

    One connection per store, in autocommit mode, shared by all threads with a
    lock. Every statement is its own transaction, so no row stays locked
    between calls. Safety between processes and hosts comes from the primary
    key and the conditional updates, not from anything in this process.
    A dropped connection is reopened on the next call.
    """

    def __init__(
        self,
        dsn: str,
        *,
        table: str = DEFAULT_TABLE,
        create: bool = True,
        connect_timeout: float = 10.0,
    ) -> None:
        parts = table.split(".")
        if not 1 <= len(parts) <= 2 or not all(parts):
            raise ValueError(f"bad table name {table!r}")
        self.dsn = dsn
        self.table = table
        self.connect_timeout = connect_timeout
        self._ident = sql.Identifier(*parts)
        self._conn_obj: psycopg.Connection[Any] | None = None
        self._lock = threading.RLock()
        if create:
            self._create()

    def _q(self, query: str) -> sql.Composed:
        return sql.SQL(query).format(table=self._ident)

    def _conn(self) -> psycopg.Connection[Any]:
        conn = self._conn_obj
        if conn is None or conn.closed or conn.broken:
            conn = psycopg.connect(
                self.dsn, autocommit=True, connect_timeout=max(1, int(self.connect_timeout))
            )
            self._conn_obj = conn
        return conn

    def _execute(self, query: str, params: Sequence[Any] = ()) -> psycopg.Cursor[Any]:
        with self._lock:
            return self._conn().execute(self._q(query), params)

    def _create(self) -> None:
        # Concurrent CREATE TABLE IF NOT EXISTS can fail on a unique index in
        # the catalog, so serialize it with an advisory lock.
        with self._lock:
            conn = self._conn()
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"once:{self.table}",))
                conn.execute(self._q(_SCHEMA))

    def table_exists(self) -> bool:
        with self._lock:
            conn = self._conn()
            name = self._ident.as_string(conn)
            row = conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()
        return row is not None and row[0] is not None

    def close(self) -> None:
        """Close the connection. It reopens on next use."""
        with self._lock:
            if self._conn_obj is not None:
                self._conn_obj.close()
                self._conn_obj = None

    def claim(self, key: str, args_hash: str | None) -> str | None:
        """Insert a pending record. Returns an owner token, or None if the key exists."""
        now = _now()
        cur = self._execute(
            "INSERT INTO {table} (key, status, args_hash, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
            (key, Status.PENDING.value, args_hash, now, now),
        )
        return now if cur.rowcount == 1 else None

    def get(self, key: str) -> Record | None:
        with self._lock:
            row = self._execute(
                f"SELECT {_COLUMNS} FROM {{table}} WHERE key = %s", (key,)
            ).fetchone()
        return None if row is None else _record(row)

    def finish(
        self,
        key: str,
        token: str,
        status: Status,
        result: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Move a pending record we own to its final state."""
        cur = self._execute(
            "UPDATE {table} SET status = %s, result = %s, error = %s, updated_at = %s "
            "WHERE key = %s AND status = %s AND updated_at = %s",
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
        now = _next_token(record.updated_at)
        cur = self._execute(
            "UPDATE {table} SET status = %s, result = %s, error = %s, updated_at = %s "
            "WHERE key = %s AND status = %s AND updated_at = %s",
            (status.value, result, error, now, record.key, record.status.value, record.updated_at),
        )
        return now if cur.rowcount == 1 else None

    def records(
        self, statuses: Iterable[Status] | None = None, limit: int | None = None
    ) -> list[Record]:
        """Records, most recently updated first."""
        query = f"SELECT {_COLUMNS} FROM {{table}}"
        params: list[Any] = []
        if statuses is not None:
            query += " WHERE status = ANY(%s)"
            params.append([Status(s).value for s in statuses])
        query += " ORDER BY updated_at DESC, key"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        with self._lock:
            rows = self._execute(query, params).fetchall()
        return [_record(r) for r in rows]

    def keys_with_prefix(self, prefix: str, limit: int = 2) -> list[str]:
        with self._lock:
            rows = self._execute(
                "SELECT key FROM {table} WHERE left(key, %s) = %s ORDER BY key LIMIT %s",
                (len(prefix), prefix, limit),
            ).fetchall()
        return [r[0] for r in rows]

    def purge(self, older_than: float, statuses: Iterable[Status]) -> int:
        """Delete records with one of ``statuses`` not updated for ``older_than`` seconds."""
        values = [Status(s).value for s in statuses]
        if not values:
            return 0
        cur = self._execute(
            "DELETE FROM {table} WHERE updated_at < %s AND status = ANY(%s)",
            (_cutoff(older_than), values),
        )
        return cur.rowcount

    def delete(self, key: str) -> bool:
        cur = self._execute("DELETE FROM {table} WHERE key = %s", (key,))
        return cur.rowcount == 1


def _record(row: Sequence[Any]) -> Record:
    return Record(row[0], Status(row[1]), row[2], row[3], row[4], row[5], row[6])
