"""The ``db`` fixture runs a test against SQLite and, if configured, Postgres.

Set ONCE_TEST_POSTGRES_DSN to a postgresql:// URL to run the Postgres side.
Each test gets its own schema, so the default table name is used everywhere.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

PG_DSN = os.environ.get("ONCE_TEST_POSTGRES_DSN", "")


def schema_dsn(base: str, schema: str) -> str:
    """The URL with search_path set to ``schema``."""
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-csearch_path%3D{schema}"


@pytest.fixture
def pg_schema() -> Iterator[str]:
    """A fresh schema in the test database, dropped afterwards. Yields its DSN."""
    if not PG_DSN:
        pytest.skip("set ONCE_TEST_POSTGRES_DSN to run Postgres tests")
    import psycopg

    schema = f"once_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield schema_dsn(PG_DSN, schema)
    finally:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(params=["sqlite", "postgres"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> str | Path:
    """A SQLite file path or a postgresql:// URL. Once(db) works with either."""
    if request.param == "sqlite":
        return tmp_path / "once.db"
    dsn: str = request.getfixturevalue("pg_schema")
    return dsn
