"""Regression tests for bugs found before launch."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from once import Hooks, Once, Status


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
