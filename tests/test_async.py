from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from once import (
    NOT_HAPPENED,
    EffectFailed,
    EffectUnknown,
    Once,
    OnceError,
    Status,
    args_hash,
    make_key,
)


@pytest.fixture
def once(db: str | Path) -> Once:
    return Once(db)


def status(once: Once, key: str) -> Status:
    record = once.get(key)
    assert record is not None
    return record.status


def test_replay_returns_stored_result(once: Once) -> None:
    calls = 0

    @once.tool
    async def send_email(to: str, subject: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"id": f"msg_{calls}"}

    async def main() -> None:
        first: dict[str, str] = await send_email("a@x.com", "hi")
        again = await send_email(to="a@x.com", subject="hi")
        assert first == again == {"id": "msg_1"}
        await send_email("b@x.com", "hi")

    asyncio.run(main())
    assert calls == 2


def test_gather_same_key_runs_once(once: Once) -> None:
    calls = 0

    @once.tool
    async def charge(amount: int) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "ch_1"

    async def main() -> list[str]:
        return await asyncio.gather(*(charge(100) for _ in range(20)))

    assert asyncio.run(main()) == ["ch_1"] * 20
    assert calls == 1


def test_gather_across_instances_uses_the_db_claim(db: str | Path) -> None:
    calls = 0

    async def charge() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return "ch_1"

    async def main() -> list[Any]:
        instances = [Once(db, wait=2.0) for _ in range(5)]
        return await asyncio.gather(*(o.aexecute(charge, key="k") for o in instances))

    assert asyncio.run(main()) == ["ch_1"] * 5
    assert calls == 1


def test_waiting_on_pending_does_not_block_the_loop(db: str | Path) -> None:
    owner = Once(db)
    token = owner.store.claim("k", args_hash({"tool": "f", "args": {}}))
    assert token is not None
    ticks = 0

    async def f() -> str:
        raise AssertionError("should not run")

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            ticks += 1
            await asyncio.sleep(0.01)

    async def finish_later() -> None:
        await asyncio.sleep(0.2)
        owner.store.finish("k", token, Status.DONE, result='"late"')

    async def main() -> Any:
        result, _, _ = await asyncio.gather(
            Once(db, wait=3.0).aexecute(f, key="k"), ticker(), finish_later()
        )
        return result

    assert asyncio.run(main()) == "late"
    assert ticks == 10


def test_timeout_marks_unknown(once: Once) -> None:
    cancelled = False
    calls = 0

    @once.tool(timeout=0.05)
    async def slow() -> str:
        nonlocal cancelled, calls
        calls += 1
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return "sent"

    async def main() -> None:
        with pytest.raises(EffectUnknown, match="timeout"):
            await slow()
        with pytest.raises(EffectUnknown):
            await slow()

    asyncio.run(main())
    assert cancelled
    assert calls == 1


def test_timeout_error_from_the_tool_is_reraised(once: Once) -> None:
    async def f() -> None:
        raise TimeoutError("upstream timed out")

    async def main() -> None:
        with pytest.raises(TimeoutError, match="upstream"):
            await once.aexecute(f, key="k", timeout=5)

    asyncio.run(main())
    assert status(once, "k") is Status.UNKNOWN


def test_cancellation_marks_unknown(once: Once) -> None:
    started = asyncio.Event()

    @once.tool
    async def post(text: str) -> None:
        started.set()
        await asyncio.sleep(5)

    async def main() -> None:
        task = asyncio.create_task(post("hello"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(EffectUnknown, match="CancelledError"):
            await post("hello")

    asyncio.run(main())


def test_failure_is_stored(once: Once) -> None:
    calls = 0

    async def charge(amount: int) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("declined")

    async def main() -> None:
        with pytest.raises(ValueError):
            await once.aexecute(charge, key="k", amount=1)
        with pytest.raises(EffectFailed):
            await once.aexecute(charge, key="k", amount=1)

    asyncio.run(main())
    assert calls == 1


def make_unknown(once: Once) -> None:
    async def f(amount: int) -> None:
        raise TimeoutError

    async def main() -> None:
        with pytest.raises(TimeoutError):
            await once.aexecute(f, key="k", tool_name="charge", amount=1)

    asyncio.run(main())


def test_sync_verify(once: Once) -> None:
    make_unknown(once)

    def verify(key: str, args: dict[str, Any]) -> Any:
        return {"id": "ch_found", "amount": args["amount"]}

    async def f(amount: int) -> None:
        raise AssertionError("should not run")

    result = asyncio.run(once.aexecute(f, key="k", tool_name="charge", verify=verify, amount=1))
    assert result == {"id": "ch_found", "amount": 1}
    assert status(once, "k") is Status.DONE


def test_async_verify_not_happened_reruns(once: Once) -> None:
    make_unknown(once)

    async def verify(key: str, args: dict[str, Any]) -> Any:
        await asyncio.sleep(0)
        return NOT_HAPPENED

    @once.tool(name="charge", verify=verify)
    async def charge(amount: int) -> str:
        return "ch_2"

    async def main() -> None:
        assert await once.aexecute(charge, key="k", amount=1) == "ch_2"
        assert await once.aexecute(charge, key="k", amount=1) == "ch_2"

    asyncio.run(main())
    assert status(once, "k") is Status.DONE


def test_scope_survives_await(once: Once) -> None:
    seen: list[str | None] = []

    @once.tool(name="post")
    async def post(text: str, idempotency_key: str | None = None) -> None:
        seen.append(idempotency_key)

    async def step() -> None:
        await asyncio.sleep(0.01)
        await post("hi")

    async def main() -> None:
        with once.scope("run_1"):
            await asyncio.sleep(0)
            await step()
            await asyncio.gather(step(), step())

    asyncio.run(main())
    assert seen == [make_key("post", {"text": "hi"}, "run_1")]


def test_aexecute_runs_plain_functions_in_a_thread(once: Once) -> None:
    threads: list[str] = []

    def f() -> str:
        threads.append(threading.current_thread().name)
        return "ok"

    assert asyncio.run(once.aexecute(f, key="k")) == "ok"
    assert threads and threads[0] != threading.current_thread().name


def test_execute_rejects_async_functions(once: Once) -> None:
    async def f() -> None:
        pass

    with pytest.raises(TypeError, match="aexecute"):
        once.execute(f, key="k")


def test_mixed_sync_and_async_on_one_db(db: str | Path) -> None:
    once = Once(db)
    calls = 0

    def charge_sync(amount: int) -> str:
        nonlocal calls
        calls += 1
        return "ch_sync"

    async def charge_async(amount: int) -> str:
        raise AssertionError("should not run")

    assert once.execute(charge_sync, key="k1", tool_name="charge", amount=1) == "ch_sync"
    replay = asyncio.run(once.aexecute(charge_async, key="k1", tool_name="charge", amount=1))
    assert replay == "ch_sync"
    assert calls == 1


def test_sync_caller_waits_for_async_owner_in_same_process(db: str | Path) -> None:
    once = Once(db)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    async def slow(amount: int) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return "ch_async"

    def charge_sync(amount: int) -> str:
        raise AssertionError("should not run")

    results: list[Any] = []
    runner = threading.Thread(
        target=lambda: results.append(
            asyncio.run(once.aexecute(slow, key="k", tool_name="charge", amount=2))
        )
    )
    runner.start()
    assert started.wait(5)
    threading.Timer(0.2, release.set).start()
    # wait=0, but the owner is in this process, so the sync caller waits for it
    assert once.execute(charge_sync, key="k", tool_name="charge", amount=2) == "ch_async"
    runner.join(5)
    assert results == ["ch_async"]
    assert calls == 1


def test_async_caller_waits_for_sync_owner_in_same_process(db: str | Path) -> None:
    once = Once(db)
    started = threading.Event()
    calls = 0

    def slow(amount: int) -> str:
        nonlocal calls
        calls += 1
        started.set()
        time.sleep(0.3)
        return "ch_sync"

    async def charge_async(amount: int) -> str:
        raise AssertionError("should not run")

    results: list[Any] = []
    runner = threading.Thread(
        target=lambda: results.append(once.execute(slow, key="k", tool_name="charge", amount=1))
    )
    runner.start()
    assert started.wait(5)

    async def main() -> tuple[Any, int]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while runner.is_alive():
                ticks += 1
                await asyncio.sleep(0.01)

        result, _ = await asyncio.gather(
            once.aexecute(charge_async, key="k", tool_name="charge", amount=1), ticker()
        )
        return result, ticks

    result, ticks = asyncio.run(main())
    runner.join(5)
    assert result == "ch_sync"
    assert results == ["ch_sync"]
    assert ticks > 5  # the loop kept running while it waited
    assert calls == 1


def test_sync_call_inside_async_owner_does_not_deadlock(once: Once) -> None:
    def inner(amount: int) -> str:
        raise AssertionError("should not run")

    async def outer(amount: int) -> str:
        # a blocking call on the loop thread for the same key must not wait
        # for the coroutine that is blocking it
        with pytest.raises(OnceError, match="inside its own call"):
            once.execute(inner, key="k", tool_name="charge", amount=amount)
        return "ok"

    assert asyncio.run(once.aexecute(outer, key="k", tool_name="charge", amount=1)) == "ok"
