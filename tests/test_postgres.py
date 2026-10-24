"""Postgres specific tests. The shared behavior suite runs on Postgres through the db fixture."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from once import Once, SQLiteStore, Status
from once.cli import main
from once.store import is_postgres_dsn, libpq_dsn, open_store

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_import_does_not_load_psycopg() -> None:
    code = "import sys, once; assert 'psycopg' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_psycopg_has_a_clear_error() -> None:
    code = (
        "import sys; sys.modules['psycopg'] = None\n"
        "from once import Once\n"
        "try:\n"
        "    Once('postgresql://localhost/x')\n"
        "except ImportError as exc:\n"
        "    print(exc)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert "agent-once[postgres]" in out


def test_path_and_store_together(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="not both"):
        Once(tmp_path / "a.db", store=SQLiteStore(tmp_path / "b.db"))


def test_store_argument(tmp_path: Path) -> None:
    with Once(store=SQLiteStore(tmp_path / "x.db")) as once:
        assert once.execute(lambda: 1, key="k") == 1
    assert (tmp_path / "x.db").is_file()


def test_bad_table_names() -> None:
    pytest.importorskip("psycopg")
    from once.postgres import PostgresStore

    for name in ["", "a.b.c", ".x", "x."]:
        with pytest.raises(ValueError, match="bad table name"):
            PostgresStore("postgresql://localhost/x", table=name, create=False)


def test_dsn_opens_postgres(pg_schema: str) -> None:
    from once.postgres import PostgresStore

    store = open_store(pg_schema)
    assert isinstance(store, PostgresStore)
    assert store.table == "once_effects"
    assert store.table_exists()
    store.close()
    store.close()  # closing twice is fine


def test_custom_table(pg_schema: str) -> None:
    from once.postgres import PostgresStore

    for table in ["Effects Log", "public.once_custom_test"]:
        store = PostgresStore(pg_schema, table=table)
        try:
            with Once(store=store) as once:
                assert once.execute(lambda: "ok", key="k") == "ok"
                assert once.execute(lambda: "again", key="k") == "ok"
                record = once.get("k")
                assert record is not None and record.status is Status.DONE
            assert store.table_exists()
        finally:
            store._execute("DROP TABLE {table}")
            store.close()
    assert not PostgresStore(pg_schema, table="nope", create=False).table_exists()


def test_reconnects_after_connection_loss(pg_schema: str) -> None:
    import psycopg

    once = Once(pg_schema)
    assert once.execute(lambda: 1, key="a") == 1
    conn = once.store._conn()  # type: ignore[attr-defined]
    pid = conn.info.backend_pid
    with psycopg.connect(pg_schema, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    record = once.get("a")  # reconnects and retries once
    assert record is not None and record.status is Status.DONE
    pid = once.store._conn().info.backend_pid  # type: ignore[attr-defined]
    with psycopg.connect(pg_schema, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert once.execute(lambda: 2, key="b") == 2
    once.close()


def test_statement_timeout(pg_schema: str) -> None:
    import psycopg

    from once.postgres import PostgresStore

    def show(store: PostgresStore) -> str:
        row = store._execute("SHOW statement_timeout").fetchone()
        assert row is not None
        return str(row[0])

    default = PostgresStore(pg_schema)
    assert show(default) == "30s"
    default.close()
    assert show(default) == "30s"  # set again on a new connection
    default.close()
    off = PostgresStore(pg_schema, statement_timeout=None)
    assert show(off) == "0"
    off.close()
    short = PostgresStore(pg_schema, statement_timeout=0.2)
    assert show(short) == "200ms"
    with pytest.raises(psycopg.errors.QueryCanceled):
        short._execute("SELECT pg_sleep(2)")
    assert show(short) == "200ms"  # still the same connection, not retried
    short.close()


def test_threads_create_the_table_at_once(pg_schema: str) -> None:
    from once.postgres import PostgresStore

    errors: list[BaseException] = []
    for i in range(10):

        def open_store(table: str = f"race_{i}") -> None:
            try:
                PostgresStore(pg_schema, table=table).close()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []


WORKER = """
import sys, time
sys.path.insert(0, {src!r})
from once import Once, EffectPending

o = Once({dsn!r}, wait=30)
while time.time() < {start}:
    time.sleep(0.001)

def charge():
    with open({log!r}, "a") as f:
        f.write("x")
    time.sleep(0.5)
    return "ch_1"

print(o.execute(charge, key="k"))
"""


def test_many_processes_race_one_key(pg_schema: str, tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    Once(pg_schema).close()
    procs = 24
    script = WORKER.format(src=SRC, dsn=pg_schema, log=str(log), start=time.time() + 3.0)
    workers = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(procs)
    ]
    outputs = [w.communicate(timeout=60)[0].strip() for w in workers]
    assert [w.returncode for w in workers] == [0] * procs
    assert log.read_text() == "x"
    assert outputs == ["ch_1"] * procs


def test_cli_missing_table(pg_schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ls", "--db", pg_schema, "--table", "nope"])
    assert code == 1
    assert "no table nope" in capsys.readouterr().err


def test_cli_table_option(pg_schema: str, capsys: pytest.CaptureFixture[str]) -> None:
    from once.postgres import PostgresStore

    with Once(store=PostgresStore(pg_schema, table="mine")) as once:
        once.execute(lambda: 1, key="abc")
    assert main(["ls", "--db", pg_schema, "--table", "mine"]) == 0
    assert capsys.readouterr().out.startswith("abc\tdone")


def test_dsn_detection_ignores_case_and_driver_suffix() -> None:
    for dsn in [
        "postgresql://h/db",
        "postgres://h/db",
        "PostgreSQL://h/db",
        "POSTGRES://h/db",
        "postgresql+psycopg://h/db",
        "Postgresql+Psycopg://h/db",
    ]:
        assert is_postgres_dsn(dsn), dsn
        assert libpq_dsn(dsn) == "postgresql://h/db"
    for path in ["once.db", "postgresql.db", "postgres:/x", "./postgresql+x/y.db"]:
        assert not is_postgres_dsn(path), path
    assert libpq_dsn("host=h dbname=db") == "host=h dbname=db"


def test_sqlalchemy_style_dsn_opens_postgres(pg_schema: str) -> None:
    from once.postgres import PostgresStore

    dsn = "PostgreSQL+psycopg://" + pg_schema.split("://", 1)[1]
    store = open_store(dsn)
    assert isinstance(store, PostgresStore)
    with Once(store=store) as once:
        assert once.execute(lambda: 1, key="k") == 1
