"""Races and odd paths that are hard to hit naturally, forced with monkeypatch."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from once import NOT_HAPPENED, EffectFailed, EffectPending, KeyConflict, Once, Status
from once.store import Record, SQLiteStore


@pytest.fixture
def once(tmp_path: Path) -> Once:
    return Once(tmp_path / "once.db")


def charge(amount: int) -> str:
    return "ch_1"


async def acharge(amount: int) -> str:
    return "ch_1"


def unknown(once: Once, key: str = "k") -> None:
    def boom(amount: int) -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        once.execute(boom, key=key, tool_name="charge", amount=1)


def fail_once(fn: Callable[..., Any], value: Any) -> Callable[..., Any]:
    calls = 0

    def patched(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return value if calls == 1 else fn(*args, **kwargs)

    return patched


def test_timeout_passes_through_errors(once: Once) -> None:
    def f() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError):
        once.execute(f, key="k", timeout=5)
    with pytest.raises(EffectFailed):
        once.execute(f, key="k", timeout=5)


def test_record_deleted_between_claim_and_read(once: Once, monkeypatch: pytest.MonkeyPatch) -> None:
    # first claim loses, then the record vanishes, then the next claim wins
    monkeypatch.setattr(once.store, "claim", fail_once(once.store.claim, None))
    monkeypatch.setattr(once.store, "get", fail_once(once.store.get, None))
    assert once.execute(charge, key="k", tool_name="charge", amount=1) == "ch_1"

    monkeypatch.setattr(once.store, "claim", fail_once(SQLiteStore.claim.__get__(once.store), None))
    monkeypatch.setattr(once.store, "get", fail_once(SQLiteStore.get.__get__(once.store), None))
    assert asyncio.run(once.aexecute(acharge, key="k2", tool_name="charge", amount=1)) == "ch_1"


def test_record_that_keeps_vanishing(once: Once, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(once.store, "claim", lambda key, h: None)
    monkeypatch.setattr(once.store, "get", lambda key: None)
    with pytest.raises(EffectPending, match="still pending"):
        once.execute(charge, key="k", tool_name="charge", amount=1)
    with pytest.raises(EffectPending):
        asyncio.run(once.aexecute(acharge, key="k", tool_name="charge", amount=1))


@pytest.mark.parametrize("verdict", [NOT_HAPPENED, "ch_found"])
def test_lost_takeover_race_reads_again(
    once: Once, monkeypatch: pytest.MonkeyPatch, verdict: Any
) -> None:
    unknown(once)
    unknown(once, "k2")
    monkeypatch.setattr(once.store, "take", fail_once(once.store.take, None))

    def verify(key: str, args: dict[str, Any]) -> Any:
        return verdict

    expected = "ch_1" if verdict is NOT_HAPPENED else "ch_found"
    assert once.execute(charge, key="k", tool_name="charge", amount=1, verify=verify) == expected
    monkeypatch.setattr(once.store, "take", fail_once(SQLiteStore.take.__get__(once.store), None))
    result = asyncio.run(
        once.aexecute(acharge, key="k2", tool_name="charge", amount=1, verify=verify)
    )
    assert result == expected


def test_async_key_conflict(once: Once) -> None:
    asyncio.run(once.aexecute(acharge, key="k", amount=1))
    with pytest.raises(KeyConflict):
        asyncio.run(once.aexecute(acharge, key="k", amount=2))


def test_async_unserializable_result(once: Once) -> None:
    from once import EffectUnknown

    async def f() -> object:
        return object()

    with pytest.raises(EffectUnknown, match="not JSON serializable"):
        asyncio.run(once.aexecute(f, key="k"))


def test_sync_verify_returning_awaitable(once: Once) -> None:
    unknown(once)

    async def lookup() -> str:
        return "ch_found"

    def verify(key: str, args: dict[str, Any]) -> Any:
        return lookup()

    result = asyncio.run(
        once.aexecute(acharge, key="k", tool_name="charge", amount=1, verify=verify)
    )
    assert result == "ch_found"


def test_setup_reraises_other_errors(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(tmp_path)  # a directory, not a file


def test_setup_retries_while_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = sqlite3.Connection.execute
    calls = 0

    class Flaky(sqlite3.Connection):
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            nonlocal calls
            if sql.startswith("PRAGMA journal_mode") and calls < 2:
                calls += 1
                raise sqlite3.OperationalError("database is locked")
            return real(self, sql, *args)

    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=Flaky, **kw))
    SQLiteStore(tmp_path / "once.db").close()
    assert calls == 2


def test_take_with_a_coarse_clock(once: Once, monkeypatch: pytest.MonkeyPatch) -> None:
    import once.store as store_module

    unknown(once)
    record = once.get("k")
    assert record is not None
    monkeypatch.setattr(store_module, "_now", lambda: record.updated_at)
    token = once.store.take(record, Status.PENDING)
    assert token is not None and token > record.updated_at


def test_finish_without_ownership_is_ignored(once: Once) -> None:
    token = once.store.claim("k", None)
    assert token is not None
    assert not once.store.finish("k", "not-the-token", Status.DONE, result="1")
    assert once.store.finish("k", token, Status.DONE, result="1")


def test_close_and_reuse(once: Once) -> None:
    assert once.execute(charge, key="k", amount=1) == "ch_1"
    once.close()
    assert once.execute(charge, key="k", amount=1) == "ch_1"
    record = once.get("k")
    assert isinstance(record, Record)
