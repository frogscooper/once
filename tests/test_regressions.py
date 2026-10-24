"""Regression tests for bugs found before launch."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from once import EffectUnknown, Hooks, Once, OnceError, Status
from once.cli import main


def test_timeout_keeps_scope(db: str | Path) -> None:
    once = Once(db)
    seen: list[str | None] = []

    @once.tool
    def inner(n: int) -> int:
        return n

    @once.tool(timeout=5)
    def outer(n: int) -> int:
        from once import current_scope

        seen.append(current_scope())
        return inner(n)

    with once.scope("order-1"):
        outer(1)
    assert seen == ["order-1"]
    # inner ran under the order-1 scope, so another order does not replay it
    with once.scope("order-2"):
        assert once.execute(inner, n=1) == 1
    assert len(once.records()) == 3
    once.close()


def test_huge_ttl_means_never_expire(db: str | Path) -> None:
    once = Once(db)
    calls: list[int] = []

    def charge() -> str:
        calls.append(1)
        return "ok"

    assert once.execute(charge, key="k", ttl=timedelta.max) == "ok"
    assert once.execute(charge, key="k2", ttl=1e15) == "ok"
    assert once.get("k").status is Status.DONE  # type: ignore[union-attr]
    assert once.execute(charge, key="k", ttl=timedelta.max) == "ok"
    assert len(calls) == 2
    once.close()


def test_interrupt_in_claim_hook_gives_the_claim_back(db: str | Path) -> None:
    def on_claim(event: object) -> None:
        raise KeyboardInterrupt

    once = Once(db, hooks=Hooks(on_claim=on_claim))
    calls: list[int] = []
    with pytest.raises(KeyboardInterrupt):
        once.execute(lambda: calls.append(1), key="k")
    assert calls == []
    assert once.get("k") is None
    once.hooks = None
    once.execute(lambda: calls.append(1), key="k")
    assert calls == [1]
    once.close()


def test_cancel_in_async_claim_hook_gives_the_claim_back(db: str | Path) -> None:
    started = asyncio.Event()

    async def on_claim(event: object) -> None:
        started.set()
        await asyncio.sleep(10)

    once = Once(db, hooks=Hooks(on_claim=on_claim))
    calls: list[int] = []

    async def charge() -> None:
        calls.append(1)

    async def main_() -> None:
        task = asyncio.create_task(once.aexecute(charge, key="k"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main_())
    assert calls == []
    assert once.get("k") is None
    assert once._inflight == {}
    once.close()


def test_cancel_during_async_claim_does_not_leak(db: str | Path) -> None:
    once = Once(db)
    gate = threading.Event()
    real_claim = once.store.claim

    claimed = threading.Event()

    def slow_claim(key: str, h: str | None) -> str | None:
        gate.wait(5)
        token = real_claim(key, h)
        claimed.set()
        return token

    once.store.claim = slow_claim  # type: ignore[method-assign,assignment]
    calls: list[int] = []

    async def charge() -> int:
        calls.append(1)
        return 1

    async def main_() -> None:
        task = asyncio.create_task(once.aexecute(charge, key="k"))
        await asyncio.sleep(0.1)
        task.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert claimed.wait(5)
        for _ in range(100):
            if not once._inflight and once.get("k") is None:
                break
            await asyncio.sleep(0.02)

    asyncio.run(main_())
    assert calls == []
    assert once._inflight == {}
    assert once.get("k") is None
    once.store.claim = real_claim  # type: ignore[method-assign]
    # a sync caller is not stuck waiting on a call that never ran
    assert once.execute(lambda: 2, key="k") == 2
    once.close()


def test_cli_leaves_other_sqlite_files_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER)")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    assert main(["ls", "--db", str(path)]) != 0
    assert "effects" in capsys.readouterr().err
    assert path.read_bytes() == before
    conn = sqlite3.connect(path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert tables == ["users"]
    assert mode == "delete"


def test_finish_survives_one_store_error(db: str | Path) -> None:
    once = Once(db)
    real_finish = once.store.finish
    fails = [sqlite3.OperationalError("connection lost")]

    def flaky_finish(*args: Any, **kwargs: Any) -> bool:
        if fails:
            raise fails.pop()
        return real_finish(*args, **kwargs)

    once.store.finish = flaky_finish  # type: ignore[method-assign]
    assert once.execute(lambda: "charged", key="k") == "charged"
    assert once.get("k").status is Status.DONE  # type: ignore[union-attr]
    once.store.finish = real_finish  # type: ignore[method-assign]
    once.close()


def test_async_finish_survives_one_store_error(db: str | Path) -> None:
    once = Once(db)
    real_finish = once.store.finish
    fails = [sqlite3.OperationalError("connection lost")]

    def flaky_finish(*args: Any, **kwargs: Any) -> bool:
        if fails:
            raise fails.pop()
        return real_finish(*args, **kwargs)

    once.store.finish = flaky_finish  # type: ignore[method-assign]

    async def charge() -> str:
        return "charged"

    assert asyncio.run(once.aexecute(charge, key="k")) == "charged"
    assert once.get("k").status is Status.DONE  # type: ignore[union-attr]
    once.store.finish = real_finish  # type: ignore[method-assign]
    once.close()


def test_postgres_connection_drop_mid_call_still_records(pg_schema: str) -> None:
    import psycopg

    once = Once(pg_schema)
    once.execute(lambda: None, key="warm")
    pid = once.store._conn_obj.info.backend_pid  # type: ignore[attr-defined]

    def charge() -> str:
        with psycopg.connect(pg_schema, autocommit=True) as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        return "charged"

    assert once.execute(charge, key="k") == "charged"
    assert once.get("k").status is Status.DONE  # type: ignore[union-attr]
    once.close()


def test_recursive_sync_call_raises_instead_of_hanging(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    box: dict[str, Any] = {}

    @once.tool
    def charge(n: int) -> int:
        return once.execute(charge, key="k", n=n)  # type: ignore[no-any-return]

    def target() -> None:
        try:
            once.execute(charge, key="k", n=1)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "deadlocked"
    assert isinstance(box["error"], OnceError)
    assert "inside its own call" in str(box["error"])
    # a fresh call after that is fine and sees the failure
    record = once.get("k")
    assert record is not None and record.status is Status.FAILED
    once.close()


def test_recursive_async_call_raises_instead_of_hanging(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")

    @once.tool
    async def charge(n: int) -> int:
        return await once.aexecute(charge, key="k", n=n)  # type: ignore[no-any-return]

    async def main_() -> None:
        with pytest.raises(OnceError, match="inside its own call"):
            await asyncio.wait_for(once.aexecute(charge, key="k", n=1), 5)

    asyncio.run(main_())
    once.close()


def test_a_later_task_started_by_fn_is_not_recursion(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")
    tasks: list[asyncio.Task[Any]] = []

    @once.tool
    async def charge(n: int) -> int:
        async def later() -> int:
            await asyncio.sleep(0.05)
            return await once.aexecute(charge, key="k", n=n)  # type: ignore[no-any-return]

        tasks.append(asyncio.ensure_future(later()))
        return n

    async def main_() -> None:
        assert await once.aexecute(charge, key="k", n=1) == 1
        assert await tasks[0] == 1

    asyncio.run(main_())
    once.close()


def test_exclude_with_an_unknown_name_raises_when_decorating(tmp_path: Path) -> None:
    once = Once(tmp_path / "once.db")

    def charge(amount: int, ctx: object = None) -> int:
        return amount

    with pytest.raises(ValueError, match="'cxt'"):
        once.tool(exclude=["cxt"])(charge)
    with pytest.raises(ValueError, match="not parameters of charge"):
        once.tool(exclude="ctx")(charge)  # a str is a collection of letters
    assert once.tool(exclude=["ctx"])(charge)(1, ctx=object()) == 1
    once.close()


def _reset_right_after_finish(once: Once) -> None:
    finish = once.store.finish

    def finish_then_reset(key: str, *args: Any, **kwargs: Any) -> Any:
        out = finish(key, *args, **kwargs)
        once.store.delete(key)
        return out

    once.store.finish = finish_then_reset  # type: ignore[method-assign]


def test_unknown_after_a_concurrent_reset_still_raises_effect_unknown(db: str | Path) -> None:
    once = Once(db)
    _reset_right_after_finish(once)
    with pytest.raises(EffectUnknown) as info:
        once.execute(lambda: object(), key="k", tool_name="t")
    assert info.value.key == "k"
    assert info.value.record.status is Status.UNKNOWN
    assert "not JSON serializable" in (info.value.record.error or "")

    async def main_() -> None:
        with pytest.raises(EffectUnknown) as info:
            await once.aexecute(lambda: object(), key="k2", tool_name="t")
        assert info.value.record.status is Status.UNKNOWN

    asyncio.run(main_())
    once.close()
