"""Postgres storage for effect records, for workers on more than one machine.

Needs psycopg 3: pip install 'agent-once[postgres]'.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

try:
    import psycopg
    from psycopg import sql
except ImportError as exc:  # pragma: no cover - depends on what is installed
    raise ImportError("PostgresStore needs psycopg, pip install 'agent-once[postgres]'") from exc

from .store import (
    _COLUMNS,
    Record,
    Status,
    _cutoff,
    _next_token,
    _now,
    libpq_dsn,
    to_record,
)

DEFAULT_TABLE = "once_effects"

T = TypeVar("T")

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
    updated_at TEXT NOT NULL,
    expires_at TEXT
)
"""


class PostgresStore:
    """Effect records in a Postgres table.

    dsn: a libpq connection string or postgresql:// URL. The scheme can be
        any case and can carry a driver suffix (postgresql+psycopg://).
    table: table name, optionally schema qualified ("myschema.effects").
        Created if missing, unless create is False. A table made before ttl
        support gets its expires_at column added either way.

    connect_timeout: seconds to wait for a connection.
    statement_timeout: seconds any one statement may run before the server
        cancels it, 30 by default. None leaves the server's setting alone.

    One connection per store, in autocommit mode, shared by all threads with a
    lock. Every statement is its own transaction, so no row stays locked
    between calls. Safety between processes and hosts comes from the primary
    key and the conditional updates, not from anything in this process.
    If the connection drops, the statement is retried once on a new one. A
    write that landed but lost its reply then looks like it lost a race,
    which is the safe side: the call reports pending instead of running twice.
    """

    def __init__(
        self,
        dsn: str,
        *,
        table: str = DEFAULT_TABLE,
        create: bool = True,
        connect_timeout: float = 10.0,
        statement_timeout: float | None = 30.0,
    ) -> None:
        parts = table.split(".")
        if not 1 <= len(parts) <= 2 or not all(parts):
            raise ValueError(f"bad table name {table!r}")
        self.dsn = libpq_dsn(dsn)
        self.table = table
        self.connect_timeout = connect_timeout
        if statement_timeout is not None and statement_timeout < 0:
            raise ValueError("statement_timeout must be None or 0 or more")
        self.statement_timeout = statement_timeout
        self._ident = sql.Identifier(*parts)
        # The index lives in the table's schema, so CREATE INDEX takes the bare
        # name and the lookup takes the qualified one.
        self._index = f"{parts[-1]}_expires_at"
        self._index_name = sql.Identifier(*parts[:-1], self._index)
        self._conn_obj: psycopg.Connection[Any] | None = None
        self._lock = threading.RLock()
        self._setup(create)

    def _q(self, query: str) -> sql.Composed:
        return sql.SQL(query).format(table=self._ident)

    def _conn(self) -> psycopg.Connection[Any]:
        conn = self._conn_obj
        if conn is None or conn.closed or conn.broken:
            conn = psycopg.connect(
                self.dsn, autocommit=True, connect_timeout=max(1, int(self.connect_timeout))
            )
            if self.statement_timeout is not None:
                ms = sql.Literal(round(self.statement_timeout * 1000))
                try:
                    conn.execute(sql.SQL("SET statement_timeout = {}").format(ms))
                except BaseException:
                    conn.close()
                    raise
            self._conn_obj = conn
        return conn

    def _retrying(self, run: Callable[[psycopg.Connection[Any]], T]) -> T:
        """Run on the connection, and once more on a new one if it dropped."""
        with self._lock:
            conn = self._conn()
            try:
                return run(conn)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # A statement timeout is an OperationalError too, but it leaves
                # the connection usable, so it is not retried.
                if not (conn.closed or conn.broken):
                    raise
            return run(self._conn())

    def _execute(self, query: str, params: Sequence[Any] = ()) -> psycopg.Cursor[Any]:
        return self._retrying(lambda conn: conn.execute(self._q(query), params))

    def _setup(self, create: bool) -> None:
        # Concurrent CREATE TABLE IF NOT EXISTS can fail on a unique index in
        # the catalog, so serialize it with an advisory lock.
        with self._lock:
            conn = self._conn()
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"once:{self.table}",))
                if create:
                    conn.execute(self._q(_SCHEMA))
                # Tables made before ttl support have no expires_at column or
                # index. Only change what is missing, since that needs table
                # ownership.
                name = self._ident.as_string(conn)
                row = conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL, EXISTS (SELECT 1 FROM pg_attribute "
                    "WHERE attrelid = to_regclass(%s) AND attname = 'expires_at' "
                    "AND NOT attisdropped), to_regclass(%s) IS NOT NULL",
                    (name, name, self._index_name.as_string(conn)),
                ).fetchone()
                if row is None or not row[0]:
                    return
                if not row[1]:
                    conn.execute(
                        self._q("ALTER TABLE {table} ADD COLUMN IF NOT EXISTS expires_at TEXT")
                    )
                if not row[2]:
                    # Partial, so it only holds records with a ttl.
                    conn.execute(
                        sql.SQL(
                            "CREATE INDEX IF NOT EXISTS {index} ON {table} (expires_at) "
                            "WHERE expires_at IS NOT NULL"
                        ).format(index=sql.Identifier(self._index), table=self._ident)
                    )

    def table_exists(self) -> bool:
        def run(conn: psycopg.Connection[Any]) -> Any:
            name = self._ident.as_string(conn)
            return conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()

        row = self._retrying(run)
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
        return None if row is None else to_record(row)

    def finish(
        self,
        key: str,
        token: str,
        status: Status,
        result: str | None = None,
        error: str | None = None,
        expires_at: str | None = None,
    ) -> bool:
        """Move a pending record we own to its final state."""
        cur = self._execute(
            "UPDATE {table} SET status = %s, result = %s, error = %s, updated_at = %s, "
            "expires_at = %s WHERE key = %s AND status = %s AND updated_at = %s",
            (status.value, result, error, _now(), expires_at, key, Status.PENDING.value, token),
        )
        return cur.rowcount == 1

    def take(
        self,
        record: Record,
        status: Status,
        result: str | None = None,
        error: str | None = None,
        expires_at: str | None = None,
    ) -> str | None:
        """Change a record only if it is unchanged since it was read.

        Returns the new token, or None if someone else changed it first.
        """
        now = _next_token(record.updated_at)
        cur = self._execute(
            "UPDATE {table} SET status = %s, result = %s, error = %s, updated_at = %s, "
            "expires_at = %s WHERE key = %s AND status = %s AND updated_at = %s",
            (
                status.value,
                result,
                error,
                now,
                expires_at,
                record.key,
                record.status.value,
                record.updated_at,
            ),
        )
        return now if cur.rowcount == 1 else None

    def renew(self, record: Record, args_hash: str | None) -> str | None:
        """Turn an expired record back into a fresh pending one, if it is unchanged.

        Returns an owner token, or None if someone else changed it first.
        """
        now = _next_token(record.updated_at)
        cur = self._execute(
            "UPDATE {table} SET status = %s, result = NULL, error = NULL, args_hash = %s, "
            "created_at = %s, updated_at = %s, expires_at = NULL "
            "WHERE key = %s AND status = %s AND updated_at = %s",
            (
                Status.PENDING.value,
                args_hash,
                now,
                now,
                record.key,
                record.status.value,
                record.updated_at,
            ),
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
        return [to_record(r) for r in rows]

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

    def purge_expired(self) -> int:
        """Delete done records whose ttl has run out."""
        cur = self._execute(
            "DELETE FROM {table} WHERE status = %s AND expires_at IS NOT NULL AND expires_at <= %s",
            (Status.DONE.value, _now()),
        )
        return cur.rowcount

    def delete(self, key: str) -> bool:
        cur = self._execute("DELETE FROM {table} WHERE key = %s", (key,))
        return cur.rowcount == 1
