# Backends

```python
Once(path=None, *, store=None, ...)
```

- `path` is a SQLite file, `once.db` in the working directory by default.
- A `path` that starts with `postgresql://` or `postgres://` (any case, `postgresql+psycopg://` too) opens a `PostgresStore` on table `once_effects` instead.
- `store` is a store object to use instead of `path`. Passing both raises `TypeError`.

`once.close()` closes the store's connections, and the instance reconnects if used again. `Once` is also a context manager that closes on exit.

## SQLite

The default. One table, `effects`, in WAL mode with a busy timeout (`Once(busy_timeout=5.0)`, in seconds):

```sql
CREATE TABLE effects (key TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT,
  error TEXT, args_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  expires_at TEXT);
```

Each thread gets its own connection in autocommit mode, so every statement is its own transaction. This works across threads and processes on one machine. SQLite over a network filesystem is not supported.

## Postgres

For workers on more than one machine, point them all at one Postgres database:

```
pip install 'agent-once[postgres] @ git+https://github.com/frogscooper/once'
```

This adds psycopg 3.

```python
from once import Once
from once.postgres import PostgresStore

once = Once("postgresql://user:pass@db/app")  # table once_effects, created if missing

# or set the table and options yourself
once = Once(store=PostgresStore("postgresql://user:pass@db/app", table="agent.effects"))
```

```python
PostgresStore(dsn, *, table="once_effects", create=True, connect_timeout=10.0, statement_timeout=30.0)
```

- `dsn` is a libpq connection string or `postgresql://` URL.
- `table` may be schema qualified, like `"agent.effects"`. The schema must already exist. Anything else with dots in it raises `ValueError`.
- `create=False` skips `CREATE TABLE`, for a table you manage yourself. It still runs the one-time `expires_at` migration on an older table (`ALTER TABLE ... ADD COLUMN` and `CREATE INDEX`), so the database user needs those rights once, or you add the column and index yourself.
- `connect_timeout` is in seconds.
- `statement_timeout` is how many seconds one statement may run before the server cancels it, with `SET statement_timeout` on each new connection. Every statement here is a single-row read or write, so 30 is generous; it is there so a lock or a stuck server fails the call instead of hanging it. `None` leaves the server setting alone.

Everything works the same as with SQLite: statuses, `verify`, `stale_after`, `ttl`, `aexecute`, `reset`, `records` and `purge`. The claim is the same `INSERT ... ON CONFLICT DO NOTHING`, and finishing or taking over a record is an `UPDATE` that only matches if the row is unchanged, so the database decides who runs a call, not anything in your process.

Things to know:

- Timestamps come from each worker's clock, so `stale_after` and `ttl` across hosts assume the clocks are roughly in sync. Keep `stale_after` well above both your longest tool call and any drift.
- Each `PostgresStore`, and so each `Once`, holds one connection in autocommit mode, shared by its threads behind a lock.
- If the connection drops, the statement is retried once on a new connection, and fails if that fails too. A write that landed but lost its reply then looks like it lost a race, which is the safe side: the call raises `EffectPending` instead of running twice.

The [CLI](cli.md) takes a URL too: `once ls --db postgresql://... --table agent.effects`.

## Your own store

`Once(store=...)` takes any object that implements the `once.Store` protocol:

| method | does |
|--------|------|
| `claim(key, args_hash)` | insert a pending record, return an owner token or None if the key exists |
| `get(key)` | return the `Record` or None |
| `finish(key, token, status, result=None, error=None, expires_at=None)` | move a pending record you own to its final state |
| `take(record, status, result=None, error=None, expires_at=None)` | change a record only if it is unchanged since it was read, return the new token or None |
| `renew(record, args_hash)` | turn an expired record back into a fresh pending one if unchanged, return a token or None |
| `records(statuses=None, limit=None)` | records, most recently updated first |
| `keys_with_prefix(prefix, limit=2)` | keys starting with `prefix`, for the CLI |
| `purge(older_than, statuses)` | delete old records with those statuses, return the count |
| `purge_expired()` | delete done records past `expires_at`, return the count |
| `delete(key)` | delete one record, return whether it existed |
| `close()` | close connections |

Timestamps are ISO 8601 UTC strings with microseconds. A pending record is owned by whoever set its current `updated_at`; `claim`, `take` and `renew` return that value as the owner token, and `finish` only applies while it still matches. `SQLiteStore` and `PostgresStore` are the reference implementations. See the [API reference](reference/stores.md).
