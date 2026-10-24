"""Property and state machine tests for the at-most-once rules."""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from once import (
    NOT_HAPPENED,
    EffectFailed,
    EffectPending,
    EffectUnknown,
    Once,
    SQLiteStore,
    Status,
    Store,
)

PG_DSN = os.environ.get("ONCE_TEST_POSTGRES_DSN", "")
KEYS = st.sampled_from(["a", "b", "c"])
PAST = "2000-01-01T00:00:00.000000+00:00"
EXPIRED = "expired"  # a done record whose ttl ran out; still in the table

ints = st.integers(-(2**63), 2**63)


@given(ints, ints, st.booleans())
def test_positional_keyword_and_default_args_share_a_key(a: int, b: int, pass_b: bool) -> None:
    once = Once(":memory:")
    keys: list[str] = []

    def tool(x: int, y: int = 7) -> None: ...

    y = b if pass_b else 7
    keys.append(once._prepare(tool, "t", (a, y), {}, None, None).key)
    keys.append(once._prepare(tool, "t", (), {"x": a, "y": y}, None, None).key)
    keys.append(once._prepare(tool, "t", (a,), {"y": y}, None, None).key)
    if not pass_b:
        keys.append(once._prepare(tool, "t", (a,), {}, None, None).key)
    assert len(set(keys)) == 1


class OnceMachine(RuleBasedStateMachine):
    """Model each key as absent, pending (a crashed owner), done, expired, failed or unknown.

    Every rule checks that the tool ran exactly when the model says it may.
    """

    backend = "sqlite"

    def __init__(self) -> None:
        super().__init__()
        self.store = self._make_store()
        self.once = Once(store=self.store, stale_after=0)
        self.model: dict[str, tuple[Status | str, Any]] = {}
        self.counter = 0

    def _make_store(self) -> Store:
        if self.backend == "postgres":
            from once.postgres import PostgresStore

            self.table = f"sm_{uuid.uuid4().hex[:12]}"
            return PostgresStore(PG_DSN, table=self.table)
        self.dir = tempfile.TemporaryDirectory()
        return SQLiteStore(os.path.join(self.dir.name, "once.db"))

    def teardown(self) -> None:
        if self.backend == "postgres":
            import psycopg

            self.store.close()
            with psycopg.connect(PG_DSN, autocommit=True) as conn:
                conn.execute(f"DROP TABLE IF EXISTS {self.table}")
        else:
            self.store.close()
            self.dir.cleanup()

    def _call(self, key: str, mode: str, verdict: Any = None, use_verify: bool = False) -> None:
        self.counter += 1
        value = self.counter
        ran: list[int] = []
        asked: list[str] = []

        def fn() -> int:
            ran.append(value)
            if mode == "fail":
                raise ValueError("declined")
            if mode == "unknown":
                raise TimeoutError("lost")
            return value

        def verify(k: str, args: dict[str, Any]) -> Any:
            asked.append(k)
            return verdict

        state = self.model.get(key)
        status = state[0] if state else None
        verifiable = status in (Status.PENDING, Status.UNKNOWN) and use_verify
        fresh = status is None or status == EXPIRED
        should_run = fresh or (verifiable and verdict is NOT_HAPPENED)
        try:
            got = self.once.execute(
                fn, key=key, tool_name="t", verify=verify if use_verify else None
            )
        except ValueError:
            assert should_run and mode == "fail"
            self.model[key] = (Status.FAILED, None)
        except TimeoutError:
            assert should_run and mode == "unknown"
            self.model[key] = (Status.UNKNOWN, None)
        except EffectFailed:
            assert status is Status.FAILED
        except EffectUnknown:
            assert status is Status.UNKNOWN and not use_verify
        except EffectPending:
            assert status is Status.PENDING and not use_verify
        else:
            if should_run:
                assert mode == "ok" and got == value
                self.model[key] = (Status.DONE, value)
            elif verifiable:
                assert got == verdict
                self.model[key] = (Status.DONE, verdict)
            else:
                assert status is Status.DONE and got == state[1]  # type: ignore[index]
        assert ran == ([value] if should_run else [])
        assert asked == ([key] if verifiable else [])

    @rule(key=KEYS, mode=st.sampled_from(["ok", "fail", "unknown"]))
    def execute(self, key: str, mode: str) -> None:
        self._call(key, mode)

    @rule(
        key=KEYS,
        mode=st.sampled_from(["ok", "fail", "unknown"]),
        verdict=st.one_of(st.just(NOT_HAPPENED), st.integers(-5, 5), st.just({"id": "ch_1"})),
    )
    def execute_with_verify(self, key: str, mode: str, verdict: Any) -> None:
        self._call(key, mode, verdict, use_verify=True)

    @rule(key=KEYS)
    def reset(self, key: str) -> None:
        assert self.once.reset(key) == (key in self.model)
        self.model.pop(key, None)

    @rule(key=KEYS)
    def crash(self, key: str) -> None:
        """Another process claims the key and dies."""
        token = self.store.claim(key, self._hash())
        if token is not None:
            self.model[key] = (Status.PENDING, None)
        else:
            # claim never takes over a row, even an expired one
            assert key in self.model

    @precondition(lambda self: any(s is Status.DONE for s, _ in self.model.values()))
    @rule(data=st.data())
    def expire(self, data: st.DataObject) -> None:
        done = sorted(k for k, (s, _) in self.model.items() if s is Status.DONE)
        key = data.draw(st.sampled_from(done))
        record = self.store.get(key)
        assert record is not None
        assert self.store.take(record, Status.DONE, record.result, None, PAST) is not None
        self.model[key] = (EXPIRED, None)

    def _hash(self) -> str:
        return self.once._prepare(lambda: None, "t", (), {}, None, None).args_hash

    @invariant()
    def store_matches_model(self) -> None:
        for key in ("a", "b", "c"):
            record = self.store.get(key)
            state = self.model.get(key)
            if state is None:
                assert record is None
            elif state[0] == EXPIRED:
                assert record is not None and record.expired()
            else:
                assert record is not None and record.status is state[0]


SETTINGS = settings(
    max_examples=60,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

TestSQLiteMachine = OnceMachine.TestCase
TestSQLiteMachine.settings = SETTINGS


class PostgresMachine(OnceMachine):
    backend = "postgres"


@pytest.mark.skipif(not PG_DSN, reason="set ONCE_TEST_POSTGRES_DSN to run Postgres tests")
class TestPostgresMachine(PostgresMachine.TestCase):  # type: ignore[misc,valid-type]
    settings = SETTINGS
