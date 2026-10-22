"""ttl: done records that expire and run again."""

from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from once import NOT_HAPPENED, EffectFailed, EffectUnknown, Once, Status, args_hash
from once.cli import main
from once.store import Record

SHORT = 0.25  # long enough that a slow runner does not expire it early


def expire() -> None:
    time.sleep(SHORT * 1.6)


class Counter:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, delay: float = 0.0) -> int:
        with self.lock:
            self.calls += 1
            n = self.calls
        time.sleep(delay)
        return n


def test_expired_record_runs_again(db: str | Path) -> None:
    count = Counter()
    with Once(db) as once:
        assert once.execute(count, key="k", ttl=SHORT) == 1
        first = once.get("k")
        assert first is not None and first.expires_at is not None
        assert first.expires_at > first.updated_at
        assert once.execute(count, key="k", ttl=SHORT) == 1
        expire()
        assert first.expired() and not first.age() < SHORT
        assert once.execute(count, key="k", ttl=SHORT) == 2
        second = once.get("k")
        assert second is not None and second.status is Status.DONE
        assert second.created_at > first.created_at
        assert second.expires_at is not None and second.expires_at > first.expires_at


def test_unexpired_record_returns_the_cached_result(db: str | Path) -> None:
    count = Counter()
    with Once(db, ttl=timedelta(hours=1)) as once:
        assert once.execute(count, key="k") == 1
        expire()
        assert once.execute(count, key="k") == 1
        record = once.get("k")
        assert record is not None and not record.expired()


def test_no_ttl_never_expires(db: str | Path) -> None:
    count = Counter()
    with Once(db) as once:
        once.execute(count, key="k")
        record = once.get("k")
        assert record is not None and record.expires_at is None and not record.expired()


def test_ttl_precedence(db: str | Path) -> None:
    count = Counter()
    with Once(db, ttl=3600) as once:

        def charge() -> int:
            return count()

        tool = once.tool(ttl=SHORT)(charge)
        assert tool() == 1
        expire()
        assert tool() == 2  # the tool's ttl beats the Once default
        assert once.execute(tool, key="k1") == 3
        expire()
        assert once.execute(tool, key="k1") == 4
        assert once.execute(tool, key="k2", ttl=math.inf) == 5  # the call's beats the tool's
        record = once.get("k2")
        assert record is not None and record.expires_at is None
        assert once.execute(count, key="k3") == 6
        record = once.get("k3")
        assert record is not None and record.expires_at is not None  # Once default


@pytest.mark.parametrize("ttl", [0, -1, math.nan, timedelta(0)])
def test_bad_ttl(tmp_path: Path, ttl: Any) -> None:
    with pytest.raises(ValueError, match="ttl"):
        Once(tmp_path / "once.db", ttl=ttl)
    with Once(tmp_path / "once.db") as once:
        with pytest.raises(ValueError, match="ttl"):
            once.tool(ttl=ttl)
        with pytest.raises(ValueError, match="ttl"):
            once.execute(lambda: 1, key="k", ttl=ttl)


def test_failed_and_unknown_records_do_not_expire(db: str | Path) -> None:
    def declined() -> None:
        raise ValueError("declined")

    def lost() -> None:
        raise TimeoutError("lost")

    with Once(db, ttl=SHORT) as once:
        with pytest.raises(ValueError):
            once.execute(declined, key="failed")
        with pytest.raises(TimeoutError):
            once.execute(lost, key="unknown")
        expire()
        for key in ("failed", "unknown"):
            record = once.get(key)
            assert record is not None and record.expires_at is None and not record.expired()
        with pytest.raises(EffectFailed):
            once.execute(declined, key="failed")
        with pytest.raises(EffectUnknown):
            once.execute(lost, key="unknown")
        assert once.purge(expired=True) == 0


def test_expired_key_takes_new_args(db: str | Path) -> None:
    with Once(db) as once:
        once.execute(lambda amount: amount, key="k", ttl=SHORT, amount=1)
        expire()
        assert once.execute(lambda amount: amount, key="k", ttl=SHORT, amount=2) == 2


def test_verified_result_gets_the_ttl(db: str | Path) -> None:
    def lost() -> None:
        raise TimeoutError("lost")

    with Once(db) as once:
        with pytest.raises(TimeoutError):
            once.execute(lost, key="k")
        found = once.execute(lost, key="k", ttl=SHORT, verify=lambda key, args: "ch_1")
        assert found == "ch_1"
        record = once.get("k")
        assert record is not None and record.status is Status.DONE and record.expires_at
        expire()
        assert once.execute(lambda: "ch_2", key="k") == "ch_2"


def test_race_at_expiry_runs_once(db: str | Path) -> None:
    count = Counter()
    with Once(db) as once:
        once.execute(count, key="k", ttl=SHORT)
    expire()

    n = 8
    start = threading.Barrier(n, timeout=10)
    results: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        # Separate instances, like separate processes.
        with Once(db, wait=10, poll_interval=0.01) as mine:
            start.wait()
            try:
                results.append(mine.execute(count, key="k", ttl=60, delay=0.2))
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert count.calls == 2
    assert results == [2] * n


def test_async_expiry(db: str | Path) -> None:
    count = Counter()

    async def main_() -> None:
        with Once(db) as once:

            @once.tool(ttl=SHORT)
            async def charge() -> int:
                return count()

            assert await charge() == 1
            assert await charge() == 1
            await asyncio.sleep(SHORT * 1.6)
            assert await charge() == 2
            assert await once.aexecute(count, key="k", ttl=SHORT) == 3
            assert await once.aexecute(count, key="k", ttl=SHORT) == 3
            await asyncio.sleep(SHORT * 1.6)
            assert await once.aexecute(count, key="k", ttl=SHORT) == 4

    asyncio.run(main_())


def test_async_verified_result_gets_the_ttl(db: str | Path) -> None:
    async def lost() -> None:
        raise TimeoutError("lost")

    async def verify(key: str, args: dict[str, Any]) -> Any:
        return "ch_1"

    async def main_() -> None:
        with Once(db) as once:
            with pytest.raises(TimeoutError):
                await once.aexecute(lost, key="k")
            assert await once.aexecute(lost, key="k", ttl=SHORT, verify=verify) == "ch_1"
            record = once.get("k")
            assert record is not None and record.expires_at is not None

    asyncio.run(main_())


def test_async_race_at_expiry_runs_once(db: str | Path) -> None:
    count = Counter()
    with Once(db) as once:
        once.execute(count, key="k", ttl=SHORT)
    expire()

    async def main_() -> list[int]:
        instances = [Once(db, wait=10, poll_interval=0.01) for _ in range(6)]

        async def charge() -> int:
            await asyncio.sleep(0.2)
            return count()

        try:
            calls = [o.aexecute(charge, key="k", ttl=60) for o in instances]
            return list(await asyncio.gather(*calls))
        finally:
            for o in instances:
                o.close()

    assert asyncio.run(main_()) == [2] * 6
    assert count.calls == 2


def test_purge_expired(db: str | Path) -> None:
    def declined() -> None:
        raise ValueError("declined")

    with Once(db) as once:
        once.execute(lambda: 1, key="old", ttl=SHORT)
        once.execute(lambda: 2, key="fresh", ttl=3600)
        once.execute(lambda: 3, key="forever")
        with pytest.raises(ValueError):
            once.execute(declined, key="failed", ttl=SHORT)
        expire()
        with pytest.raises(TypeError):
            once.purge()
        assert once.purge(expired=True) == 1
        assert sorted(r.key for r in once.records()) == ["failed", "forever", "fresh"]
        assert once.purge(expired=True) == 0
        once.execute(lambda: 1, key="old", ttl=SHORT)
        expire()
        assert once.purge(0, expired=True) == 3  # old by ttl, forever and fresh by age
        assert [r.key for r in once.records()] == ["failed"]


def test_cli_gc_expired(db: str | Path, capsys: pytest.CaptureFixture[str]) -> None:
    with Once(db) as once:
        once.execute(lambda: 1, key="old", ttl=SHORT)
        once.execute(lambda: 2, key="fresh", ttl=3600)
    expire()
    assert main(["show", "--db", str(db), "fresh"]) == 0
    assert '"expires_at": "20' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["gc", "--db", str(db)])
    assert "--expired" in capsys.readouterr().err
    assert main(["gc", "--db", str(db), "--expired"]) == 0
    assert capsys.readouterr().out.strip() == "deleted 1 record"
    assert main(["gc", "--db", str(db), "--expired", "--older-than", "0"]) == 0
    assert capsys.readouterr().out.strip() == "deleted 1 record"


# Migration from tables made before ttl support.

OLD_SCHEMA = """
CREATE TABLE {table} (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    args_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
OLD_HASH = args_hash({"tool": "charge", "args": {}})
OLD_ROW = (
    "INSERT INTO {table} VALUES ('old', 'done', '\"ch_1\"', NULL, '{hash}', "
    "'2026-01-01T00:00:00.000000+00:00', '2026-01-01T00:00:00.000000+00:00')"
)


def check_migrated(target: str | Path) -> None:
    count = Counter()

    def charge() -> int:
        return count()

    with Once(target, ttl=SHORT) as once:
        record = once.get("old")
        assert isinstance(record, Record) and record.expires_at is None
        assert once.execute(charge, key="old") == "ch_1"  # old records never expire
        assert once.execute(charge, key="new") == 1
        expire()
        assert once.execute(charge, key="new") == 2
        assert once.purge(expired=True) == 0
    with Once(target) as again:  # migrating twice is a no-op
        assert again.execute(charge, key="old") == "ch_1"


def test_sqlite_migration(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(OLD_SCHEMA.format(table="effects"))
    conn.execute(OLD_ROW.format(table="effects", hash=OLD_HASH))
    conn.commit()
    conn.close()
    check_migrated(path)
    conn = sqlite3.connect(path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(effects)")]
    conn.close()
    assert columns[-1] == "expires_at"


def test_postgres_migration(pg_schema: str) -> None:
    import psycopg

    from once.postgres import PostgresStore

    with psycopg.connect(pg_schema, autocommit=True) as conn:
        conn.execute(OLD_SCHEMA.format(table="once_effects"))
        conn.execute(OLD_ROW.format(table="once_effects", hash=OLD_HASH))
    # create=False still adds the column, since every read needs it
    store = PostgresStore(pg_schema, create=False)
    assert store.get("old") is not None
    store.close()
    check_migrated(pg_schema)
    with psycopg.connect(pg_schema, autocommit=True) as conn:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'once_effects' AND table_schema = current_schema()"
        )
        assert "expires_at" in {row[0] for row in cur.fetchall()}


def test_postgres_missing_table_with_create_false(pg_schema: str) -> None:
    from once.postgres import PostgresStore

    store = PostgresStore(pg_schema, table="nope", create=False)
    assert not store.table_exists()
    store.close()


def test_not_happened_after_expiry_is_unaffected(db: str | Path) -> None:
    # verify only runs for unknown or stale records, never for expired ones
    seen: list[str] = []

    def verify(key: str, args: dict[str, Any]) -> Any:
        seen.append(key)
        return NOT_HAPPENED

    with Once(db) as once:
        once.execute(lambda: 1, key="k", ttl=SHORT, verify=verify)
        expire()
        assert once.execute(lambda: 2, key="k", ttl=SHORT, verify=verify) == 2
    assert seen == []


def open_at_once(target: str | Path, n: int = 8) -> None:
    start = threading.Barrier(n, timeout=10)
    errors: list[BaseException] = []

    def worker() -> None:
        start.wait()
        try:
            Once(target).close()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_sqlite_migration_race(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(OLD_SCHEMA.format(table="effects"))
    conn.execute(OLD_ROW.format(table="effects", hash=OLD_HASH))
    conn.commit()
    conn.close()
    open_at_once(path)
    check_migrated(path)


def test_postgres_migration_race(pg_schema: str) -> None:
    import psycopg

    with psycopg.connect(pg_schema, autocommit=True) as conn:
        conn.execute(OLD_SCHEMA.format(table="once_effects"))
        conn.execute(OLD_ROW.format(table="once_effects", hash=OLD_HASH))
    open_at_once(pg_schema)
    check_migrated(pg_schema)
